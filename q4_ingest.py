"""
Quant4all — Ingesta desde el Flex Web Service de IBKR (§18 de la spec).

Protocolo de dos pasos:
  1. GET /SendRequest?t={token}&q={queryId}&v=3[&fd=..&td=..]
     -> <FlexStatementResponse><Status>Success</Status>
        <ReferenceCode>..</ReferenceCode><Url>..</Url>
  2. GET {Url}?t={token}&q={referenceCode}&v=3
     -> el XML del statement, o <Status>Warn</Status><ErrorCode>1019</ErrorCode>
        si todavía se está generando -> reintentar

Los parámetros fd/td permiten sobrescribir el rango de fechas de la query
guardada (máx. 365 días). Es lo que permite que la aplicación trocee el
histórico sin que el usuario reconfigure nada en el Client Portal.

Límites de ritmo documentados por IBKR para /SendRequest:
    1 petición por segundo, máximo 10 por minuto.

SEGURIDAD: el token viaja en la query string. Nunca registrar la URL completa
en logs. Ver `_redact`.
"""

from __future__ import annotations

import os
import time
import hashlib
import logging
import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

log = logging.getLogger("q4.ingest")

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND = BASE + "/SendRequest"

# Errores transitorios de IBKR: se reintentan con backoff. 1018 ("too many
# requests") es un límite de ritmo puntual y entra aquí. 1001 ("Statement could
# not be generated at this time. Please try again shortly") es un fallo temporal
# de generación del servidor —el propio mensaje pide reintentar— y también entra.
# 1025 ("too many failed attempts") NO: reintentarlo realimenta el castigo.
RETRYABLE = {"1001", "1009", "1018", "1019", "1021"}
FATAL_HINTS = {
    # 1003 observado en producción: el mensaje real de IBKR es "Statement is not
    # available". No significa "query inválida" (eso da 1020); significa que no
    # hay statement para el rango pedido, típicamente porque `td` va más allá del
    # último cierre generado. Se modela como StatementNotAvailable.
    "1003": "El statement no está disponible para el rango pedido "
            "(¿td más allá del último cierre generado, fin de semana o festivo?)",
    "1012": "El token ha caducado o ha sido revocado. Regenéralo en Client Portal",
    "1013": "La query ha sido eliminada en Client Portal",
    # 1015 observado en producción (2026-08-18): mismo remedio que el 1012,
    # pero como mensaje ("Token is invalid") en vez de expiración/revocación
    # explícita. Se trata igual — ver TokenExpired más abajo.
    "1015": "El token no es válido. Regenéralo en Client Portal",
    # 1018/1020/1025 observados en producción durante esta puesta en marcha.
    "1018": "Demasiadas peticiones seguidas: límite de ritmo. Reintentable con backoff",
    # 1020 observado también con un token de formato incorrecto, no sólo con
    # fd/td mal: IBKR no puede ni validar la petición.
    "1020": "Petición inválida: token con formato incorrecto, o formato/rango de fd/td",
    "1025": "Demasiados intentos fallidos: IBKR ha bloqueado el token temporalmente. "
            "Reintentar lo realimenta; hay que dejarlo reposar (minutos u horas)",
}

MAX_WINDOW_DAYS = 365
SEND_MIN_INTERVAL = 1.1          # límite: 1 req/s
SEND_PER_MINUTE = 10

# Reintento ante errores de RED (no de IBKR): connection reset, timeout, 5xx.
NET_RETRIES = 4
NET_BACKOFF = 2.0                # segundos, se duplica en cada intento
POLL_MAX_WAIT = 60.0             # techo del backoff de sondeo (evita esperas absurdas)


def _redact(url: str) -> str:
    """Nunca dejar el token en un log."""
    import re
    return re.sub(r"([?&]t=)[^&]*", r"\1<redacted>", url)


class FlexError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        hint = FATAL_HINTS.get(code)
        super().__init__(f"[{code}] {message}" + (f" — {hint}" if hint else ""))


class TokenExpired(FlexError):
    """1012/1015 — requieren acción del usuario (token nuevo).

    1015 ("Token is invalid") observado en producción con el mismo remedio
    que el 1012 (caducado/revocado): sin un token nuevo del Client Portal no
    hay forma de que un reintento tenga éxito, así que se trata igual —
    pausa la sincronización en vez de seguir reintentando en vano.
    """
    pass


class StatementNotAvailable(FlexError):
    """1003 — no hay statement para el rango pedido.

    Observado contra IBKR real: pedir `td` más allá del último cierre generado
    (mañanas antes del proceso nocturno, fines de semana, festivos) devuelve
    1003 para TODO el bloque, no un statement truncado. No es un error de
    usuario ni un fallo transitorio de red: es "aún no hay datos". El
    orquestador lo trata como `no_new_data`, no como reintento.
    """
    pass


class RateLimitBlocked(FlexError):
    """1025 — "too many failed attempts": IBKR bloquea el token temporalmente.

    Observado en esta puesta en marcha tras una ráfaga de peticiones fallidas.
    A diferencia del 1018 (límite de ritmo puntual, reintentable), reintentar el
    1025 realimenta el castigo. El orquestador pausa y deja reposar el token en
    vez de seguir pidiendo.
    """
    pass


# --------------------------------------------------------------------------
# Limitador de ritmo
# --------------------------------------------------------------------------

class _Pacer:
    def __init__(self):
        self._last = 0.0
        self._window: list[float] = []

    def wait(self):
        now = time.monotonic()
        gap = SEND_MIN_INTERVAL - (now - self._last)
        if gap > 0:
            time.sleep(gap)
        self._window = [t for t in self._window if time.monotonic() - t < 60]
        if len(self._window) >= SEND_PER_MINUTE:
            sleep_for = 60 - (time.monotonic() - self._window[0]) + 0.5
            log.info("Límite de 10/min alcanzado, esperando %.1fs", sleep_for)
            time.sleep(max(sleep_for, 0))
            self._window = []
        self._last = time.monotonic()
        self._window.append(self._last)


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------

@dataclass
class FlexClient:
    token: str
    query_id: str
    raw_dir: str = "./raw_ibkr"
    max_poll: int = 12
    poll_wait: float = 5.0
    max_send_retries: int = 6        # reintentos del SendRequest ante códigos transitorios
    session: requests.Session = field(default_factory=requests.Session)
    _pacer: _Pacer = field(default_factory=_Pacer)

    # -- protocolo ---------------------------------------------------------

    def _parse_envelope(self, text: str) -> ET.Element | None:
        """Devuelve el nodo raíz si es un sobre de control; None si es un statement."""
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise FlexError("PARSE", "Respuesta no es XML válido")
        return root if root.tag == "FlexStatementResponse" else None

    def _get(self, url: str, params: dict, timeout: float) -> requests.Response:
        """GET con reintento ante errores de RED transitorios y HTTP 5xx.

        El resto del cliente maneja los errores de IBKR, que llegan con HTTP 200
        y un sobre de control. Pero un connection reset, un timeout o un 5xx son
        de la capa de red y tumbaban el job (nos pasó con un 'Connection reset by
        peer'). Se reintentan con backoff. El token nunca aparece en el log.
        """
        last = None
        for attempt in range(NET_RETRIES):
            try:
                r = self.session.get(url, params=params, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last = e
            else:
                if not (500 <= r.status_code < 600):
                    r.raise_for_status()           # 4xx (raro) -> error duro, no se reintenta
                    return r
                last = requests.HTTPError(f"HTTP {r.status_code}")
            if attempt < NET_RETRIES - 1:
                wait = NET_BACKOFF * (2 ** attempt)
                log.warning("Red inestable (%s), reintento %d/%d en %.0fs",
                            type(last).__name__, attempt + 1, NET_RETRIES, wait)
                time.sleep(wait)
        raise FlexError("NET", f"Red inestable tras {NET_RETRIES} intentos: {last}")

    @staticmethod
    def _raise_for_code(code: str, msg: str) -> None:
        """Traduce los códigos de IBKR que tienen tratamiento propio."""
        if code in ("1012", "1015"):
            raise TokenExpired(code, msg)
        if code == "1003":
            raise StatementNotAvailable(code, msg)
        if code == "1025":
            raise RateLimitBlocked(code, msg)

    def request_statement(self, fd: str | None = None, td: str | None = None,
                          on_progress=None) -> tuple[str, str]:
        params = {"t": self.token, "q": self.query_id, "v": "3"}
        if fd or td:
            if not (fd and td):
                raise ValueError("fd y td deben ir juntos")
            if (dt.datetime.strptime(td, "%Y%m%d") -
                    dt.datetime.strptime(fd, "%Y%m%d")).days > MAX_WINDOW_DAYS:
                raise ValueError(f"El rango {fd}–{td} supera {MAX_WINDOW_DAYS} días")
            params.update(fd=fd, td=td)

        for attempt in range(self.max_send_retries):
            if on_progress:
                on_progress(attempt + 1, self.max_send_retries, "send")
            self._pacer.wait()
            r = self._get(SEND, params, timeout=60)
            log.debug("SendRequest %s", _redact(r.url))
            root = self._parse_envelope(r.text)
            if root is None:
                raise FlexError("PROTO", "SendRequest devolvió un statement en vez de un sobre")
            status = (root.findtext("Status") or "").strip()
            if status == "Success":
                return root.findtext("ReferenceCode").strip(), root.findtext("Url").strip()
            code = (root.findtext("ErrorCode") or "?").strip()
            msg = (root.findtext("ErrorMessage") or "").strip()
            self._raise_for_code(code, msg)            # 1012 / 1003 / 1025
            if code in RETRYABLE:
                wait = min(self.poll_wait * (1.6 ** attempt), POLL_MAX_WAIT)
                log.info("SendRequest código %s, reintento %d/%d en %.0fs",
                         code, attempt + 1, self.max_send_retries, wait)
                time.sleep(wait)
                continue
            raise FlexError(code, msg)
        raise FlexError("TIMEOUT", f"SendRequest no tuvo éxito tras {self.max_send_retries} intentos")

    def get_statement(self, url: str, ref: str, on_progress=None) -> str:
        for attempt in range(self.max_poll):
            if on_progress:
                on_progress(attempt + 1, self.max_poll, "poll")
            self._pacer.wait()
            r = self._get(url, {"t": self.token, "q": ref, "v": "3"}, timeout=180)
            root = self._parse_envelope(r.text)
            if root is None:
                return r.text                      # es el statement
            code = (root.findtext("ErrorCode") or "?").strip()
            msg = (root.findtext("ErrorMessage") or "").strip()
            self._raise_for_code(code, msg)        # 1012 / 1003 / 1025
            if code in RETRYABLE:
                wait = min(self.poll_wait * (1.6 ** attempt), POLL_MAX_WAIT)
                log.info("Código %s, reintento %d/%d en %.0fs", code, attempt + 1, self.max_poll, wait)
                time.sleep(wait)
                continue
            raise FlexError(code, msg)
        raise FlexError("TIMEOUT", f"El statement no se generó tras {self.max_poll} intentos")

    # -- descubrimiento del último cierre disponible ----------------------

    def latest_available_date(self, on_progress=None) -> str | None:
        """max(reportDate) que IBKR tiene ya generado, vía la query sin fd/td.

        Necesario porque IBKR devuelve 1003 para TODO el bloque si `td` supera
        el último cierre generado (verificado contra el servicio real). El
        incremental descubre esta fecha primero y pide sólo hasta ella, en vez
        de pedir hasta hoy y estrellarse cada mañana, fin de semana y festivo.

        La query se ejecuta con su periodo guardado (relativo a "ahora"), así
        que su fecha máxima es el último cierre disponible sea cual sea su
        configuración. Devuelve None si el statement no trae ninguna fecha.
        """
        ref, url = self.request_statement(on_progress=on_progress)
        xml = self.get_statement(url, ref, on_progress=on_progress)
        root = ET.fromstring(xml)
        dates = sorted(
            e.get("reportDate")
            for e in root.iter("EquitySummaryByReportDateInBase")
            if e.get("reportDate")
        )
        return dates[-1] if dates else None

    # -- almacenamiento inmutable -----------------------------------------

    def fetch(self, fd: str | None = None, td: str | None = None,
              on_progress=None) -> str:
        """Descarga un bloque y lo guarda tal cual. Devuelve la ruta del fichero.

        El XML crudo se guarda SIEMPRE y nunca se modifica. Cualquier bug de
        parseo se corrige reprocesando el crudo, jamás volviendo a descargar:
        IBKR puede haber cambiado datos históricos entre descargas y eso
        rompería la reproducibilidad de los golden values.

        on_progress(intento, total, fase) — "send" (pidiendo la referencia,
        normalmente rápido) o "poll" (esperando a que IBKR termine de
        generar el informe, la parte que puede llevar varios minutos en
        bloques grandes). Puramente informativo — no cambia reintentos ni
        temporizaciones, sólo da a quien llama la ocasión de decir algo
        mientras tanto. Sin esto, get_statement() puede estar varios
        MINUTOS sin mandar nada — visto en la práctica: eso puede bastar
        para que un proxy intermedio (Streamlit Cloud está detrás de uno)
        dé por muerta la conexión del navegador por inactividad aunque el
        proceso en el servidor siga vivo y termine bien — el usuario ve la
        pantalla congelada y solo al recargar aparecen los datos, que en
        realidad ya se habían guardado a tiempo."""
        ref, url = self.request_statement(fd, td, on_progress=on_progress)
        xml = self.get_statement(url, ref, on_progress=on_progress)

        os.makedirs(self.raw_dir, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = f"{fd}_{td}" if fd else "default"
        digest = hashlib.sha256(xml.encode()).hexdigest()[:12]
        path = os.path.join(self.raw_dir, f"{self.query_id}_{tag}_{stamp}_{digest}.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
        log.info("Guardado %s (%.1f KB)", path, len(xml) / 1024)
        return path


# --------------------------------------------------------------------------
# Planificación de bloques
# --------------------------------------------------------------------------

def _prev_weekday(d: dt.datetime) -> dt.datetime:
    """Retrocede al día laborable anterior (evita sábado y domingo)."""
    while d.weekday() >= 5:          # 5 = sábado, 6 = domingo
        d -= dt.timedelta(days=1)
    return d


def plan_windows(start: str, end: str, lead_days: int = 5,
                 max_days: int = MAX_WINDOW_DAYS) -> list[tuple[str, str]]:
    """Trocea [start, end] en ventanas de <= max_days.

    `lead_days` adelanta el inicio del PRIMER bloque para que el día de
    arranque traiga su snapshot completo de posiciones y su ConversionRate.
    Sin ese margen aparece el problema de bootstrap de §11.8: el primer día
    tiene NAV pero no composición por divisa, y la estrategia queda con ~21 pb
    de incertidumbre.

    El `td` de cada bloque se lleva al día laborable anterior si cae en fin de
    semana. Verificado contra IBKR real: pedir un bloque cuyo `td` es un día
    sin cierre (p. ej. 31-dic-2022 en sábado, 31-dic-2023 en domingo) devuelve
    1003 para TODO el bloque. Alinear el corte a un día hábil evita el error
    sin ninguna petición fallida. Los festivos entre semana los cubre el
    retroceso acotado del backfill. El corte cae sobre un fin de semana sin
    cierres, así que no se pierde ningún día con datos.
    """
    d0 = dt.datetime.strptime(start, "%Y%m%d") - dt.timedelta(days=lead_days)
    d1 = dt.datetime.strptime(end, "%Y%m%d")
    out = []
    cur = d0
    while cur <= d1:
        boundary = min(cur + dt.timedelta(days=max_days - 1), d1)
        td = _prev_weekday(boundary)
        out.append((cur.strftime("%Y%m%d"), td.strftime("%Y%m%d")))
        cur = boundary + dt.timedelta(days=1)   # el siguiente bloque sigue tras el corte original
    return out


# CorporateAction/OptionEAE/SecurityInfo promovidas a obligatorias (antes
# solo recomendadas, §20.3): sin CorporateAction los splits no reconcilian
# (hasta 15 % del volumen, ver README), y las otras dos respaldan
# subyacente/multiplicador y ejercicios de opciones — a petición expresa,
# ya no basta con tenerlas "recomendadas" sin comprobar.
REQUIRED_SECTIONS = [
    "StmtFunds", "ChangeInNAV", "ChangeInPositionValues", "CashReport",
    "EquitySummaryInBase", "OpenPositions", "FxPositions",
    "PriorPeriodPositions", "Trades", "TradeTransfers", "FxTransactions",
    "CashTransactions", "Transfers", "ConversionRates",
    "CorporateAction", "OptionEAE", "SecurityInfo",
]


def validate_query(xml_text: str) -> dict:
    """Comprueba que la Flex Query del usuario trae todo lo que el motor necesita.

    Se ejecuta una sola vez en el onboarding, sobre un bloque corto. Las
    etiquetas XML no dependen del idioma de la interfaz, así que esto funciona
    aunque el usuario tenga el Client Portal en español.
    """
    root = ET.fromstring(xml_text)
    present = {e.tag for e in root.iter()}
    missing = [s for s in REQUIRED_SECTIONS if s not in present]

    accounts = sorted({e.get("accountId") for e in root.iter("FlexStatement")
                       if e.get("accountId")})
    dates = sorted({e.get("reportDate")
                    for e in root.iter("EquitySummaryByReportDateInBase")})
    daily = len({e.get("reportDate") for e in root.iter("OpenPosition")}) > 1

    return dict(
        ok=not missing and daily,
        missing_sections=missing,
        accounts=accounts,
        date_range=(dates[0], dates[-1]) if dates else None,
        daily_positions=daily,
        note=("Faltan secciones: " + ", ".join(missing)) if missing else
             ("" if daily else
              "La query no devuelve posiciones diarias: revisa que el desglose "
              "sea por día y no un único snapshot de cierre"),
    )


# --------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------

def backfill(client: FlexClient, start: str, end: str,
             lead_days: int = 5, on_progress=None) -> list[str]:
    """Descarga el histórico completo troceado. Devuelve las rutas de los crudos."""
    windows = plan_windows(start, end, lead_days)
    paths = []
    for i, (fd, td) in enumerate(windows, 1):
        if on_progress:
            on_progress(i, len(windows), fd, td)
        paths.append(client.fetch(fd, td))
    return paths


def incremental(client: FlexClient, last_date: str, overlap_days: int = 10) -> str | None:
    """Sincronización diaria. Solapa hacia atrás para capturar correcciones.

    IBKR revisa statements ya emitidos (dividendos reclasificados, comisiones
    ajustadas). Sin solape, esas correcciones no llegan nunca. Con solape, el
    normalizador debe deduplicar por transactionID y detectar conflictos de NAV
    en las fechas repetidas.

    El `td` se fija al ÚLTIMO CIERRE DISPONIBLE, no a hoy: pedir hasta hoy
    cuando el cierre aún no está generado devuelve 1003 para todo el bloque
    (verificado contra IBKR real). Devuelve None si no hay nada nuevo.

    Si el hueco (por una parada larga) supera el máximo de un bloque, un solo
    fetch no puede cubrirlo: lanza ValueError para que el caller haga backfill.
    """
    latest = client.latest_available_date()
    if not latest or latest <= last_date:
        return None
    fd = (dt.datetime.strptime(last_date, "%Y%m%d")
          - dt.timedelta(days=overlap_days)).strftime("%Y%m%d")
    if (dt.datetime.strptime(latest, "%Y%m%d")
            - dt.datetime.strptime(fd, "%Y%m%d")).days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"Hueco {fd}→{latest} > {MAX_WINDOW_DAYS} días: un incremental no basta, "
            "hace falta un backfill multi-bloque")
    return client.fetch(fd, latest)
