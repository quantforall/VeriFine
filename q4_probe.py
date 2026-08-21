"""VeriFine — instrumentación ligera para medir hidratación, primer cálculo,
errores de Drive y latencia de servicios externos bajo carga real (Fase 0 del
plan de escalabilidad a 300 usuarios).

Sin dependencias nuevas ni backend de métricas: cada evento se registra como
una línea de log estructurada — Streamlit Cloud ya captura stdout/stderr, así
que basta con `grep "metric event="` sobre esos logs para sacar p50/p95/p99
por evento. `resource.getrusage()` da el pico de RSS del proceso sin
necesitar `psutil`. Esto NO sustituye un load-test real (Fase 5 del plan) —
sólo da datos de producción mientras tanto, para no dimensionar a ciegas
(Fase 2/5/6 dependen de estos números)."""

from __future__ import annotations

import time
import logging
import resource
import threading
from contextlib import contextmanager

log = logging.getLogger("q4.probe")


def peak_rss_mb() -> float:
    """Pico de RSS del proceso hasta ahora, en MB.

    `resource.getrusage().ru_maxrss` viene en KB en Linux pero en BYTES en
    macOS — normalizar aquí evita un número 1000x desproporcionado según
    dónde corra (Streamlit Cloud es Linux; desarrollo local es Mac). Un
    proceso real de VeriFine no baja de unos pocos MB, así que el umbral de
    10**7 separa ambos casos sin ambigüedad (10 MB en KB ya son 10**10)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if raw > 10**7 else raw / 1024


@contextmanager
def timed(event: str, **fields):
    """Mide un bloque y loguea `event=<event> ms=<duración> rss_mb=<pico>
    campo=valor...` al terminar — éxito o excepción (el tiempo hasta el
    fallo también es un dato de capacidad). Uso:

        with timed("hydrate_session", session_id=sid):
            ...

    Deliberadamente NO envuelve nada en try/except propio: una excepción
    dentro del bloque se loguea (con su tiempo hasta el fallo) y luego sigue
    propagándose tal cual — este helper nunca debe cambiar el comportamiento
    de lo que mide, sólo observarlo."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000
        extra = "".join(f" {k}={v}" for k, v in fields.items())
        log.info("metric event=%s ms=%.0f rss_mb=%.1f%s", event, ms, peak_rss_mb(), extra)


class Counter:
    """Contador simple, seguro entre hilos, para eventos que interesa saber
    CUÁNTOS pasan en la vida del proceso (p. ej. 429/403 de Drive) sin
    loguear una línea por cada uno bajo carga alta — sólo cada `log_every`
    (y la primera vez, para no esperar `log_every` sucesos a ver que algo
    pasa)."""

    def __init__(self, name: str, log_every: int = 20):
        self.name = name
        self.log_every = log_every
        self._n = 0
        self._lock = threading.Lock()

    def hit(self, **fields) -> None:
        with self._lock:
            self._n += 1
            n = self._n
        if n == 1 or n % self.log_every == 0:
            extra = "".join(f" {k}={v}" for k, v in fields.items())
            log.warning("metric event=%s count=%d%s", self.name, n, extra)
