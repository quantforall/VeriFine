"""
Quant4all — Operaciones: P&L por ticker (§20 de la spec).

Distinto propósito que q4_engine (TWR/atribución): aquí la unidad es divisa,
no porcentaje, y la fuente de verdad es `fifoPnlRealized` de IBKR, nunca un
importe recalculado por nosotros (§20.2). El FIFO propio (con hora real,
§20.2) sirve sólo para decidir qué lote de apertura corresponde a cada
cierre — nunca para el importe.

Dos niveles de salida:
    Operation   — una fila por operación (lote de apertura × cierre que lo
                  consume, total o parcialmente). Lleva `account`.
    TickerAgg   — una fila por ticker (o subyacente, en FUT/OPT), sumando
                  todas las Operation que aplican. Sin `account`.
"""

from __future__ import annotations

import bisect
import itertools
from dataclasses import dataclass, field

import pandas as pd

import q4_parser as P
import q4_engine as E

DIVIDEND_TYPES = {"Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"}
# §20.4 — activos que participan del motor de Operaciones. CASH son
# conversiones de divisa, no posiciones que se compran/venden — fuera de
# alcance aquí.
TRADED_CATEGORIES = {"STK", "OPT", "FUT"}


@dataclass
class _Lot:
    """Lote de apertura vivo durante el barrido FIFO. `qty` con signo:
    positivo = largo, negativo = corto. Se consume en trozos a medida que
    llegan cierres; lo que queda al final del barrido es la parte abierta."""
    qty: float
    price: float | None          # None sólo si entry_source = no_determinado
    orig_qty: float               # tamaño original, para repartir su comisión
    entry_date: str | None
    entry_datetime: str
    entry_commission: float       # comisión TOTAL de la operación de apertura
    entry_source: str             # "trade" | "transfer" | "no_determinado"
    entry_symbol: str             # símbolo de contrato tal cual (opción/futuro)
    currency: str
    multiplier: float


@dataclass
class Operation:
    account: str
    symbol: str                   # grupo (subyacente si FUT/OPT, si no símbolo)
    kind: str                     # "acción" | "opciones" | "futuros" — §20.4
    contract_symbol: str          # símbolo de contrato real (entrada)
    direction: str                 # "largo" | "corto"
    status: str                    # "abierta" | "cerrada"
    entry_date: str | None
    entry_price: float | None
    entry_commission_local: float
    entry_source: str
    exit_date: str | None
    exit_price: float | None
    exit_commission_local: float
    quantity: float                 # cantidad emparejada de este lote (positiva)
    multiplier: float
    gain_local: float               # §20.8 — importe atribuido al PERIODO
    pct_return: float | None
    currency: str


@dataclass
class TickerAgg:
    symbol: str
    kind: str                     # "acción" | "opciones" | "futuros" — §20.4
    revalorizacion_local: float
    dividendos_local: float
    comisiones_local: float
    total_local: float
    capital_base_local: float
    pct_return: float | None
    currency: str
    operations: list[Operation] = field(default_factory=list)


# --------------------------------------------------------------------------
# Precio de referencia en una fecha (arrastre del último valor conocido —
# mismo criterio que la matriz FX de §7).
# --------------------------------------------------------------------------

class _PriceLookup:
    def __init__(self, ds: P.Dataset):
        self._by_key: dict[tuple[str, str], tuple[list[str], list[float]]] = {}
        if ds.positions.empty:
            return
        # Orden por (cuenta, conid, fecha) UNA VEZ para toda la tabla, no un
        # sort_values("date") por cada uno de los cientos de grupos (cuenta,
        # conid) — mismo motivo y mismo patrón que el orden de build() en
        # este mismo fichero (el coste fijo de sort_values() por llamada
        # domina con muchos grupos pequeños). "date" no tiene empates dentro
        # de un mismo (cuenta, conid) — load() ya deduplica positions por
        # (account, date, conid) — así que no hace falta kind="stable" aquí
        # para la corrección, solo por consistencia/barato.
        positions = ds.positions.sort_values(["account", "conid", "date"], kind="stable")
        for (acct, conid), sub in positions.groupby(["account", "conid"]):
            self._by_key[(acct, conid)] = (sub["date"].tolist(), sub["price"].tolist())

    def price_on_or_before(self, account: str, conid: str, date: str) -> float | None:
        dates, prices = self._by_key.get((account, conid), ([], []))
        i = bisect.bisect_right(dates, date)
        return prices[i - 1] if i else None


# --------------------------------------------------------------------------
# FIFO por (cuenta, símbolo de grupo) — reconstrucción de por vida, no
# recortada al periodo (§20.8: el periodo se aplica DESPUÉS, sobre el
# resultado, no dentro del emparejamiento).
# --------------------------------------------------------------------------

_KIND_BY_CATEGORY = {"STK": "acción", "OPT": "opciones", "FUT": "futuros"}


def _group_key(row) -> tuple[str, str]:
    """(símbolo de grupo, tipo). El tipo entra en la clave a propósito: el
    mismo símbolo puede operarse como acción Y como subyacente de opciones o
    futuros (IWM, TLT en datos reales) — sin el tipo en la clave se
    emparejarían en el mismo FIFO, que no tiene sentido entre instrumentos
    distintos, y se confundirían en el agregado (pedido explícito: separar).

    `row` es una namedtuple de `.itertuples()` (no un `pandas.Series`) —
    acceso por atributo, `getattr(..., None)` en vez de `.get()` (los
    namedtuples no lo tienen)."""
    cat = row.asset_category
    kind = _KIND_BY_CATEGORY.get(cat, cat)
    if cat in ("FUT", "OPT") and getattr(row, "underlying_symbol", None):
        return row.underlying_symbol, kind
    return row.symbol, kind


def _splits_index(ds: P.Dataset) -> dict[tuple[str, str], list[tuple[str, int, int]]]:
    """(cuenta, símbolo) -> splits ordenados (datetime, ratio_num, ratio_den).

    Una sola pasada sobre `corporate_actions` para TODA la cuenta, no una
    por grupo: `build()` llama a `_fifo_operations()` una vez por (cuenta,
    símbolo, tipo) — con una cuenta activa de varios años son cientos de
    grupos — y antes cada uno filtraba la tabla ENTERA de corporate_actions
    por su cuenta a mano (perfilado sobre datos reales: ~85 ms de los
    ~600 ms totales de `build()`, sólo en esto)."""
    ca = ds.corporate_actions
    if ca.empty:
        return {}
    out: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    for (account, symbol), sub in ca.groupby(["account", "symbol"]):
        out[(account, symbol)] = sorted(zip(sub["datetime"], sub["ratio_num"], sub["ratio_den"]))
    return out


def _transfers_index(ds: P.Dataset) -> dict[tuple[str, str], list[tuple]]:
    """(cuenta, conid) -> Transfer ordenadas por fecha, cada una
    (fecha, quantity, position_amount_base, currency, symbol).

    Mismo motivo que `_splits_index()`: antes `_transfer_lot()` filtraba
    TODA la tabla de movimientos —incluido un `.astype(str).str.startswith()`
    nada barato— una vez por cada grupo del FIFO que necesitara este
    respaldo (perfilado: ~98 ms de los ~600 ms totales)."""
    mov = ds.movements
    if mov.empty:
        return {}
    transfers = mov[mov["type"].astype(str).str.startswith("TRANSFER_")]
    if transfers.empty:
        return {}
    out: dict[tuple[str, str], list[tuple]] = {}
    for (account, conid), sub in transfers.groupby(["account", "conid"]):
        # kind="stable": puede haber más de una Transfer el mismo día para
        # el mismo (cuenta, conid) — verificado en datos reales (5 de 30
        # grupos) — y _transfer_lot() toma "la última" tras ordenar; sin
        # orden estable, cuál es "la última" en un empate no está definido
        # (mismo motivo que el `kind="stable"` de build(), ver su comentario).
        sub = sub.sort_values("date", kind="stable")
        out[(account, conid)] = list(zip(sub["date"], sub["quantity"],
                                        sub["position_amount_base"], sub["currency"],
                                        sub["symbol"]))
    return out


def _transfer_lot(transfers_idx: dict, account: str, conid: str,
                  before_date: str) -> _Lot | None:
    """§20.5 — origen de una posición sin Trade de apertura: la Transfer más
    reciente de ese conid/cuenta anterior al cierre. El precio de entrada
    implícito sale de positionAmountInBase / quantity (ya en divisa base;
    se usa tal cual, es la mejor referencia disponible sin Trade).

    `transfers_idx` ya viene de `_transfers_index()` — la lista (pequeña)
    de ESE (cuenta, conid), no la tabla entera de movimientos."""
    if not conid:
        return None
    candidates = transfers_idx.get((account, conid))
    if not candidates:
        return None
    eligible = [c for c in candidates if c[0] <= before_date]   # ya vienen ordenadas por fecha
    if not eligible:
        return None
    date, qty, pos_amount_base, currency, symbol = eligible[-1]
    qty = float(qty)
    if qty == 0:
        return None
    price = abs(float(pos_amount_base) / qty)
    return _Lot(qty=qty, price=price, orig_qty=abs(qty), entry_date=date,
               entry_datetime=date, entry_commission=0.0, entry_source="transfer",
               entry_symbol=symbol or "", currency=currency or "?",
               multiplier=1.0)


def _no_determinado_lot(qty_needed: float, exit_price: float, currency: str,
                        multiplier: float) -> _Lot:
    """§20.5 — ni Trade ni Transfer explican el origen. No se inventa un
    precio/fecha de entrada: se deja constancia (entry_price=None) y el
    importe sigue siendo el fifoPnlRealized de IBKR, repartido igual que
    cualquier otro lote (§20.7) — lo único que falta es la trazabilidad."""
    return _Lot(qty=qty_needed, price=None, orig_qty=abs(qty_needed), entry_date=None,
               entry_datetime="00000000;000000", entry_commission=0.0,
               entry_source="no_determinado", entry_symbol="", currency=currency,
               multiplier=multiplier)


def _fifo_operations(splits_idx: dict, transfers_idx: dict, rows: list,
                     account: str, group_symbol: str, kind: str) -> list[dict]:
    """Barrido FIFO de por vida. Devuelve filas crudas (una por lote×cierre
    emparejado o lote que queda abierto), SIN recortar todavía al periodo.

    `rows` ya es una LISTA de namedtuples (de `.itertuples()`), no un
    DataFrame — `build()` hace una sola `itertuples()` sobre la tabla
    ENTERA, ya ordenada por (cuenta, símbolo, tipo, datetime), y separa los
    grupos con `itertools.groupby()` en Python puro. Antes se llamaba a
    `.itertuples()` una vez POR GRUPO (cientos, con una cuenta activa de
    varios años) — cada llamada tiene un coste de montaje fijo
    (perfilado: ese montaje, no el recorrido de filas en sí, dominaba con
    muchos grupos pequeños). Con una sola llamada para toda la tabla, ese
    coste se paga una vez, no cientos.

    Sin `.get()` en el bucle (los namedtuples no lo tienen) — todas las
    columnas de `trades` existen siempre (ver `parse_file()`), así que el
    acceso por atributo es seguro."""
    # los splits sólo tienen sentido sobre la acción misma (§20.6): un ajuste
    # de cantidad/precio por ratio no es correcto para un contrato de
    # opción/futuro sobre ese subyacente (ahí IBKR ajusta strike/multiplicador
    # de otra forma, que no se modela aquí).
    splits = splits_idx.get((account, group_symbol), []) if kind == "acción" else []
    lots: list[_Lot] = []
    split_idx = 0
    out: list[dict] = []

    for t in rows:
        while split_idx < len(splits) and splits[split_idx][0] < t.datetime:
            _, num, den = splits[split_idx]
            for lot in lots:
                lot.qty *= num / den
                if lot.price is not None:
                    lot.price *= den / num
                lot.orig_qty *= num / den
            split_idx += 1

        qty = t.quantity
        remaining = qty
        trade_total_qty = abs(qty) or 1.0   # evita división por cero si qty=0
        matches: list[tuple[_Lot, float]] = []
        oc = t.open_close or ""

        while remaining != 0 and lots and (lots[0].qty > 0) != (remaining > 0):
            lot = lots[0]
            take = min(abs(remaining), abs(lot.qty))
            matches.append((lot, take))
            lot.qty -= take if lot.qty > 0 else -take
            remaining -= take if remaining > 0 else -take
            if abs(lot.qty) < 1e-9:
                lots.pop(0)

        # queda un resto de CIERRE sin lote que lo explique -> Transfer, y si
        # tampoco hay, no_determinado (§20.5). Un solo intento de cada; no se
        # itera indefinidamente si el resto sigue sin cubrirse del todo.
        if remaining != 0 and "C" in oc:
            seed = _transfer_lot(transfers_idx, account, t.conid, t.date)
            if seed is None or (seed.qty > 0) == (remaining > 0):
                seed = _no_determinado_lot(-remaining, t.trade_price,
                                           t.currency, t.multiplier)
            take = min(abs(remaining), abs(seed.qty))
            matches.append((seed, take))
            seed.qty -= take if seed.qty > 0 else -take
            remaining -= take if remaining > 0 else -take
            if abs(seed.qty) > 1e-9:
                lots.insert(0, seed)   # sobra del seed: queda como lote vivo
            if remaining != 0:
                # aún falta más de lo que Transfer/no_determinado pudo cubrir
                extra = _no_determinado_lot(-remaining, t.trade_price,
                                            t.currency, t.multiplier)
                matches.append((extra, abs(remaining)))
                remaining = 0

        if remaining != 0:
            # "O" puro (o resto de C;O tras cerrar del todo): abre lote nuevo.
            lots.append(_Lot(
                qty=remaining, price=t.trade_price, orig_qty=abs(remaining),
                entry_date=t.date, entry_datetime=t.datetime,
                entry_commission=float(t.commission_local), entry_source="trade",
                entry_symbol=t.symbol, currency=t.currency,
                multiplier=t.multiplier))

        fifo_total = float(t.fifo_pnl_realized_local)
        comm_total = float(t.commission_local)
        for lot, take in matches:
            frac = take / trade_total_qty
            was_long = t.quantity < 0   # cierre por venta => el lote era largo
            entry_comm = (lot.entry_commission * (take / lot.orig_qty)
                         if lot.orig_qty else 0.0)
            out.append(dict(
                account=account, symbol=group_symbol, kind=kind,
                contract_symbol=lot.entry_symbol,
                direction="largo" if was_long else "corto",
                entry_date=lot.entry_date, entry_price=lot.price,
                entry_commission_local=entry_comm, entry_source=lot.entry_source,
                exit_date=t.date, exit_price=t.trade_price,
                exit_commission_local=comm_total * frac,
                quantity=take, multiplier=t.multiplier,
                fifo_gain_local=fifo_total * frac, currency=t.currency,
                status="cerrada"))

    for lot in lots:
        out.append(dict(
            account=account, symbol=group_symbol, kind=kind,
            contract_symbol=lot.entry_symbol,
            direction="largo" if lot.qty > 0 else "corto",
            entry_date=lot.entry_date, entry_price=lot.price,
            entry_commission_local=lot.entry_commission, entry_source=lot.entry_source,
            exit_date=None, exit_price=None, exit_commission_local=0.0,
            quantity=abs(lot.qty), multiplier=lot.multiplier,
            fifo_gain_local=None, currency=lot.currency, status="abierta"))
    return out


# --------------------------------------------------------------------------
# Alcance del periodo (§20.8) — dos referencias, no una.
# --------------------------------------------------------------------------

def _period_scope(row: dict, start: str, end: str, prices: _PriceLookup,
                  account: str, conid_by_symbol: dict) -> dict | None:
    """Devuelve la Operation (como dict) con el importe acotado al periodo,
    o None si la operación no tiene actividad dentro de [start, end]."""
    entry_d = row["entry_date"]
    exit_d = row["exit_date"]
    holding_end = exit_d or end   # abierta: "hasta ahora" para el solape
    if (entry_d is not None and entry_d > end) or holding_end < start:
        return None

    conid = conid_by_symbol.get(row["contract_symbol"])
    qty, mult = row["quantity"], row["multiplier"]
    sign = 1 if row["direction"] == "largo" else -1

    # sin entrada real conocida (§20.5): no se puede recortar con precisión,
    # así que se confía en fifoPnlRealized completo si la salida cae en el
    # periodo, en vez de fabricar un precio de referencia que no existe.
    clean = (row["entry_source"] == "no_determinado" or
            (entry_d is not None and entry_d >= start and
             exit_d is not None and exit_d <= end))

    if clean:
        gain = row["fifo_gain_local"] if row["fifo_gain_local"] is not None else 0.0
        ref_entry_price = row["entry_price"]
        ref_exit_price = row["exit_price"] if row["exit_price"] is not None else ref_entry_price
    else:
        if entry_d is not None and entry_d < start:
            ref_entry_price = (prices.price_on_or_before(account, conid, start)
                               if conid else None) or row["entry_price"]
        else:
            ref_entry_price = row["entry_price"]
        if exit_d is not None and exit_d <= end:
            ref_exit_price = row["exit_price"]
        else:
            ref_exit_price = (prices.price_on_or_before(account, conid, end)
                              if conid else None) or ref_entry_price
        gain = (sign * qty * mult * (ref_exit_price - ref_entry_price)
               if ref_entry_price is not None and ref_exit_price is not None else 0.0)

    pct = (sign * (ref_exit_price - ref_entry_price) / ref_entry_price
          if ref_entry_price else None)
    capital_base = qty * mult * ref_entry_price if ref_entry_price is not None else 0.0

    entry_comm = row["entry_commission_local"] if (entry_d is None or entry_d >= start) else 0.0
    exit_comm = (row["exit_commission_local"]
                if exit_d is not None and exit_d <= end else 0.0)

    return dict(
        account=row["account"], symbol=row["symbol"], kind=row["kind"],
        contract_symbol=row["contract_symbol"],
        direction=row["direction"], status=row["status"],
        entry_date=entry_d, entry_price=row["entry_price"],
        entry_commission_local=entry_comm, entry_source=row["entry_source"],
        exit_date=exit_d, exit_price=row["exit_price"], exit_commission_local=exit_comm,
        quantity=qty, multiplier=mult, gain_local=gain, pct_return=pct,
        currency=row["currency"], capital_base=capital_base)


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------

def build(ds: P.Dataset, start: str, end: str,
         accounts: list[str] | None = None) -> tuple[list[Operation], list[TickerAgg]]:
    """§20 — devuelve (detalle por operación, agregado por ticker)."""
    if ds.trades.empty:
        return [], []

    trades = ds.trades[ds.trades["asset_category"].isin(TRADED_CATEGORIES)].copy()
    if accounts is not None:
        trades = trades[trades["account"].isin(accounts)]
    if trades.empty:
        return [], []

    # dos columnas, no una tupla: evita la expansión ambigua de pandas al
    # asignar el resultado de apply() cuando la función devuelve una tupla.
    group_symbols, group_kinds = [], []
    for r in trades.itertuples(index=False):
        s, k = _group_key(r)
        group_symbols.append(s)
        group_kinds.append(k)
    trades["group_symbol"] = group_symbols
    trades["group_kind"] = group_kinds
    # Orden por (cuenta, símbolo de grupo, tipo, datetime) UNA VEZ para TODA
    # la tabla — no un groupby() de pandas + una itertuples() por grupo
    # (cientos, con una cuenta activa de varios años: perfilado sobre datos
    # reales, el montaje de itertuples() en sí —no el recorrido de filas—
    # dominaba con tantos grupos pequeños). Con las tres claves de grupo
    # como columnas primarias del orden, las filas de cada grupo quedan
    # CONTIGUAS, así que basta con una sola itertuples() sobre la tabla
    # entera + itertools.groupby() (Python puro, sin coste de montaje) para
    # separarlas — ver el bucle de abajo.
    #
    # kind="stable" — NO es cosmético. 161 de 389 grupos reales (cuenta,
    # símbolo, tipo) tienen dos o más operaciones con el MISMO datetime
    # exacto (ejecuciones parciales al mismo segundo). El "quicksort" por
    # defecto de pandas NO es estable, y desempata esos casos de forma
    # distinta según se ordene la tabla entera o cada grupo pequeño por
    # separado — verificado contra datos reales: sin `kind="stable"` esto
    # cambiaba qué lote se empareja con qué cierre en justo esos 161
    # grupos (2.577 de 2.877 operaciones de detalle, en la cuenta real de
    # prueba). Con `kind="stable"` los empates conservan el orden en que
    # vienen en el extracto de IBKR — el único desempate defendible
    # cuando de verdad no hay más precisión temporal que ese segundo, Y da
    # el mismo resultado tanto si se ordena por grupo como si se ordena
    # (cuenta, símbolo, tipo, datetime) de una tacada como aquí (verificado).
    trades = trades.sort_values(
        ["account", "group_symbol", "group_kind", "datetime"], kind="stable")
    prices = _PriceLookup(ds)
    conid_by_symbol = dict(zip(trades["symbol"], trades["conid"]))
    # Índices de splits/transfers UNA VEZ para toda la cuenta, no uno por
    # grupo (cientos de grupos con una cuenta activa de varios años) — ver
    # _splits_index()/_transfers_index().
    splits_idx = _splits_index(ds)
    transfers_idx = _transfers_index(ds)

    detail: list[Operation] = []
    aggs: dict[tuple[str, str], TickerAgg] = {}

    # Una sola itertuples() para TODA la tabla (ya ordenada arriba), y
    # itertools.groupby() para separar los grupos por su clave — en vez de
    # trades.groupby(...) de pandas, que montaría una itertuples() nueva
    # por cada grupo dentro de _fifo_operations(). itertools.groupby() sólo
    # agrupa TRAMOS CONTIGUOS con la misma clave — por eso el orden de
    # arriba pone (cuenta, símbolo, tipo) como claves primarias.
    all_rows = trades.itertuples(index=False)
    for (account, group_symbol, kind), group_rows in itertools.groupby(
            all_rows, key=lambda r: (r.account, r.group_symbol, r.group_kind)):
        raw_ops = _fifo_operations(splits_idx, transfers_idx, list(group_rows),
                                   account, group_symbol, kind)
        for row in raw_ops:
            scoped = _period_scope(row, start, end, prices, account, conid_by_symbol)
            if scoped is None:
                continue
            op = Operation(
                account=scoped["account"], symbol=scoped["symbol"], kind=scoped["kind"],
                contract_symbol=scoped["contract_symbol"], direction=scoped["direction"],
                status=scoped["status"], entry_date=scoped["entry_date"],
                entry_price=scoped["entry_price"],
                entry_commission_local=scoped["entry_commission_local"],
                entry_source=scoped["entry_source"], exit_date=scoped["exit_date"],
                exit_price=scoped["exit_price"],
                exit_commission_local=scoped["exit_commission_local"],
                quantity=scoped["quantity"], multiplier=scoped["multiplier"],
                gain_local=scoped["gain_local"], pct_return=scoped["pct_return"],
                currency=scoped["currency"])
            detail.append(op)

            key = (group_symbol, kind)
            agg = aggs.setdefault(key, TickerAgg(
                symbol=group_symbol, kind=kind, revalorizacion_local=0.0,
                dividendos_local=0.0, comisiones_local=0.0, total_local=0.0,
                capital_base_local=0.0, pct_return=None, currency=op.currency))
            agg.revalorizacion_local += op.gain_local
            agg.comisiones_local += op.entry_commission_local + op.exit_commission_local
            agg.capital_base_local += scoped["capital_base"]
            agg.operations.append(op)

    # dividendos por ticker (§20.9) — netos de Withholding Tax por el signo.
    # Sólo pueden caer en un grupo "acción": ni opciones ni futuros reparten
    # dividendo propio (el que cobra la posición subyacente es otro conid).
    if not ds.movements.empty and aggs:
        div = ds.movements[ds.movements["type"].isin(DIVIDEND_TYPES) &
                           (start <= ds.movements["date"]) & (ds.movements["date"] <= end)]
        if accounts is not None:
            div = div[div["account"].isin(accounts)]
        symbol_by_conid = dict(zip(trades["conid"],
                                   zip(trades["group_symbol"], trades["group_kind"])))
        for r in div.itertuples(index=False):
            key = symbol_by_conid.get(r.conid, (r.symbol, "acción"))
            if not key[0] or key not in aggs:
                continue   # dividendo de un ticker sin operaciones en el periodo: fuera de alcance
            aggs[key].dividendos_local += float(r.amount_local)

    for agg in aggs.values():
        # comisiones_local ya viene con signo negativo (coste) desde IBKR
        # (ibCommission), igual que en el resto del proyecto — se SUMA, no se
        # resta, o el coste se contaría dos veces con el signo volteado.
        agg.total_local = agg.revalorizacion_local + agg.dividendos_local + agg.comisiones_local
        agg.pct_return = (agg.revalorizacion_local / agg.capital_base_local
                          if agg.capital_base_local else None)

    return detail, sorted(aggs.values(), key=lambda a: -abs(a.total_local))


# --------------------------------------------------------------------------
# §21 — CARTERA: posiciones abiertas + efectivo, a la última fecha
# disponible de cada cuenta. A diferencia de Operaciones (§20), el importe
# de la plusvalía latente sale de costBasisPrice/fifoPnlUnrealized de IBKR
# (OpenPosition) directamente — no de nuestro FIFO. Es la contabilidad que
# ya lleva IBKR internamente, incluye sus propios ajustes de splits y
# transferencias, y evita heredar aquí los casos "no_determinado" de §20.5.
# --------------------------------------------------------------------------

# Categorías cuyo `value_local` es NOCIONAL, no capital inmovilizado — igual
# criterio que NON_NAV_CATEGORIES en q4_parser, extendido a opciones por
# decisión explícita: aparecen en la tabla con su P&L, pero no en el pastel
# de % sobre el patrimonio ni en el total de equity que lo acompaña.
PIE_EXCLUDED_KINDS = {"futuros", "opciones"}


@dataclass
class PositionRow:
    account: str
    symbol: str
    description: str
    kind: str                       # "acción" | "opciones" | "futuros"
    currency: str
    direction: str                   # "largo" | "corto"
    quantity: float
    entry_price: float | None        # costBasisPrice (IBKR), o respaldo FIFO si falta
    entry_date: str | None           # mejor esfuerzo: lote abierto más antiguo (§20 FIFO)
    current_price: float | None
    # fifoPnlUnrealized (IBKR), o recalculado con el precio de entrada si
    # falta (sólo acciones — ver §21.1). None si de verdad no hay dato: NO
    # se enmascara con 0.0, eso mostraría una plusvalía falsa de "ni gano
    # ni pierdo" cuando lo honesto es decir que no se sabe.
    unrealized_gain_local: float | None
    pct_return: float | None
    value_local: float               # positionValue (IBKR)
    in_equity_weight: bool           # False para futuros/opciones (ver arriba)
    value_analysis_ccy: float = 0.0  # value_local convertido a la divisa de análisis
    # % que esta posición representa sobre equity_total_analysis_ccy (mismo
    # denominador que el pastel de más abajo) — None sólo si equity_total es
    # 0 (cartera vacía). Se rellena en una segunda pasada, al final de
    # portfolio(): equity_total no se conoce del todo hasta sumar también el
    # efectivo, que se procesa DESPUÉS del bucle de posiciones.
    pct_weight: float | None = None


@dataclass
class CashRow:
    account: str
    currency: str
    balance_local: float
    value_analysis_ccy: float = 0.0  # balance_local convertido a la divisa de análisis
    pct_weight: float | None = None  # mismo denominador/segunda pasada que PositionRow


@dataclass
class PieSlice:
    label: str
    value_analysis_ccy: float
    pct: float


@dataclass
class PortfolioSnapshot:
    as_of: str
    positions: list[PositionRow]
    cash: list[CashRow]
    pie: list[PieSlice]
    equity_total_analysis_ccy: float     # acciones + efectivo, SIN futuros/opciones
    exposure_total_analysis_ccy: float   # equity + |nocional| de futuros/opciones, largo o corto
    currency: str


def portfolio(ds: P.Dataset, accounts: list[str] | None = None,
             analysis_currency: str | None = None, pie_top_n: int = 8,
             weight_basis: str = "patrimonio") -> PortfolioSnapshot:
    """weight_basis controla el pastel y el "% Peso" de la tabla de
    Posiciones (a petición expresa — dos formas de ver la misma cartera):

        "patrimonio"  (por defecto) futuros/opciones fuera del pastel y del
                      peso, igual que siempre — equity_total_analysis_ccy
                      como denominador.
        "exposicion"  futuros/opciones SÍ entran en el pastel, en valor
                      ABSOLUTO (mismo criterio que ya usa exposure_total:
                      un corto expone al mercado igual que un largo, no se
                      compensan) — exposure_total_analysis_ccy como
                      denominador.

    equity_total/exposure_total/notional_total (las 3 cajitas del KPI) NO
    cambian con esto — son hechos objetivos de la cartera, no una forma de
    mirarla; solo cambia qué entra en el pastel y sobre qué base se pesa.
    """
    # top 8 + "Otros" = máximo 9 porciones (a petición expresa: con 5+Otros
    # el bloque "Otros" se comía demasiado del pastel). 9 no es arbitrario:
    # coincide con el número de colores de PIE_COLORS en app.py — subir esto
    # más exige ampliar esa paleta antes, o dos porciones acabarían
    # compartiendo color.
    m = analysis_currency or ds.base_currency
    fx = E.FX(P.fx_matrix(ds), ds.base_currency)

    def to_analysis(amount_local: float, currency: str, date: str) -> float:
        return amount_local * fx.rate(currency, date) / fx.rate(m, date)

    pos = ds.positions
    if accounts is not None:
        pos = pos[pos["account"].isin(accounts)]
    if pos.empty:
        return PortfolioSnapshot(as_of="", positions=[], cash=[], pie=[],
                                 equity_total_analysis_ccy=0.0,
                                 exposure_total_analysis_ccy=0.0, currency=m)

    last_date_by_acct = pos.groupby("account")["date"].max().to_dict()
    pos = pos[pos["date"] == pos["account"].map(last_date_by_acct)]
    as_of = max(last_date_by_acct.values())

    # Entrada real, mejor esfuerzo: lote abierto más antiguo del motor de
    # Operaciones para ese (cuenta, símbolo de contrato) — mismo Trade/
    # Transfer que ya usa Operaciones, no un dato nuevo. Antes sólo se
    # guardaba la fecha "de más" (informativa) y el precio dependía
    # únicamente de costBasisPrice de IBKR; cuando esa columna venía vacía
    # en el extracto, no había ningún respaldo — de ahí el precio de
    # entrada en blanco que se reportó. Ahora fecha Y precio salen de la
    # MISMA fila (el mismo lote), y sirven de respaldo cuando falta
    # costBasisPrice, no sólo de adorno.
    detail, _ = build(ds, ds.dates[0], ds.dates[-1], accounts=accounts)
    earliest_entry: dict[tuple[str, str], str] = {}
    earliest_entry_price: dict[tuple[str, str], float] = {}
    for o in detail:
        if o.status != "abierta" or o.entry_date is None:
            continue
        key = (o.account, o.contract_symbol)
        if key not in earliest_entry or o.entry_date < earliest_entry[key]:
            earliest_entry[key] = o.entry_date
            earliest_entry_price[key] = o.entry_price

    rows: list[PositionRow] = []
    pie_pool: dict[str, float] = {}
    equity_total = 0.0
    # §21.2 ampliado: exposición bruta = equity + |nocional| de futuros/opciones,
    # largo o corto sin compensar — un corto expone al mercado igual que un
    # largo, compensarlos escondería nocional real (ver respuesta al usuario).
    notional_total = 0.0

    # .itertuples(), no .iterrows() — mismo motivo que _fifo_operations()
    # (ver su comentario). getattr(..., None) en vez de `.get()`: los
    # namedtuples no lo tienen; el resultado es idéntico porque estas
    # columnas siempre existen en `ds.positions` (parse_file() las pone
    # siempre, con None si IBKR no trae el dato) — getattr con default sólo
    # es la red de seguridad, no cambia ningún caso real.
    for r in pos.itertuples(index=False):
        if abs(r.quantity) < 1e-9:
            continue
        kind = _KIND_BY_CATEGORY.get(r.asset_category, r.asset_category)
        # pandas convierte los None de campos ausentes a NaN al montar la
        # columna numérica — pd.notna() los detecta a los dos, `is not None`
        # se cuela con NaN (NaN is not None -> True) y ensucia el cálculo.
        cb_price = getattr(r, "cost_basis_price", None)
        cb_price = cb_price if pd.notna(cb_price) else None
        unrl = getattr(r, "fifo_pnl_unrealized", None)
        unrl = unrl if pd.notna(unrl) else None
        cb_money = getattr(r, "cost_basis_money", None)
        cb_money = cb_money if pd.notna(cb_money) else None
        key = (r.account, r.symbol)

        # Respaldo cuando IBKR no trae costBasisPrice para esta fila (pasa,
        # sobre todo en acciones): el precio del lote FIFO más antiguo que
        # ya calcula Operaciones para la misma cuenta/contrato.
        if cb_price is None:
            cb_price = earliest_entry_price.get(key)

        # fifoPnlUnrealized de IBKR es la fuente de verdad cuando existe.
        # Si falta, NO se enmascara con 0,0 (eso mostraría una plusvalía
        # falsa de "sin ganancia ni pérdida" cuando en realidad no se sabe)
        # — se recalcula con el precio ya obtenido arriba (real o de
        # respaldo) sólo para acciones, donde el multiplicador es 1 y el
        # cálculo es directo; en futuros/opciones el multiplicador importa
        # y no se guarda en `positions`, así que se deja sin dato antes que
        # arriesgar un número mal escalado.
        if unrl is None and cb_price is not None and kind == "acción":
            sign = 1 if r.quantity > 0 else -1
            unrl = sign * abs(r.quantity) * (r.price - cb_price)

        if cb_money is None and cb_price is not None:
            cb_money = abs(r.quantity) * cb_price if kind == "acción" else None

        pct = unrl / abs(cb_money) if unrl is not None and cb_money else None
        in_weight = kind not in PIE_EXCLUDED_KINDS
        value_analysis = to_analysis(r.value_local, r.currency, r.date)

        # description: NaN (no `or`, se cuela — NaN es "truthy") si IBKR no la
        # trae para esa fila; cae al símbolo. IBKR la manda en MAYÚSCULAS
        # ("APPLE INC") — .title() para "Apple Inc", más legible en la tabla.
        desc = getattr(r, "description", None)
        desc = desc if pd.notna(desc) and desc else r.symbol

        rows.append(PositionRow(
            account=r.account, symbol=r.symbol,
            description=desc.title(), kind=kind,
            currency=r.currency, direction="largo" if r.quantity > 0 else "corto",
            quantity=r.quantity, entry_price=cb_price,
            entry_date=earliest_entry.get(key),
            current_price=r.price, unrealized_gain_local=unrl,
            pct_return=pct, value_local=r.value_local, in_equity_weight=in_weight,
            value_analysis_ccy=value_analysis))

        label = r.symbol
        if in_weight:
            equity_total += value_analysis
        else:
            notional_total += abs(value_analysis)

        # Qué entra en el pastel depende del modo (ver docstring). En
        # "patrimonio", igual que siempre: solo lo que ya cuenta para
        # equity_total, con su signo. En "exposición" entra TODO, y
        # futuros/opciones en valor absoluto — mismo criterio que
        # notional_total: un corto expone igual que un largo.
        if weight_basis == "exposicion":
            pool_value = value_analysis if in_weight else abs(value_analysis)
            pie_pool[label] = pie_pool.get(label, 0.0) + pool_value
        elif in_weight:
            pie_pool[label] = pie_pool.get(label, 0.0) + value_analysis

    cash_rows: list[CashRow] = []
    cash = ds.cash
    if accounts is not None:
        cash = cash[cash["account"].isin(accounts)]
    if not cash.empty:
        last_cash_date = cash.groupby("account")["date"].max().to_dict()
        cash = cash[cash["date"] == cash["account"].map(last_cash_date)]
        for r in cash.itertuples(index=False):
            if abs(r.balance_local) < 0.01:
                continue
            value_analysis = to_analysis(r.balance_local, r.currency, r.date)
            cash_rows.append(CashRow(account=r.account, currency=r.currency,
                                     balance_local=r.balance_local,
                                     value_analysis_ccy=value_analysis))
            equity_total += value_analysis
            # §21.3 — en el pastel el efectivo va combinado en una sola porción
            # al tipo de cambio vigente, sea la divisa que sea; en la tabla de
            # detalle (cash_rows, arriba) sigue apareciendo por cuenta/divisa.
            pie_pool["Efectivo"] = pie_pool.get("Efectivo", 0.0) + value_analysis

    # Denominador del peso: equity_total en modo "patrimonio" (de siempre),
    # exposure_total (equity + nocional) en modo "exposición" — ver
    # docstring. Segunda pasada porque equity_total no está cerrado hasta
    # sumar el efectivo, procesado arriba DESPUÉS del bucle de posiciones.
    weight_total = (equity_total + notional_total
                    if weight_basis == "exposicion" else equity_total)
    for row in rows:
        row.pct_weight = (100 * row.value_analysis_ccy / weight_total
                          if weight_total else None)
    for row in cash_rows:
        row.pct_weight = (100 * row.value_analysis_ccy / weight_total
                          if weight_total else None)

    ordered = sorted(pie_pool.items(), key=lambda kv: -kv[1])
    top, rest = ordered[:pie_top_n], ordered[pie_top_n:]
    pie = [PieSlice(label=k, value_analysis_ccy=v,
                    pct=100 * v / weight_total if weight_total else 0.0) for k, v in top]
    if rest:
        rest_sum = sum(v for _, v in rest)
        pie.append(PieSlice(label="Otros", value_analysis_ccy=rest_sum,
                            pct=100 * rest_sum / weight_total if weight_total else 0.0))

    return PortfolioSnapshot(as_of=as_of, positions=rows, cash=cash_rows, pie=pie,
                             equity_total_analysis_ccy=equity_total,
                             exposure_total_analysis_ccy=equity_total + notional_total,
                             currency=m)
