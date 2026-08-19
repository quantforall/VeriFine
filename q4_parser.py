"""
Quant4all — Parser (§1 de la spec).

Convierte los XML crudos de IBKR en las cuatro tablas canónicas:

    nav          (account, date) -> total, accruals
    positions    (account, date, conid) -> currency, quantity, price, value_local
    cash         (account, date, currency) -> balance_local
    fx           (date, currency) -> rate_to_base
    movements    (account, date, movement_id) -> type, currency, amount, ...

A partir de aquí el motor es agnóstico al origen: no vuelve a ver un XML.

Aquí vive todo el conocimiento sucio del formato de IBKR. Cada regla no
obvia lleva la referencia a la sección de la spec que la justifica.
"""

from __future__ import annotations

import bisect
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import pandas as pd

log = logging.getLogger("q4.parser")

NAV_TOL = 1e-6

# §3 — clasificación de movimientos.
# Sólo estos tipos de CashTransaction son flujo de capital. Dividendos,
# intereses, comisiones, retenciones e impuestos son RENDIMIENTO y no se
# neutralizan: forman parte de lo que la cartera ha ganado o perdido.
CASH_FLOW_TYPES = {"Deposits/Withdrawals"}

# §20.6 — único tipo de CorporateAction que el motor de Operaciones sabe
# aplicar hoy. "FS" = split. Descripción del split viene como texto libre
# ("SPLIT 10 FOR 1 (...)"), no como dos campos numéricos separados.
_SPLIT_RE = re.compile(r"SPLIT (\d+) FOR (\d+)")


@dataclass
class Dataset:
    nav: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    cash: pd.DataFrame = field(default_factory=pd.DataFrame)
    fx: pd.DataFrame = field(default_factory=pd.DataFrame)
    movements: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    corporate_actions: pd.DataFrame = field(default_factory=pd.DataFrame)
    base_currency: str = "EUR"
    nav_conflicts: list = field(default_factory=list)
    bootstrapped: list = field(default_factory=list)

    @property
    def dates(self) -> list[str]:
        return sorted(self.nav["date"].unique())

    @property
    def accounts(self) -> list[str]:
        return sorted(self.nav["account"].unique())


# --------------------------------------------------------------------------
# Parseo de un fichero
# --------------------------------------------------------------------------

def _f(el, attr, default=0.0) -> float:
    v = el.get(attr)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def parse_file(path: str) -> dict[str, list]:
    """Parsea un XML crudo. Devuelve listas de dicts, sin deduplicar."""
    root = ET.parse(path).getroot()
    out = dict(nav=[], positions=[], cash=[], fx=[], movements=[], boot=[],
              trades=[], corporate_actions=[])

    base = "EUR"
    acc_info = root.find(".//AccountInformation")
    if acc_info is not None and acc_info.get("currency"):
        base = acc_info.get("currency")

    for e in root.iter("EquitySummaryByReportDateInBase"):
        total = _f(e, "total")
        # §2 — `accruals` es el resto del NAV que NO tiene divisa asignable
        # (dividendAccruals, interestAccruals…). Se resta el efectivo Y todas las
        # clases de activo que ya entran por `OpenPosition` (stock, options,
        # bonds, funds, commodities, notes): si no se restara `options`, una
        # opción se contaría dos veces —como posición y como accrual— y
        # descuadraría el NAV. Con una sola cuenta de acciones nunca se notó.
        accruals = total - sum(_f(e, k) for k in (
            "cash", "stock", "options", "bonds", "funds", "commodities", "notes"))
        out["nav"].append(dict(account=e.get("accountId"), date=e.get("reportDate"),
                               total=total, accruals=accruals, cash_base=_f(e, "cash"),
                               currency=e.get("currency") or base))

    for e in root.iter("OpenPosition"):
        # positionValue viene en DIVISA LOCAL del instrumento. fxRateToBase
        # existe pero no se usa aquí: §0 exige almacenar en local y convertir
        # con la matriz fx, no con el tipo incrustado en cada fila.
        # assetCategory se guarda para excluir el nocional de derivados que no
        # es NAV (ver NON_NAV_CATEGORIES / currency_buckets).
        out["positions"].append(dict(
            account=e.get("accountId"), date=e.get("reportDate"),
            conid=e.get("conid"), symbol=e.get("symbol"),
            asset_category=e.get("assetCategory"),
            currency=e.get("currency"), quantity=_f(e, "position"),
            price=_f(e, "markPrice"), value_local=_f(e, "positionValue"),
            # §21 — CARTERA: coste base y plusvalía latente YA calculados por
            # IBKR (su propia contabilidad de splits/transferencias incluida).
            # No se recalculan con nuestro FIFO — mismo principio que §20.2
            # con fifoPnlRealized, aplicado a lo que sigue abierto.
            description=e.get("description"),
            cost_basis_price=_f(e, "costBasisPrice", None),
            cost_basis_money=_f(e, "costBasisMoney", None),
            fifo_pnl_unrealized=_f(e, "fifoPnlUnrealized", None)))

    for e in root.iter("FxPosition"):
        # Sólo el nivel SUMMARY: los lotes individuales duplicarían el saldo.
        if e.get("levelOfDetail") != "SUMMARY":
            continue
        out["cash"].append(dict(account=e.get("accountId"), date=e.get("reportDate"),
                                currency=e.get("fxCurrency"),
                                balance_local=_f(e, "quantity")))

    for e in root.iter("ConversionRate"):
        out["fx"].append(dict(date=e.get("reportDate"), currency=e.get("fromCurrency"),
                              to=e.get("toCurrency"), rate=_f(e, "rate")))

    for e in root.iter("CashTransaction"):
        t = e.get("type")
        out["movements"].append(dict(
            account=e.get("accountId"), movement_id=e.get("transactionID"),
            # §8.1 — reportDate SIEMPRE. settleDate se guarda pero no se usa:
            # imputar a la liquidación desviaba el TWR de 2023 en 6.942 pb.
            date=e.get("reportDate"), settle_date=e.get("settleDate"),
            type=t, currency=e.get("currency"),
            # symbol/conid: vacíos salvo en dividendos/retenciones ligados a un
            # instrumento — §20.9, para atribuir dividendos por ticker.
            symbol=e.get("symbol"), conid=e.get("conid"),
            amount_local=_f(e, "amount"), fx_to_base=_f(e, "fxRateToBase", 1.0),
            flow_base=_f(e, "amount") * _f(e, "fxRateToBase", 1.0)
            if t in CASH_FLOW_TYPES else 0.0,
            is_flow=t in CASH_FLOW_TYPES, counterparty=None, source="cash"))

    for e in root.iter("Transfer"):
        fx = _f(e, "fxRateToBase", 1.0) or 1.0
        # §11.7 — el flujo de una transferencia suma su parte de efectivo Y su
        # parte de valores. Omitir positionAmountInBase desviaba 2023 en 575 pb.
        flow_base = _f(e, "cashTransfer") * fx + _f(e, "positionAmountInBase")
        out["movements"].append(dict(
            account=e.get("accountId"), movement_id=e.get("transactionID"),
            date=e.get("reportDate"), settle_date=e.get("settleDate"),
            type="TRANSFER_" + (e.get("assetCategory") or "?"),
            # symbol/conid/quantity: origen de una posición cuando no hay Trade
            # de apertura — §20.5. positionAmountInBase / quantity da el precio
            # de entrada implícito en la divisa base; se deja crudo aquí, el
            # motor de Operaciones decide cómo usarlo.
            symbol=e.get("symbol"), conid=e.get("conid"), quantity=_f(e, "quantity"),
            position_amount_local=_f(e, "positionAmount"),
            position_amount_base=_f(e, "positionAmountInBase"),
            currency=e.get("currency"),
            amount_local=_f(e, "cashTransfer") + _f(e, "positionAmountInBase") / fx,
            fx_to_base=fx, flow_base=flow_base, is_flow=True,
            # La contraparte decide si es interno al consolidar (§8).
            counterparty=e.get("account"), source="transfer"))

    # §20 — Operaciones (P&L por ticker). Se itera por FlexStatement, no con
    # root.iter("Trade") plano: accountId no siempre viene en el propio Trade
    # (dependía de qué campos tuviera marcados el Flex Query), pero el
    # FlexStatement que lo envuelve siempre lo lleva. e.get("accountId") tiene
    # prioridad si está (Query ya corregido); si no, cae al del statement.
    for st in root.iter("FlexStatement"):
        acct_fallback = st.get("accountId")
        for e in st.iter("Trade"):
            out["trades"].append(dict(
                account=e.get("accountId") or acct_fallback,
                trade_id=e.get("tradeID"), conid=e.get("conid"), symbol=e.get("symbol"),
                underlying_symbol=e.get("underlyingSymbol") or None,
                asset_category=e.get("assetCategory"), put_call=e.get("putCall") or None,
                strike=_f(e, "strike", None), expiry=e.get("expiry") or None,
                multiplier=_f(e, "multiplier", 1.0) or 1.0, currency=e.get("currency"),
                date=e.get("tradeDate"),
                # dateTime: "AAAAMMDD;HHMMSS" — ordena bien como texto (§20.2).
                datetime=e.get("dateTime") or e.get("tradeDate"),
                quantity=_f(e, "quantity"), trade_price=_f(e, "tradePrice"),
                commission_local=_f(e, "ibCommission"),
                open_close=e.get("openCloseIndicator"), buy_sell=e.get("buySell"),
                fifo_pnl_realized_local=_f(e, "fifoPnlRealized")))
        for e in st.iter("CorporateAction"):
            if e.get("type") != "FS":
                continue    # §20.6 — sólo splits; otros tipos, fuera de alcance hoy
            m = _SPLIT_RE.search(e.get("description") or "")
            if not m:
                continue
            out["corporate_actions"].append(dict(
                account=e.get("accountId") or acct_fallback,
                action_id=e.get("actionID"), conid=e.get("conid"), symbol=e.get("symbol"),
                datetime=e.get("dateTime") or e.get("reportDate"),
                ratio_num=int(m.group(1)), ratio_den=int(m.group(2))))

    # §11.8 — bootstrap del día previo al primer statement: tiene NAV pero no
    # snapshot de posiciones. Se reconstruye la composición por divisa desde
    # priorPeriodValue (valores) y startingCash (efectivo).
    # Se hace por CADA statement/cuenta, no solo el primero: en una FlexQuery
    # multi-cuenta cada cuenta tiene su propio día de arranque sin snapshot, y
    # tratar únicamente stmts[0] dejaba las demás cuentas sin componer.
    have_snap = {(e.get("accountId"), e.get("reportDate")) for e in root.iter("OpenPosition")}
    have_snap |= {(e.get("accountId"), e.get("reportDate")) for e in root.iter("FxPosition")}
    for st in root.iter("FlexStatement"):
        acct = st.get("accountId")
        navs = sorted({e.get("reportDate") for e in st.iter("EquitySummaryByReportDateInBase")})
        if not navs:
            continue
        d = navs[0]
        if (acct, d) in have_snap:            # ese día ya trae snapshot: no es bootstrap
            continue
        tot = next((_f(e, "total") for e in st.iter("EquitySummaryByReportDateInBase")
                    if e.get("reportDate") == d), 0.0)
        if abs(tot) < 1e-6:                    # cuenta vacía (NAV≈0): nada que reconstruir
            continue
        for e in st.iter("ChangeInPositionValue"):
            if e.get("currency") not in (None, "", "BASE_SUMMARY"):
                out["boot"].append(dict(account=acct, date=d, kind="position",
                                        currency=e.get("currency"),
                                        value_local=_f(e, "priorPeriodValue")))
        for e in st.iter("CashReportCurrency"):
            if e.get("levelOfDetail") == "Currency":
                out["boot"].append(dict(account=acct, date=d, kind="cash",
                                        currency=e.get("currency"),
                                        value_local=_f(e, "startingCash")))

    out["base_currency"] = base
    return out


# --------------------------------------------------------------------------
# Consolidación de varios ficheros
# --------------------------------------------------------------------------

def load(paths: list[str]) -> Dataset:
    """Parsea y funde varios crudos en un único Dataset.

    Los bloques del backfill y el solape de 10 días del incremental hacen que
    las fechas repetidas sean la NORMA (§19.3). Deduplicar es obligatorio, y
    los conflictos de NAV se registran en vez de sobrescribirse (§19.6).
    """
    acc = dict(nav=[], positions=[], cash=[], fx=[], movements=[], boot=[],
              trades=[], corporate_actions=[])
    base = "EUR"
    for p in paths:
        r = parse_file(p)
        base = r.pop("base_currency", base)
        for k in acc:
            acc[k].extend(r[k])

    ds = Dataset(base_currency=base)

    # --- NAV: detectar conflictos antes de deduplicar -------------------
    nav = pd.DataFrame(acc["nav"])
    if not nav.empty:
        g = nav.groupby(["account", "date"])["total"]
        spread = (g.max() - g.min())
        bad = spread[spread > NAV_TOL]
        for (a, d), diff in bad.items():
            sub = nav[(nav.account == a) & (nav.date == d)]["total"]
            ds.nav_conflicts.append(dict(account=a, date=d, diff=float(diff),
                                         values=sorted(set(sub.round(6)))))
            log.warning("Conflicto de NAV %s %s: %s", a, d, sorted(set(sub.round(6))))
        ds.nav = (nav.sort_values("date")
                     .drop_duplicates(subset=["account", "date"], keep="last")
                     .reset_index(drop=True))

    ds.positions = (pd.DataFrame(acc["positions"])
                    .drop_duplicates(subset=["account", "date", "conid"], keep="last")
                    .reset_index(drop=True)) if acc["positions"] else pd.DataFrame()

    ds.cash = (pd.DataFrame(acc["cash"])
               .drop_duplicates(subset=["account", "date", "currency"], keep="last")
               .reset_index(drop=True)) if acc["cash"] else pd.DataFrame()

    fx = pd.DataFrame(acc["fx"])
    if not fx.empty:
        fx = fx[fx["to"] == base]
        ds.fx = fx.drop_duplicates(subset=["date", "currency"], keep="last") \
                  .drop(columns=["to"]).reset_index(drop=True)

    mov = pd.DataFrame(acc["movements"])
    if not mov.empty:
        # §19.3 — deduplicar el solape, pero por (transactionID, cuenta): las DOS
        # patas de una transferencia interna comparten transactionID en cuentas
        # distintas, y deduplicar solo por transactionID tiraba una pata (el
        # consolidado salía bien, pero la cuenta que envía, analizada sola,
        # perdía sus salidas). Con la cuenta en la clave, ambas patas sobreviven
        # y el solape (misma pata repetida) se sigue deduplicando.
        ds.movements = mov.drop_duplicates(subset=["movement_id", "account"], keep="last") \
                          .sort_values("date").reset_index(drop=True)

    # §20 — mismo criterio de dedup que movements: el solape del backfill
    # repite tradeID/actionID entre bloques, la clave es (cuenta, id).
    tr = pd.DataFrame(acc["trades"])
    if not tr.empty:
        ds.trades = tr.drop_duplicates(subset=["account", "trade_id"], keep="last") \
                      .sort_values("datetime").reset_index(drop=True)

    ca = pd.DataFrame(acc["corporate_actions"])
    if not ca.empty:
        ds.corporate_actions = ca.drop_duplicates(subset=["account", "action_id"], keep="last") \
                                  .sort_values("datetime").reset_index(drop=True)

    # --- bootstrap: inyectar la composición del primer día ---------------
    boot_ad: set = set()          # (cuenta, fecha) reconstruidas por bootstrap
    if acc["boot"]:
        boot = pd.DataFrame(acc["boot"]).drop_duplicates(
            subset=["account", "date", "kind", "currency"], keep="last")
        have = set(zip(ds.positions.get("account", []), ds.positions.get("date", []))) \
            if not ds.positions.empty else set()
        have |= set(zip(ds.cash.get("account", []), ds.cash.get("date", []))) \
            if not ds.cash.empty else set()
        rows_p, rows_c = [], []
        for _, r in boot.iterrows():
            if (r["account"], r["date"]) in have:
                continue
            if r["kind"] == "position":
                rows_p.append(dict(account=r["account"], date=r["date"],
                                   conid=f"BOOT_{r['currency']}", symbol="BOOTSTRAP",
                                   currency=r["currency"], quantity=0.0, price=0.0,
                                   value_local=r["value_local"]))
            else:
                rows_c.append(dict(account=r["account"], date=r["date"],
                                   currency=r["currency"],
                                   balance_local=r["value_local"]))
        if rows_p:
            ds.positions = pd.concat([ds.positions, pd.DataFrame(rows_p)], ignore_index=True)
        if rows_c:
            ds.cash = pd.concat([ds.cash, pd.DataFrame(rows_c)], ignore_index=True)
        boot_ad = {(r["account"], r["date"]) for r in rows_p + rows_c}
        if rows_p or rows_c:
            days = sorted({r["date"] for r in rows_p + rows_c})
            ds.bootstrapped = days
            log.info("Bootstrap aplicado a %s", days)

    # --- reconciliar el efectivo en divisa base (§2) ----------------------
    # IBKR no siempre emite un FxPosition para la moneda base: una cuenta con
    # sólo efectivo en EUR no trae FxPosition, así que ese efectivo no entra por
    # los cubos. Se inyecta como cubo de la base la diferencia entre el `cash`
    # del NAV y el efectivo ya capturado. Es FX-invariante (base, tipo 1), así
    # que vale igual para el escenario congelado. Nunca se notó con una sola
    # cuenta porque siempre tenía posiciones/USD.
    if not ds.nav.empty and "cash_base" in ds.nav.columns:
        fxm = fx_matrix(ds)
        keys = {c: sorted(m) for c, m in fxm.items()}

        def _rate(cur, date):
            if cur == base:
                return 1.0
            m = fxm.get(cur)
            if not m:
                return 1.0
            ks = keys[cur]
            i = bisect.bisect_right(ks, date)
            return m[ks[i - 1]] if i else m[ks[0]]

        cap: dict = {}
        if not ds.cash.empty:
            for a, d, c, v in zip(ds.cash["account"], ds.cash["date"],
                                  ds.cash["currency"], ds.cash["balance_local"]):
                cap[(a, d)] = cap.get((a, d), 0.0) + v * _rate(c, d)
        # Umbral de 1 € para reconciliar sólo efectivo base GENUINAMENTE ausente
        # (una cuenta sin FxPosition de la base: miles de €), no el ruido de coma
        # flotante de la reconstrucción (fracciones de céntimo), que bastaría
        # para voltear el signo de algún retorno diario.
        # Y nunca en los días bootstrap: ahí la composición ya es una aproximación
        # (§11.8) y tocarla cambiaría el freeze de t0, reintroduciendo el
        # artefacto que v2.3 eliminó.
        RECON_TOL = 1.0
        rows = [dict(account=a, date=d, currency=base, balance_local=cb - cap.get((a, d), 0.0))
                for a, d, cb in zip(ds.nav["account"], ds.nav["date"], ds.nav["cash_base"])
                if (a, d) not in boot_ad and abs(cb - cap.get((a, d), 0.0)) > RECON_TOL]
        if rows:
            ds.cash = pd.concat([ds.cash, pd.DataFrame(rows)], ignore_index=True)
            ds.cash = ds.cash.groupby(["account", "date", "currency"],
                                      as_index=False)["balance_local"].sum()
            log.info("Efectivo en divisa base reconciliado en %d (cuenta,fecha)", len(rows))

    return ds


# --------------------------------------------------------------------------
# Vistas derivadas
# --------------------------------------------------------------------------

def fx_matrix(ds: Dataset) -> dict[str, dict[str, float]]:
    """{currency: {date: rate_to_base}}, con la base a 1,0 constante."""
    m: dict[str, dict[str, float]] = {ds.base_currency: {}}
    if not ds.fx.empty:
        for cur, sub in ds.fx.groupby("currency"):
            m[cur] = dict(zip(sub["date"], sub["rate"]))
    return m


# El `positionValue` de un futuro es su NOCIONAL, que NO forma parte del NAV: la
# posición no inmoviliza capital y su P&L se liquida a diario en efectivo (que ya
# está en los cubos). Sumar el nocional descuadraba 2021, cuando la cuenta llevó
# un corto de futuros S&P. Verificado: total = cash + stock + accruals, sin los
# futuros. Otros derivados de nocional (FOP, CFD) necesitarían el mismo trato.
NON_NAV_CATEGORIES = {"FUT"}


def currency_buckets(ds: Dataset, accounts: list[str] | None = None) -> dict[str, dict[str, float]]:
    """{date: {currency: importe en divisa local}}, posiciones + efectivo.

    Es la entrada del escenario FX-neutral (§7): congelar la matriz fx y
    revalorar estos mismos cubos.
    """
    positions = ds.positions
    if not positions.empty and "asset_category" in positions.columns:
        positions = positions[~positions["asset_category"].isin(NON_NAV_CATEGORIES)]
    out: dict[str, dict[str, float]] = {}
    for df, col in ((positions, "value_local"), (ds.cash, "balance_local")):
        if df.empty:
            continue
        d = df if accounts is None else df[df["account"].isin(accounts)]
        for (date, cur), v in d.groupby(["date", "currency"])[col].sum().items():
            out.setdefault(date, {})
            out[date][cur] = out[date].get(cur, 0.0) + float(v)
    return out


def flows(ds: Dataset, accounts: list[str] | None = None) -> dict[str, list[tuple[str, float]]]:
    """{date: [(currency, amount_local), ...]} de los flujos EXTERNOS.

    §8 — una transferencia entre dos cuentas ambas seleccionadas es interna y
    se anula. Como ambas patas comparten reportDate, basta con excluirlas: no
    hace falta ventana de emparejamiento.
    """
    if ds.movements.empty:
        return {}
    m = ds.movements[ds.movements["is_flow"]]
    if accounts is not None:
        m = m[m["account"].isin(accounts)]
        internal = m["counterparty"].isin(accounts)
        m = m[~internal]
    out: dict[str, list[tuple[str, float]]] = {}
    for _, r in m.iterrows():
        out.setdefault(r["date"], []).append((r["currency"], float(r["amount_local"])))
    return out


def accruals(ds: Dataset, accounts: list[str] | None = None) -> dict[str, float]:
    d = ds.nav if accounts is None else ds.nav[ds.nav["account"].isin(accounts)]
    return d.groupby("date")["accruals"].sum().to_dict()


def nav_series(ds: Dataset, accounts: list[str] | None = None) -> dict[str, float]:
    """§8 — la consolidación es a nivel de VALOR, nunca promediando retornos."""
    d = ds.nav if accounts is None else ds.nav[ds.nav["account"].isin(accounts)]
    return d.groupby("date")["total"].sum().to_dict()
