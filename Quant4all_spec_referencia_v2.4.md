# Quant4all — Especificación de referencia v2.4

*Documento normativo. Define qué debe calcular el motor, con qué convenciones, qué tests debe pasar y cómo se presentan los resultados. No contiene decisiones de stack.*

> **v2.4 — 18/08/2026.** Consolidación multi-cuenta **validada** contra una FlexQuery real de 5 cuentas y su histórico completo 2021→2026 (§16.2): T7 y T8 pasan. Al validarlo se corrigieron **tres bugs** del parser: (1) el bootstrap sólo trataba el primer statement; (2) el dedup de movimientos por `transactionID` perdía una pata de cada transferencia interna (ahora por `(transactionID, cuenta)`); (3) el efectivo en la divisa base no se capturaba porque IBKR no emite `FxPosition` para ella (§2, reconciliación desde `@cash`). Golden de una cuenta intactos.
>
> **v2.3 — 18/08/2026.** Corrección tras la puesta en marcha real de la ingesta (backfill 2021→2026 contra IBKR). (1) §13.1/§13.3: la Estrategia de 2023 y del histórico se corrigen (9,9341 %→9,7163 %; histórico 113,80 %→113,27 %). El 9,9341 % era un **artefacto**: un escalado de cubos en el día de arranque (`20221230`), que en la extracción antigua era un bootstrap sin `ConversionRate`. Con el backfill ese día trae su FX real y reconstruye el NAV exacto, así que el escalado sobra. (2) §11.8 pasa a CERRADA: la banda ±21 pb se resuelve al extremo **bajo** (~9,72 %), no al alto. (3) Nota de parser: el nocional de los futuros (`assetCategory=FUT`) **no es NAV** y se excluye de los cubos; sumarlo descuadraba 2021 en ~83 k€.
>
> **v2.2 — 16/08/2026.** Añadida §19: sincronización. Backfill reanudable e incremental diario sobre la misma query, con canario de regresión contra los golden values en cada ejecución.
>
> **v2.1 — 16/08/2026.** Añadida §18: ingesta directa desde el Flex Web Service. El troceado del histórico pasa a ser automático (`fd`/`td`) y el margen de 5 días del backfill cierra §11.8.
>
> **v2.0 — 16/08/2026.** Consolidación completa. La spec está **validada de extremo a extremo contra datos reales** de la cuenta U7790974 sobre una serie continua de 945 días (2022-12-30 → 2026-08-13). Novedades respecto a v1.2: §13 resultados validados y *golden values*; §14 corregida (CAGR por calendario, drawdown diario, ventanas móviles, regla de vacíos); §16 estado del proyecto; §17 especificación del panel.
>
> Historial: v1.0 spec inicial · v1.1 calibración de `w` y fecha de flujo · v1.2 métricas y benchmark · v2.0 validación completa.

---

## 0. Notación y convenciones

| Símbolo | Significado |
|---|---|
| `A` | Conjunto de cuentas seleccionadas para el análisis |
| `t` | Fecha de cierre de mercado (día hábil) |
| `t₀` | Cierre del día **anterior** al inicio del período (valor de partida) |
| `t_N` | Cierre del último día del período |
| `i` | Instrumento (posición) |
| `c` | Divisa |
| `c(i)` | Divisa local del instrumento `i` |
| `m` | **Moneda de análisis** elegida por el usuario (EUR, USD, …) |
| `n` | **Numerario interno** del almacenamiento (arbitrario, invisible al usuario) |
| `q(i,a,t)` | Cantidad del instrumento `i` en la cuenta `a` al cierre de `t` |
| `p(i,t)` | Precio de `i` al cierre de `t`, **en divisa local `c(i)`** |
| `C(c,a,t)` | Saldo de efectivo en divisa `c`, cuenta `a`, cierre de `t` |
| `X(c,t)` | Unidades de numerario por 1 unidad de la divisa `c`, al cierre de `t` |

**Regla de oro:** todo se almacena en divisa local. `X` es la única capa de conversión. La moneda de análisis `m` **no existe en la capa de datos**, sólo en la de valoración.

Tipo de cambio derivado entre dos divisas cualesquiera:

```
X(c → m, t) = X(c, t) / X(m, t)
```

Esto elimina la moneda base de IBKR del modelo. EUR y USD son dos vistas simétricas del mismo objeto; ninguna es privilegiada.

**Signo de los flujos:** `F > 0` significa capital que **entra** en el conjunto `A`.

**Precisión:** todos los cálculos en punto flotante de doble precisión. Prohibido redondear resultados intermedios. El redondeo ocurre únicamente en la capa de presentación.

---

## 1. Objeto canónico: la cartera diaria

El motor trabaja sobre una única estructura. Todo lo demás se deriva de ella.

**Tabla `positions`** — grano `(account, date, instrument)`

```
account, date, instrument_id, currency, quantity, price_local, accrued_local
```

**Tabla `cash`** — grano `(account, date, currency)`

```
account, date, currency, balance
```

**Tabla `fx`** — grano `(date, currency)`

```
date, currency, rate_to_numeraire
```

**Tabla `movements`** — grano `(account, date, movement_id)`

```
account, date, movement_id, type, currency, amount,
counterparty_account, instrument_id, settlement_group_id
```

`type` ∈ {`DEPOSIT`, `WITHDRAWAL`, `TRANSFER_CASH`, `TRANSFER_SECURITY`, `TRADE`, `FX_CONVERSION`, `DIVIDEND`, `INTEREST`, `FEE`, `TAX`, `OTHER`}.

Ninguna otra fuente de datos entra en el cálculo. Los XML de IBKR se normalizan a estas cuatro tablas y a partir de ahí el motor es agnóstico al origen.

---

## 2. Valoración diaria

Valor de la cartera al cierre de `t`, expresado en la moneda de análisis `m`:

```
V(m, t) = Σ_{a∈A} [ Σ_i q(i,a,t) · (p(i,t) + accrued(i,a,t)) · X(c(i)→m, t)
                   + Σ_c C(c,a,t) · X(c→m, t) ]
```

**Decisión cerrada (§11.1):** `accrued` **se incluye**. El NAV de referencia es el atributo `total` de `EquitySummaryByReportDateInBase`, que agrega `cash + stock + options + bonds + funds + dividendAccruals + ...`. Con esta definición el motor reproduce el TWR de IBKR de forma exacta.

**Efectivo en divisa base (v2.4).** IBKR **no emite un `FxPosition` para la moneda base**: una cuenta que en algún tramo tiene sólo efectivo en EUR no trae ningún `FxPosition`, y ese efectivo no entra por los cubos por divisa. Con una sola cuenta (siempre con posiciones y USD) nunca se notó; con multi-cuenta descuadraba el NAV hasta por su valor íntegro. Se reconcilia inyectando como cubo de la base la diferencia entre `EquitySummaryByReportDateInBase/@cash` y el efectivo ya capturado. Es FX-invariante (base, tipo 1), así que vale igual en el escenario congelado. **No se aplica en los días bootstrap** (§11.8): ahí la composición ya es aproximada y tocarla reintroduciría el artefacto de escalado que resolvió v2.3. Umbral de 1 € para no reconciliar el ruido de coma flotante.

**Derivados de nocional (v2.3).** El `positionValue` de un futuro (`assetCategory=FUT`) es su **nocional**, que no es NAV: el futuro no inmoviliza capital y su P&L se liquida a diario en efectivo. Se excluye de los cubos (`NON_NAV_CATEGORIES`). Otros derivados de nocional (FOP, CFD) necesitarían el mismo trato.

---

## 3. Clasificación de movimientos: flujo vs. rendimiento

Un movimiento es **flujo de capital** si y sólo si cruza la frontera del conjunto `A`. Todo lo demás es rendimiento y no se neutraliza.

| Tipo | ¿Flujo? | Nota |
|---|---|---|
| `DEPOSIT` / `WITHDRAWAL` | **Sí** | Siempre externo |
| `TRANSFER_*` con contraparte ∉ `A` | **Sí** | Externo |
| `TRANSFER_*` con contraparte ∈ `A` | **No** | Interno: se anula al consolidar (§8) |
| `TRADE` | No | Cambia composición, no capital |
| `FX_CONVERSION` | No | Cambia composición, no capital |
| `DIVIDEND` / `INTEREST` | No | Rendimiento |
| `FEE` / `TAX` | No | Rendimiento (negativo) |

Flujo neto del día, en moneda de análisis:

```
F(m, t) = Σ_{a∈A} Σ_{k ∈ flujos(a,t)} amount(k) · X(currency(k) → m, t)
```

**Cada flujo se convierte al FX de su propia fecha.** Nunca a un FX de cierre de período.

---

## 4. Retorno diario y convención de flujos

Convención parametrizada por `w ∈ [0,1]`, peso temporal del flujo dentro del día:

```
r(m, t) = [ V(m,t) − V(m,t−1) − F(m,t) ] / [ V(m,t−1) + w · F(m,t) ]
```

- `w = 1` → flujo a inicio de día. Equivale a `V(t) / (V(t−1) + F(t)) − 1`.
- `w = 0` → flujo a cierre de día. Equivale a `(V(t) − F(t)) / V(t−1) − 1`.
- `w ∈ (0,1)` → Modified Dietz intradía.

**Valor calibrado: `w = 1` (flujo a inicio de día).** Con `w = 1` y fecha de flujo = `reportDate`, el motor reproduce el `twr` de `ChangeInNAV` de IBKR con diferencia **0,00000 pb** en los dos períodos de auditoría disponibles (2023-01-05→2024-01-05 y 2024-08-13→2025-08-13). `w` se mantiene como parámetro configurable para futuras validaciones, pero el valor de producción es 1.

Valores alternativos, para dimensionar el error de elegir mal: `w = 0,5` desvía −0,87 pb / −6,98 pb y `w = 0` desvía −4,55 pb / −13,86 pb en esos mismos períodos.

**Advertencia metodológica:** que el TWR anual compuesto cuadre con IBKR **no valida** la convención. Con flujos pequeños respecto al NAV, los tres valores de `w` producen anuales casi idénticos. La calibración debe hacerse sobre los retornos **diarios** de los días con flujo material.

---

## 5. Encadenamiento

```
1 + R(m, [t₀, t_N]) = Π_{t=t₀+1}^{t_N} ( 1 + r(m, t) )
```

Propiedades que se derivan y deben respetarse:

- **Composición por bloques:** partir el período en subperíodos contiguos y multiplicar los factores da exactamente el mismo resultado. Esto resuelve el límite de 365 días de FlexQuery sin aproximación alguna, siempre que los bloques empalmen en cierres consecutivos y ningún flujo se cuente dos veces.
- **YTD y períodos personalizados** no requieren lógica distinta: sólo cambian `t₀` y `t_N`.
- `V(m, t₀)` es el cierre del día **anterior** al primer día del período. Un período que empieza el 1 de enero usa el cierre del 31 de diciembre.

---

## 6. Moneda de análisis: qué es y qué no es

El TWR en moneda `m` se calcula recorriendo toda la cadena en `m`: valoración diaria en `m`, flujos en `m`, retorno diario en `m`, composición. **No se convierte un retorno ya calculado.**

### 6.1 Relación cerrada en ausencia de flujos

Si en `[t₀, t_N]` no hay ningún flujo externo, la cadena telescopa exactamente:

```
1 + R(m) = ( 1 + R(n) ) · X(m, t₀) / X(m, t_N)
```

Es decir: **sin flujos, el TWR en otra moneda es el TWR original ajustado multiplicativamente por el movimiento de la divisa en el período.** No es una aproximación, es una identidad.

### 6.2 Consecuencia práctica

Los TWR USD provisionales del documento de trabajo son consistentes con esta fórmula cerrada aplicada al TWR EUR. Eso significa que:

1. Son **correctos** para cualquier período sin flujos externos.
2. Son **incorrectos**, en segundo orden, para cualquier período con flujos — porque un flujo entra a un FX intermedio, no al del cierre.

La magnitud del error crece con el tamaño del flujo relativo al NAV y con la distancia entre el FX del día del flujo y el del cierre. El motor debe calcular la cadena completa; la fórmula cerrada de §6.1 queda como **test de regresión**, no como método de cálculo.

### 6.3 Prohibiciones explícitas

- `R(USD) ≠ R(EUR) + ΔFX`. La relación es multiplicativa, no aditiva.
- No usar un FX único de cierre para valorar la serie completa.
- No usar el indicador *Performance adjusted to market base* de IBKR como motor.

---

## 7. Atribución Estrategia / FX

### 7.1 Escenario FX-neutral

Se recalcula toda la cadena sustituyendo la matriz de tipos de cambio por su valor congelado en `t₀`:

```
X̃(c, t) = X(c, t₀)   para todo t ∈ [t₀, t_N]
```

Reglas del escenario congelado:

- Toda valoración usa `X̃`.
- Todo flujo se convierte con `X̃`.
- **Toda `FX_CONVERSION` se ejecuta al tipo congelado.** Bajo `X̃` una conversión de divisa conserva valor exactamente y no genera P&L. Esto es lo correcto: la conversión deja de ser una decisión con resultado.
- Toda `TRADE` en divisa extranjera se liquida al tipo congelado.
- Cantidades, precios locales, dividendos, intereses, comisiones e impuestos son **idénticos** a los del escenario real. Sólo cambia la matriz FX.

De ahí:

```
1 + R_estrategia = Π_t ( 1 + r̃(t) )
```

### 7.2 Invariancia de la estrategia respecto a la moneda

Bajo `X̃` toda la serie de valores se escala por la constante `1/X̃(m)`, que se cancela en el cociente del retorno diario. Por tanto:

> **`R_estrategia` es el mismo número se analice en EUR o en USD.**

Esta es una propiedad deseable y un test fuerte: si el motor devuelve estrategias distintas para EUR y USD, hay un error de implementación. Toda la diferencia entre monedas queda absorbida por el término FX, que es donde debe estar.

### 7.3 Efecto divisa como residuo

```
R_FX(m) = ( 1 + R_total(m) ) / ( 1 + R_estrategia ) − 1
```

Definir FX como residuo **garantiza por construcción** que el desglose cierre:

```
( 1 + R_estrategia ) · ( 1 + R_FX ) − 1 = R_total
```

Calcular ambos componentes de forma independiente dejaría un término cruzado no atribuido y el criterio de aceptación fallaría por unos pocos puntos básicos, sin que eso indicara ningún error real.

### 7.4 Propiedades y limitaciones aceptadas

- **La atribución no es aditiva entre subperíodos.** Encadenar la atribución de 2023 y 2024 no da la atribución de 2023–2024, porque la fecha de congelación cambia. Es una consecuencia inevitable de fijar el FX en `t₀` y debe reflejarse en la interfaz: la atribución se calcula siempre para el período mostrado.
- **Una posición de divisa deliberada** (carry, especulación en FX) queda clasificada íntegramente como efecto divisa, no como estrategia. Es coherente con la definición, pero conviene documentarlo si en el futuro se opera FX de forma discrecional.
- `fxTranslationPnl` de IBKR **no se usa**. Es un componente contable, no la atribución económica definida aquí. Puede servir como control cualitativo, nunca como entrada.

---

## 8. Consolidación multi-cuenta

La consolidación es **a nivel de valor, nunca de retorno**:

```
V(m,t) = Σ_{a∈A} V_a(m,t)        F(m,t) = Σ_{a∈A} F_a(m,t)
```

y luego se aplica §4 sobre los agregados. Nunca promediar los retornos de las cuentas.

Con esto, una transferencia A→B con `A,B ∈ A` se anula automáticamente: `−x + x = 0`. No hace falta lógica especial, sólo clasificación correcta.

### 8.1 Fecha del flujo: `reportDate`, nunca `settleDate` (crítico)

Validado empíricamente: IBKR imputa el flujo en la fecha de **`reportDate`**, no en la de liquidación. Usar `settleDate` produce errores catastróficos, no marginales:

| Período | `reportDate` | `settleDate` |
|---|---|---|
| 2024-08→2025-08 | 0,00 pb | +53,4 pb |
| 2023-01→2024-01 | 0,00 pb | **+6 942,6 pb** |

El caso de 2023 lo ilustra: una transferencia de −26.011,69 € con `reportDate = 20230424` y `settleDate = 20230426`. Imputada a la fecha de liquidación, el NAV ya ha caído en 26 k€ dos días antes de que el motor registre el flujo, y esos dos retornos diarios se disparan.

**Regla normativa:** `settleDate` se almacena pero **no se usa nunca en el cálculo**. El campo relevante es `reportDate`.

Esto simplifica también la consolidación multi-cuenta: si ambas patas de una transferencia interna llevan el mismo `reportDate` — que es el caso en los datos observados — la anulación `−x + x = 0` es automática y no hace falta ninguna ventana de emparejamiento. Se mantiene, no obstante, un **log de excepciones**: cualquier movimiento con contraparte ∈ `A` que no encuentre su pata simétrica en el mismo `reportDate` se registra para revisión manual en lugar de tratarse silenciosamente como externo.

---

## 9. Casos límite

| Situación | Tratamiento normativo |
|---|---|
| `V(t−1) + w·F(t) ≤ 0` | Retorno del día **indefinido**. Romper la cadena en segmentos y reportar el período como no calculable, con la fecha señalada. Prohibido devolver 0 silenciosamente. |
| `V(t−1) = 0` con `F(t) > 0` | Inicio de un nuevo segmento. `r(t)` se calcula sobre `F(t)` como base si `w = 1`; si `w = 0`, indefinido. |
| Cartera cerrada a cero y reabierta | Dos segmentos; se encadenan multiplicativamente si el usuario pide el período completo. |
| Día sin cotización (festivo de un mercado) | Arrastrar el último precio disponible. El FX debe arrastrarse igual, del mismo día. Nunca mezclar precio de `t` con FX de `t−1`. |
| FX ausente para una divisa en `t` | Error duro, no interpolar por defecto. Interpolación sólo si se activa explícitamente y se registra. |
| Instrumento sin precio pero con cantidad ≠ 0 | Error duro. Es una posición no valorada y corrompería el NAV. |
| Split / corporate action | Debe llegar reflejado en `quantity` y `price_local`. Si no, salto espurio en el retorno. Test de detección: retorno diario \|r\| > 20 % sin flujo → alerta. |

---

## 10. Batería de tests de aceptación

Cada test es binario y automatizable. La spec se considera implementada cuando los diez pasan.

| # | Test | Criterio |
|---|---|---|
| **T1** | TWR EUR vs. `ChangeInNAV/@twr` de IBKR | \|Δ\| ≤ 1 pb — **PASA con 0,00000 pb** en los 2 períodos de auditoría |
| **T1b** | TWR EUR 2025 vs. cadena de los `twr` diarios de IBKR | **PASA**: 2,879392 % por ambas vías |
| **T2** | Calibración de `w` y de la fecha de flujo | **CERRADO**: `w = 1`, `reportDate` |
| **T2b** | Reconciliación del flujo total vs. `depositsWithdrawals + internalCashTransfers + assetTransfers` | **PASA**: Δ < 1e−11 € en ambos períodos |
| **T3** | Identidad sin flujos (§6.1) | **PASA**: 2026 no tuvo flujos y cadena = forma cerrada a 0,0 pb |
| **T4** | Invariancia de estrategia (§7.2) | **PASA**: `R_estr(EUR)` = `R_estr(USD)` con Δ ≤ 1,8e−13 en los 4 períodos |
| **T5** | Cierre multiplicativo | **PASA**: Δ ≤ 2,2e−16 |
| **T6** | Composición por bloques | **PASA**: la serie 2023–2026 se ensambla de 4 ficheros sin discontinuidad |
| **T6b** | Reconstrucción del NAV desde cubos por divisa | **PASA**: residuo máx. 0,000021 € sobre 945 días |
| **T7** | Transferencia A→B con `A,B` seleccionadas | **PASA (v2.4)**: validado contra 5 cuentas reales — al consolidar, las 5 transferencias internas se excluyen (flujo neto interno = 0) |
| **T8** | Transferencia A→B con sólo `A` seleccionada | **PASA (v2.4)**: la cuenta que envía (U11843602), analizada sola, captura sus salidas (−13.000 €) como flujo de capital |
| **T9** | Cambio de moneda de análisis | Cantidades, precios locales y composición idénticos; sólo cambia la valoración |
| **T10** | Regresión congelada | *Golden values* de §13; cualquier refactor que los mueva falla el build |
| **T11** | CAGR de ventana de 1 año = acumulado de esa ventana | \|Δ\| ≤ 1 pb — falla con el convenio `N/252`, pasa con calendario (§14.3) |

---

## 11. Decisiones abiertas (bloquean el cierre de la spec)

**11.1 — Definición de NAV. CERRADA.** Se incluyen los devengos. NAV = `EquitySummaryByReportDateInBase/@total`.

**11.2 — Valor de `w`. CERRADA.** `w = 1`, flujo a inicio de día, imputado en `reportDate`. Reproduce IBKR exactamente; no hace falta subperiodificación intradía.

**11.3 — Ventana de emparejamiento. CERRADA (sin ventana).** Ver §8.1: ambas patas comparten `reportDate`. Se mantiene el log de excepciones.

**11.4 — Fecha de congelación del FX.** Este documento fija `t₀` del período analizado. Alternativa no elegida: congelación móvil diaria (equivale a una cobertura perfecta reajustada cada día), que sí sería aditiva entre subperíodos pero responde a otra pregunta económica. *Recomendación: mantener `t₀` y documentar la no-aditividad en la interfaz.*

**11.5 — Numerario interno `n`. CERRADA.** EUR, porque es la moneda base de la cuenta en los XML y evita una conversión en la carga. La elección no afecta a ningún resultado.

**11.6 — Cobertura de datos. CERRADA para 2023–2026.** Serie continua validada del **2022-12-30 al 2026-08-13**, 945 fechas, sin huecos y sin conflictos de NAV entre ficheros. Pendiente: **2021 y 2022**, necesarios para la ventana móvil de 5 años (§14.5) y para incluir un mercado bajista en la muestra. La cuenta se abrió el 2021-01-06, así que el dato existe. Ver §16.

**11.8 — Bootstrap del día inicial. CERRADA (v2.3).** El primer día de una extracción tiene NAV pero no snapshot de posiciones ni `ConversionRate`. Se reconstruía desde `ChangeInPositionValue/@priorPeriodValue` + `CashReportCurrency/@startingCash`, y un escalado de los cubos por divisa cuadraba el NAV bajo un FX arrastrado. Ese parche movía `R_estrategia` de 2023 entre 9,72 % y 9,93 % — **±21 pb**. **Resuelto con el backfill real 2021→2026**: cada bloque se extrae con un día hábil de margen por delante (§18.6), así el día de arranque llega con su `ConversionRate` propio (para 2023, `X(USD,20221230)=0,934240`) y reconstruye el NAV exacto. Con el dato real el escalado es inerte y la banda se cierra al extremo **bajo**: `R_estrategia(2023) = 9,7163 %`. El escalado era una muleta para un dato ausente; con el dato presente, sobra y no debe aplicarse.

**11.7 — NUEVA: definición del flujo por transferencias.** Fórmula validada, que reconcilia con `ChangeInNAV` a 1e−12:

```
flujo(Transfer) = cashTransfer × fxRateToBase + positionAmountInBase
```

Las transferencias de valores (`assetCategory = STK`) son flujo de capital por su `positionAmountInBase`, y aparecen en `ChangeInNAV/@assetTransfers`. Omitirlas desviaba el TWR de 2023 en 575 pb.

---

## 12. Resumen de la arquitectura de cálculo

```
XML IBKR
   ↓ normalización
positions / cash / fx / movements        ← divisa local, sin moneda base
   ↓ clasificación de movimientos (§3) + emparejamiento interno (§8.1)
serie diaria: V(m,t), F(m,t)             ← una función, parametrizada por m y por X
   ↓ §4 + §5
R_total(m)          [X real]
R_estrategia        [X congelado en t₀]   ← misma función, otra matriz FX
   ↓ §7.3
R_FX(m) = residuo
```

Un solo motor de valoración, un solo motor de TWR, dos invocaciones. No existen rutas de código separadas para EUR y USD, ni para real y FX-neutral. Esa es la condición que hace que los tests T3, T4 y T5 sean verificaciones reales y no tautologías.


---

---

## 13. Resultados validados (*golden values*)

Serie continua 2022-12-30 → 2026-08-13, cuenta U7790974. Todos los TWR EUR reproducen el `ChangeInNAV/@twr` de IBKR con **0,00000 pb**.

### 13.1 Por año natural

| Período | TWR EUR | TWR USD | Estrategia | Efecto divisa (EUR) |
|---|---|---|---|---|
| 2023 | +7,497979 % | +11,6035 % | +9,7163 % | −2,0218 % |
| 2024 | +29,524351 % | +21,5302 % | +21,4391 % | +6,6578 % |
| 2025 | +2,879392 % | +16,7450 % | +16,7209 % | −11,8587 % |
| 2026 YTD | +39,087122 % | +36,5230 % | +36,5246 % | +1,8770 % |
| **Histórico** | **+99,24 %** | **+116,18 %** | **+113,27 %** | **−6,58 %** |

`R_estrategia` es idéntica se analice en EUR o en USD (T4). *(Corregido en v2.3: la Estrategia de 2023 y del histórico llevaban un artefacto de escalado del día de arranque; ver §11.8. Los valores anteriores eran 9,9341 % / 113,80 %.)*

### 13.2 Riesgo por año (serie estrategia)

| Año | Vol | Vol bajista | Sharpe* | Sortino* | Máx. DD | VaR 95 | Días + |
|---|---|---|---|---|---|---|---|
| 2023 | 15,46 % | 11,04 % | 0,63 | 0,88 | −14,84 % | −1,63 % | 56,2 % |
| 2024 | 27,24 % | 18,17 % | 0,78 | 1,17 | −24,54 % | −2,29 % | 51,9 % |
| 2025 | 22,30 % | 16,06 % | 0,75 | 1,04 | −20,13 % | −2,36 % | 53,6 % |
| 2026 YTD | 32,83 % | 21,85 % | n/d | n/d | −14,54 % | −2,85 % | 57,8 % |
| Histórico | 24,33 % | 16,67 % | 0,96 | 1,40 | −24,40 % | −2,34 % | 54,7 % |

\* con `rf = 0`. Con €STR realista bajan en torno a 0,10–0,15. Ver §14.3. *(Sharpe/Sortino corregidos en v2.4 al convenio de CAGR por calendario de §14.3; una versión previa los imprimió con el convenio antiguo `N/252` y salían algo más bajos — vol, MaxDD y VaR no dependen del convenio.)*

Máximo drawdown en total EUR: **−25,89 %**, frente a −24,40 % de la estrategia. En 2025 la brecha llega a 5,7 puntos (−25,85 % vs −20,13 %): caída que no vino de las posiciones sino del tipo de cambio.

### 13.3 Ventanas móviles a 2026-08-13

| Ventana | Acum. EUR | Anual. EUR | Acum. estrategia | Anual. estrategia | Vol | Máx. DD |
|---|---|---|---|---|---|---|
| 1 año | +55,28 % | +55,32 % | +52,94 % | +52,99 % | 29,88 % | −14,54 % |
| 3 años | +74,65 % | +20,44 % | +84,98 % | +22,77 % | 25,97 % | −24,49 % |
| 5 años | — | — | — | — | — | — |
| Total (3,62 años) | +99,24 % | +20,98 % | +113,27 % | +23,28 % | 24,33 % | −24,40 % |

### 13.4 Descomposición del riesgo por divisa

Sobre log-retornos diarios del histórico completo:

| | Valor |
|---|---|
| Volatilidad total EUR | 24,68 % |
| Volatilidad estrategia | 24,34 % |
| Volatilidad componente divisa | 6,53 % |
| Correlación estrategia / divisa | −0,082 |
| Reparto de varianza | estrategia 97,3 % · divisa +7,0 % · cruzado −4,3 % |

**Lectura:** la divisa mueve el resultado ±12 puntos en un año pero apenas añade 34 pb de volatilidad, porque la correlación negativa compensa. Es un problema de distorsión de la lectura, no de riesgo.

### 13.5 Flujos por año (control)

| Año | Nº flujos | Neto | Mayor |
|---|---|---|---|
| 2023 | 5 | −23.794,03 € | −26.011,69 € |
| 2024 | 6 | +9.733,76 € | +30.000,00 € |
| 2025 | 6 | −33,75 € | +5.000,00 € |
| 2026 | 0 | 0 € | — |

---

## 14. Capa de métricas de rentabilidad y riesgo

### 14.1 Serie de entrada

Todas las métricas se calculan sobre la **serie de retornos diarios encadenados** `{r(t)}` de §4, nunca sobre el NAV.

> **Regla dura:** el drawdown se mide sobre el índice TWR `I(t) = Π(1+r(s))`, no sobre el patrimonio. Sobre el NAV, la transferencia de −26.011 € de abril de 2023 aparece como un drawdown del ~38 % que jamás existió.

> **Segunda regla dura:** el drawdown se calcula siempre sobre la serie **diaria**, aunque el gráfico se pinte con puntos mensuales. Una curva mensual subestima sistemáticamente la caída máxima.

Toda pantalla de riesgo muestra **dos series en paralelo**:

| Columna | Serie | Responde a |
|---|---|---|
| **Total** | `r(m,t)` con FX real | "¿Cuánto ha rendido mi dinero?" |
| **Estrategia** | `r̃(t)` con FX congelado | "¿Cuánto ha rendido mi sistema?" |

La columna Estrategia es invariante a la moneda de análisis (§7.2); la columna Total no. Al cambiar EUR ↔ USD sólo se mueve la primera. Ese contraste es el argumento del producto y no debe explicarse con texto: debe verse.

### 14.2 Definiciones

Con `N` observaciones, `P = 252`, `Y` = años de calendario transcurridos y tasa libre de riesgo `rf`:

```
Acumulado      R = Π(1+r(t)) − 1
CAGR           (1+R)^(1/Y) − 1                    Y = días naturales / 365,25
Volatilidad    σ = stdev({r}) · √P
Vol. bajista   σ⁻ = √( Σ min(r,0)² / N ) · √P
Sharpe         (CAGR − rf) / σ
Sortino        (CAGR − rf) / σ⁻
MaxDD          min( I(t)/max_{s≤t} I(s) − 1 )     sobre serie diaria
Calmar         CAGR / |MaxDD|
VaR 95         percentil 5 de {r}
CVaR 95        media de {r} por debajo del VaR 95
```

### 14.3 Convenios fijados

**El tiempo se cuenta de dos maneras distintas y no son intercambiables.**

- **CAGR: calendario.** `Y = días naturales / 365,25`. El convenio anterior (`N/252`) estaba mal: con 261 sesiones reales al año, una ventana de 12 meses exactos daba un "anualizado" *menor* que el acumulado — +52,94 % acumulado frente a +52,94 %→+50,72 % anualizado. Una rentabilidad a un año anualizada tiene que ser igual a la acumulada, por definición (test T11).
- **Volatilidad: observaciones.** `√252` sigue siendo correcto, porque cuenta sesiones y no calendario.
- **`rf` es un parámetro obligatorio, no cero por defecto.** Con `rf = 0` el Sharpe de la estrategia sale 0,92; con €STR realista baja a ~0,80. Fuente: €STR para EUR, SOFR para USD, curva diaria.
- **Períodos con `Y < 1`:** se muestra el acumulado sin anualizar y se ocultan CAGR, Sharpe, Sortino y Calmar. El 2026 YTD daba Calmar 4,32 anualizando 161 días: una cifra sin significado. La interfaz marca el período con una etiqueta explícita, no lo oculta.

### 14.4 Descomposición del riesgo por divisa

Sobre log-retornos, `l_total = l_estrategia + l_divisa` es exacta. Por tanto:

```
Var(total) = Var(estrategia) + Var(divisa) + 2·Cov(estrategia, divisa)
```

Resultados medidos en §13.4. Esta descomposición merece bloque propio en la interfaz porque **contradice la conclusión que sugiere la tabla de rentabilidad**: la divisa parece el gran riesgo y no lo es.

### 14.5 Ventanas móviles

Ventanas estándar terminadas en la última fecha disponible: **1 año, 3 años, 5 años y total**. Se calculan sobre calendario, no sobre número de sesiones.

**Regla de vacíos:** una ventana sin histórico suficiente se muestra **vacía y con la razón escrita al lado** ("Sin datos · requiere histórico desde ago 2021"). No se oculta la fila ni se rellena con el período disponible más largo. Ocultarla hace pensar que la ventana no existe en el producto; rellenarla con menos histórico es directamente una mentira.

### 14.6 Gráfico de curva de capital

- Se dibuja el **índice TWR normalizado a 100**, nunca el patrimonio. Con el patrimonio, el depósito de 30.000 € de 2024 aparece como un salto vertical que no es rentabilidad.
- Dos líneas: Total en la moneda de análisis y Estrategia. La divergencia entre ambas *es* el efecto divisa acumulado.
- Puntos mensuales para el trazo; **las métricas nunca se leen del gráfico**, siempre de la serie diaria (§14.1).

---

## 15. Comparación contra benchmark

### 15.1 El dato no está en IBKR

El FlexQuery no entrega series de índices. Hace falta una fuente externa de precios. Requisitos:

1. **Retorno total, no índice de precios.** Usar el índice de precios del S&P 500 en vez del Total Return infravalora el benchmark en ~1,8 %/año y regala alfa falso. Es el error más caro de esta sección.
2. **Serie diaria** alineable con las fechas del motor.
3. **Histórico** desde al menos 2022-12-30.

Opción preferente: la **Client Portal Web API del propio IBKR** (`/iserver/marketdata/history`). Misma fuente de precios que las valoraciones, sin alta en otro proveedor, y coherente con las marcas que ya usa el motor. Alternativas: Stooq (CSV gratuito), EOD Historical Data, Alpha Vantage.

### 15.2 Regla de coherencia de divisa (crítica)

El benchmark debe compararse contra la serie **equivalente en tratamiento de divisa**:

| Serie de la cartera | Serie del benchmark |
|---|---|
| Estrategia (FX-neutral) | Benchmark **en su divisa local** |
| Total en EUR | Benchmark **convertido a EUR con FX diario** |
| Total en USD | Benchmark en USD |

Comparar la estrategia FX-neutral contra un benchmark convertido a euros mezcla dos efectos y produce un alfa que es puro ruido cambiario.

### 15.3 Alineación de calendarios

Las fechas del motor son las que reporta IBKR (~261/año), que no coinciden con el calendario de ningún índice concreto: hay festivos europeos en los que el mercado estadounidense abre y viceversa.

- **Regla:** las fechas del motor mandan. El benchmark se reindexa a ellas.
- **Huecos:** arrastrar el último precio disponible genera días de retorno exactamente 0 que **amortiguan artificialmente la volatilidad del benchmark**. Debe contabilizarse y reportarse el número de días arrastrados; si supera ~3 % de las observaciones, la comparación de volatilidad no es fiable.

### 15.4 Métricas relativas

```
Beta                 Cov(r_p, r_b) / Var(r_b)
Alfa de Jensen       CAGR_p − [ rf + β·(CAGR_b − rf) ]
Tracking error       stdev(r_p − r_b) · √P
Information ratio    (CAGR_p − CAGR_b) / TE
Captura alcista      media(r_p | r_b>0) / media(r_b | r_b>0)
Captura bajista      media(r_p | r_b<0) / media(r_b | r_b<0)
Correlación          corr(r_p, r_b)
```

### 15.5 Elección del benchmark

La cartera observada es prácticamente 100 % renta variable estadounidense de perfil growth/momentum de beta alta (APP, AXON, CRWD, PLTR, GEV, VST, NRG, DASH). Contra el S&P 500 el alfa saldrá inflado por exposición a factor, no por habilidad.

**Regla normativa:** la aplicación exige **al menos dos benchmarks** —uno amplio (S&P 500 TR o ACWI TR) y uno de estilo comparable (Nasdaq-100 TR o un índice momentum)— y muestra la beta junto al alfa siempre. Un alfa sin su beta al lado es un número engañoso.

### 15.6 Proveedor: Yahoo Finance. DECIDIDA.

Implementado en `q4_benchmark.py`. Notas operativas obligatorias:

- **`auto_adjust=True` es obligatorio** y está fijado en el código. Es lo que convierte el precio en proxy de retorno total.
- **No usar `^GSPC`**: es índice de precios sin dividendos. El catálogo por defecto es `SPY` (amplio) + `QQQ` / `MTUM` (estilo).
- **El ajuste de Yahoo se recalcula hacia atrás** con cada dividendo, así que dos descargas en fechas distintas no dan la misma serie. La caché en CSV no es una optimización, es un requisito de reproducibilidad: se versiona y sólo se refresca la cola.
- El módulo separa `fetch_benchmark()` (con red) de `load_benchmark()` (sólo caché), para que el cálculo sea reproducible aunque Yahoo caiga o cambie de formato.

**Consecuencia conocida del calendario (§15.3):** el motor reporta ~261 sesiones al año y el NYSE ~251. Son ~10 días al año arrastrados, en torno al **4 % de las observaciones**, por encima del umbral del 3 %. La bandera `vol_fiable` saldrá en `False` por defecto y no es un fallo. Para comparación contra benchmark se recomienda **intersección de calendarios** en lugar de arrastre: se pierden ~10 observaciones al año de 261 y se gana una beta que significa algo.

---

## 16. Estado del proyecto y trabajo pendiente

### 16.1 Cerrado y validado

Motor de TWR, convención de flujos, moneda de análisis, escenario FX-neutral, atribución Estrategia/FX y capa de métricas. Tests T1–T6b y T9, T11 pasan sobre datos reales de la cuenta U7790974.

### 16.2 Consolidación multi-cuenta — VALIDADA (v2.4)

**T7 y T8 validados** contra una FlexQuery real de **5 cuentas** (U7790974, U11843602, U14729981, U19161516, U7411263), rango 2026. El parser separa las 5 cuentas (cada sección trae su `accountId`); la consolidación es a nivel de valor (§8) y las transferencias internas se anulan por exclusión de contraparte (§8.1). Al validarlo se corrigieron **dos bugs** que la implementación arrastraba:

- **Bootstrap multi-cuenta**: sólo se reconstruía el día de arranque del primer statement; las demás cuentas quedaban sin componer. Ahora se recorre cada statement.
- **Dedup de transferencias**: las dos patas de una transferencia interna comparten `transactionID` en cuentas distintas; deduplicar sólo por `transactionID` tiraba una pata (el consolidado salía bien, pero la cuenta que envía, analizada sola, perdía sus salidas). Se deduplica por `(transactionID, cuenta)`.

Requisito de configuración: la FlexQuery debe entregar **detalle por cuenta**, no un statement consolidado. Pendiente aún: **ingerir el histórico completo** de las cuentas ligadas en el pipeline (backfill + job diario); lo validado es la lógica de consolidación sobre un bloque de 2026.

### 16.3 Extracciones pendientes

| Bloque | Rango | Desbloquea | Estado |
|---|---|---|---|
| 2021 | 2020-12-28 → 2021-12-31 | Ventana móvil de 5 años | **Hecho** (backfill) |
| 2022 | 2022-01-01 → 2022-12-31 | Mercado bajista en la muestra | **Hecho** (backfill) |
| Re-extracción 2023 | desde 2022-12-28 | Elimina los ±21 pb de §11.8 | **Hecho** — resuelto a 9,7163 % |
| Cuentas ligadas | mismos rangos | Tests T7 y T8 | **Lógica validada (v2.4)**; falta backfill del histórico completo |

El backfill real 2021→2026 (v2.3) trajo 2021 y 2022 en una serie continua de **1466 días (2020-12-31 → 2026-08-14, 0 conflictos de NAV)**. La ventana de 5 años queda disponible. Dos avisos de los datos de 2021: (a) la cuenta arranca en NAV=0 y se fondea el 2021-01-08, así que el análisis empieza en el primer día fondeado, no en el cero; (b) el nocional de un corto de futuros S&P (sólo 2021) se excluye de los cubos (§1/parser).

Nota sobre el drawdown: con 2022 ya en la muestra, el máximo drawdown de la estrategia sobre la serie completa pasa a **−25,88 %** e incorpora mercado bajista real; deja de salir íntegramente de un tramo alcista.

### 16.4 Anomalía documentada

El documento de trabajo previo daba **+38,2871 %** para 2026 YTD. Ese valor **no corresponde a ningún cierre de mercado**: cae entre el 11 de agosto (+36,85 %) y el 12 (+38,57 %). Casi con seguridad era una lectura intradía. El valor correcto a cierre del 13-08-2026 es **+39,087122 %**. Los otros tres años del documento previo sí eran correctos.

### 16.5 Orden de trabajo propuesto

1. Implementar §18 y §19 (ingesta y sincronización) — aguas arriba de todo lo demás.
2. Escribir el motor de producción sobre la spec cerrada (§0–§12).
3. Congelar los *golden values* de §13 como tests de regresión.
4. Backfill de 2021–2022 y de las cuentas ligadas, ya automático.
5. Implementar §14 y el panel de §17.
6. Implementar §15 con Yahoo, empezando por el módulo de descarga y caché.

---

## 17. Panel de rentabilidad y riesgo

Especificación de la pantalla principal. El orden importa: va de lo agregado a lo detallado.

### 17.1 Estructura

| Bloque | Contenido | Regla |
|---|---|---|
| Cabecera | Rango de fechas, nº de sesiones, selector EUR/USD | El selector es global |
| KPI | Acumulado total · Acumulado estrategia · Anualizado estrategia · Máx. drawdown | 4 tarjetas |
| Curva | Índice TWR a 100, dos líneas | §14.6 |
| Por año | Total · Estrategia · Divisa · Vol · Máx. DD | Fila por año natural |
| Ventanas | 1a · 3a · 5a · Total | Vacíos explícitos (§14.5) |
| Riesgo divisa | Reparto de varianza y correlación | §14.4 |
| Benchmark | Alfa, beta, captura | §15, cuando haya datos |

### 17.2 Reglas de presentación

- **El selector EUR/USD no debe mover la columna Estrategia.** Si se mueve, hay un bug (T4).
- **Los períodos no anualizables llevan etiqueta visible**, no se ocultan.
- **Signos y color:** verde/rojo sólo en rentabilidad y efecto divisa. Volatilidad y drawdown no llevan color de signo; el drawdown siempre en rojo por ser negativo por construcción.
- **Cifras tabulares** en todas las columnas numéricas, para que las magnitudes se comparen verticalmente de un vistazo.
- **Nunca mostrar un Sharpe sin decir qué `rf` se usó**, ni un alfa sin su beta al lado (§15.5).

---

## 18. Ingesta desde IBKR (Flex Web Service)

La aplicación descarga los XML directamente. El usuario entrega un **token** y un **Query ID**; no vuelve a tocar el Client Portal.

### 18.1 Protocolo

Dos pasos, versión 3 obligatoria (`v=3`; sin ella IBKR asume la 2).

```
1. GET /AccountManagement/FlexWebService/SendRequest?t={token}&q={queryId}&v=3[&fd=..&td=..]
   -> <FlexStatementResponse><Status>Success</Status>
      <ReferenceCode>..</ReferenceCode><Url>..</Url>

2. GET {Url}?t={token}&q={referenceCode}&v=3
   -> el XML del statement, o <Status>Warn</Status><ErrorCode>1019</ErrorCode>
      si aún se está generando -> reintentar con backoff
```

**`fd` / `td` son el hallazgo que define la arquitectura.** Sobrescriben el rango de fechas de la query guardada, hasta 365 días. La query se configura **sin período**, y es el motor quien pide cada bloque. Todo el troceado manual del histórico desaparece.

### 18.2 Límites de ritmo

`/SendRequest` admite **1 petición por segundo, máximo 10 por minuto**. Un backfill de 6 años son 6 bloques; con cuatro cuentas, 24. Hace falta un limitador con ventana deslizante, no un `sleep` fijo — implementado en `_Pacer`.

### 18.3 Errores

| Código | Tratamiento |
|---|---|
| 1009, 1018, 1019, 1021 | Reintentable con backoff exponencial |
| 1012 | **Token caducado o revocado.** Error de usuario: pedir token nuevo, no reintentar |
| 1003 | Query ID inválido o ajeno al token |
| 1013 | La query fue borrada en Client Portal |
| 1020 | Petición inválida: formato de `fd`/`td` o rango |

El 1012 necesita su propia excepción porque es el único que requiere acción del usuario. Reintentarlo consume cuota y no arregla nada.

### 18.4 El token es una credencial

Viaja en la query string, lo que tiene tres consecuencias no negociables:

- **Nunca registrar la URL completa.** Las librerías HTTP la sacan por defecto en logs y trazas de error. Redactar antes de cualquier `log` o reporte de excepción.
- **Cifrado en reposo**, con clave fuera de la base de datos.
- **Nunca en el frontend.** La descarga ocurre en servidor; el navegador no ve el token en ningún momento.

Da acceso de lectura a los extractos completos de la cuenta. Rotación recomendada cada 90 días.

### 18.5 Lo que la aplicación NO puede automatizar

El Flex Web Service **sólo ejecuta queries ya guardadas**; no permite crearlas. El usuario tiene que configurar la suya en Client Portal con las 14 secciones de §18.7. Es el único paso manual del onboarding y hay que diseñarlo asumiendo que se hará mal.

**Validación obligatoria en el alta:** descargar un bloque corto y comprobar las etiquetas XML presentes. Las etiquetas no dependen del idioma de la interfaz, así que la comprobación funciona con el Client Portal en cualquier idioma — a diferencia de una guía escrita con los nombres de los menús. Implementado en `validate_query()`, que además detecta el error más probable: una query que devuelve un único snapshot de cierre en vez de posiciones diarias.

### 18.6 Backfill e incremental

**Backfill.** Ventanas de ≤365 días con **5 días naturales de margen por delante** del primer bloque. Ese margen resuelve §11.8: el día inicial llega con su snapshot de posiciones y su `ConversionRate` completos, y los ±21 pb de incertidumbre de la estrategia de 2023 desaparecen (resuelto en v2.3: el `ConversionRate` real hace inerte el escalado que los provocaba). **Aviso operativo verificado contra IBKR real (v2.3):** el corte `td` de cada bloque debe caer en día hábil; pedir un `td` sin cierre (fin de semana, festivo, o más allá del último cierre generado) devuelve `1003` para todo el bloque, no un truncado.

**Incremental.** Cada sincronización solapa **10 días hacia atrás**. IBKR revisa statements ya emitidos: dividendos reclasificados, comisiones ajustadas, retenciones corregidas. Sin solape esas correcciones no llegan nunca.

**Consecuencia para el normalizador:** las fechas repetidas entre bloques son la norma, no la excepción. Deduplicar por `transactionID`, y para el NAV comparar `(account, reportDate)` y **levantar alerta si dos bloques discrepan** en vez de sobrescribir en silencio. Sobre los ficheros validados esa comprobación dio 0 conflictos en 945 días.

### 18.7 Almacenamiento del crudo

El XML descargado se guarda **inmutable**, con nombre `{queryId}_{fd}_{td}_{timestampUTC}_{sha256:12}.xml`, y no se modifica jamás.

**Regla:** un fallo de parseo se corrige **reprocesando el crudo**, nunca volviendo a descargar. IBKR puede haber cambiado datos históricos entre descargas, y una redescarga silenciosa rompería los *golden values* de §13 sin dejar rastro. El hash en el nombre permite detectar exactamente eso.

Secciones que la query debe traer, por etiqueta XML:

`StmtFunds` · `ChangeInNAV` · `ChangeInPositionValues` · `CashReport` · `EquitySummaryInBase` · `OpenPositions` · `FxPositions` · `PriorPeriodPositions` · `Trades` · `TradeTransfers` · `FxTransactions` · `CashTransactions` · `Transfers` · `ConversionRates`

### 18.8 Arquitectura revisada

```
token + queryId
   ↓ §18.1  FlexClient
XML crudo inmutable  ──────────────► almacén append-only
   ↓ §1     parser
positions / cash / fx / movements
   ↓ §2–§7  motor
series diarias, TWR, atribución
   ↓ §14    métricas   ↓ §15  benchmark
   ↓ §17    panel
```

El crudo es la frontera. Aguas arriba hay red, credenciales y errores transitorios; aguas abajo todo es determinista y reproducible desde ficheros. Los tests de §13 corren enteros aguas abajo, sin tocar la red.

---

## 19. Sincronización: backfill inicial e incremental diario

**Una sola Flex Query para ambos modos.** Backfill e incremental son la misma llamada con distinto rango `fd`/`td`. No hay dos integraciones ni dos configuraciones que el usuario pueda desalinear.

### 19.1 Los dos modos

| | Backfill | Incremental |
|---|---|---|
| Cuándo | Una vez, tras validar el alta | Cada día |
| Rango | Ventanas de ≤365 días desde el inicio del histórico | `watermark − 10 días` → hoy |
| Volumen | ~10–14 MB por bloque anual | ~400 KB |
| Generación en servidor | Lenta: el sondeo necesita paciencia | Rápida |

### 19.2 El backfill es reanudable

Estado persistido **después de cada ventana**, no al final. Seis años son seis peticiones; si la cuarta falla, las tres primeras no se repiten. Cada bloque anual tarda en generarse en el servidor de IBKR y consume cuota del límite de 10 peticiones por minuto.

### 19.3 Solape de 10 días

IBKR **revisa extractos ya emitidos**: dividendos reclasificados, comisiones ajustadas, retenciones corregidas. Sin solape, esas correcciones no llegan nunca.

Efecto secundario útil: el solape elimina el problema de bootstrap de §11.8 en los incrementales. Ese problema sólo afecta al primer día del histórico; en un incremental el primer día del bloque ya se conoce de la sincronización anterior, con su composición por divisa completa. No hace falta ningún caso especial.

### 19.4 El watermark sale de los datos

`watermark = max(reportDate)` **realmente ingerido**, nunca el `td` solicitado. Si se pide hasta hoy y IBKR aún no ha generado el cierre del día, devuelve hasta ayer sin avisar. Guardar el `td` pedido dejaría un hueco permanente que el solape del día siguiente no cubriría.

### 19.5 El recálculo es siempre completo

Prohibido añadir sólo el día nuevo al final de la cadena. Una corrección dentro de la ventana de solape cambia un retorno pasado y arrastra todo lo posterior; además, el escenario FX-neutral congela el tipo de cambio en `t₀` del período, así que cualquier cambio obliga a rehacer el período entero. Recalcular 945 días son milisegundos.

### 19.6 Canario de regresión en producción

Los años cerrados son inmutables: 2023 dio **+7,497979 %** y no puede cambiar. Cada sincronización diaria recalcula los años cerrados y los compara contra los *golden values* de §13 con tolerancia de 1 pb.

Si se mueven, o IBKR ha reexpresado datos históricos o hay un bug en el motor. En ambos casos se quiere saber ese mismo día. Es una prueba de regresión que corre sola en producción y no cuesta nada.

Complementariamente, las fechas repetidas entre bloques se comparan por NAV: un conflicto se **registra y alerta**, nunca se sobrescribe en silencio. Sobre los ficheros validados el resultado fue 0 conflictos en 945 días.

### 19.7 Política de fallos

| Situación | Tratamiento |
|---|---|
| Fin de semana, festivo, cierre no generado | `no_new_data`. **No es un error**, la tarea termina en silencio |
| 1009 / 1018 / 1019 / 1021 | Reintento con backoff exponencial |
| 1012 token caducado | **Pausa** la sincronización y avisa al usuario. Reintentar consume cuota y no arregla nada |
| Otros errores Flex | Se registra, se reintenta al día siguiente; alerta tras N fallos consecutivos |

El crudo se escribe **antes** de reprocesar. Si la descarga se corta a medias no queda estado parcial en la base de datos.

### 19.8 Ventana horaria y expectativa del usuario

Los extractos se generan en el proceso nocturno de IBKR. Una ejecución a media mañana CET recoge con margen el cierre estadounidense de la víspera. La tarea debe ser tolerante: si el día pedido aún no está, no falla, lo recogerá mañana vía solape.

**El Flex Web Service no entrega datos intradía.** El panel está siempre "a cierre de", nunca a tiempo real, y debe etiquetarlo de forma explícita. Es exactamente el origen de la anomalía de §16.4: el +38,2871 % del documento previo no correspondía a ningún cierre porque era una lectura intradía.

---

## 20. Operaciones — P&L por ticker

Pestaña nueva, separada de §17 (rebautizado **Métricas**). Responde a "¿en qué estoy ganando dinero?", no a "¿cuánto ha rendido mi cartera?" — por eso vive aparte: usa una unidad distinta (divisa, no TWR) y una fuente de verdad distinta (`fifoPnlRealized` de IBKR, no el motor de §4–§7).

### 20.1 Por qué divisa y no porcentaje agregado

Sumar contribuciones en % a lo largo de un periodo no cierra con el TWR total: el TWR compone geométricamente y hace falta un algoritmo de suavizado (Cariño/Menchero) para repartir el término de interés compuesto. En divisa, la ganancia de cada posición es una resta contable (`valor_final − valor_inicial − inversión_neta`) y la suma de todas las posiciones cierra con la ganancia total de la cartera **por construcción**, sin residuo ni orden. Es la misma razón por la que §7 define la divisa como residuo en vez de calcularla aparte: cerrar por construcción, no por cuadre a posteriori.

### 20.2 Fuente del importe: `fifoPnlRealized`, nunca recalculado

IBKR ya hace el emparejamiento FIFO y publica el resultado neto de comisión en `Trade.fifoPnlRealized`. **No se recalcula el importe** — se usa tal cual. Validado contra el histórico real completo (2021-01-28 → 2026-08-18, 4.525 operaciones únicas, 5 cuentas): con hora de ejecución real y splits aplicados, el 88 % de las combinaciones cuenta/símbolo reconcilian exactas (<0,02 €) reconstruyendo FIFO nosotros mismos y comparando contra ese campo; el 12 % restante traza a un único origen identificado (§20.5), no a fallos del método.

Nuestro propio FIFO (fecha+hora real, ordenado por `dateTime`) se usa **solo** para decidir qué mostrar como fecha/precio de entrada al desplegar una operación — nunca para el importe.

### 20.3 Campos del Flex Query necesarios

Ampliación validada sobre el Query original (§18.7): sección **Operaciones** con todos los campos (en particular `accountId`, `dateTime` con hora, `underlyingSymbol`, `putCall`, `strike`, `expiry`, `multiplier` — antes solo llegaba `tradeDate`, sin hora, y sin subyacente para derivados), más las secciones **Información de instrumento financiero** (`SecurityInfo`, respaldo de subyacente/multiplicador por `conid`), **Ejercicios, asignaciones y vencimiento de opciones** (`OptionEAE`) y **Acciones corporativas** (`CorporateAction`, imprescindible para splits — sin ella NVDA/AVGO/KLAC no reconciliaban, error de hasta el 15 % del volumen).

Cobertura verificada sobre el histórico completo: `dateTime` y `accountId` al 100 %; `underlyingSymbol` al 100 % en FUT y OPT.

### 20.4 Agrupación

Por `(cuenta, símbolo)`, salvo futuros y opciones que se agrupan por `(cuenta, underlyingSymbol)` — todos los vencimientos de un mismo subyacente son una sola línea de agregado, con cada contrato como operación individual desplegable dentro.

**El tipo de instrumento (acción/opciones/futuros) forma parte de la clave de agrupación, no sólo de la etiqueta.** El mismo símbolo puede operarse directamente y a la vez ser subyacente de opciones o futuros — comprobado con datos reales: `IWM` y `TLT` se operan como acción y como subyacente de opciones en las mismas cuentas. Sin el tipo en la clave, ambos se emparejarían en el mismo FIFO (sin sentido: son instrumentos distintos, con `conid` distinto) y se sumarían en el mismo agregado, ocultando cuál de los dos está ganando o perdiendo. La vista de Operaciones muestra el tipo explícitamente (columna en el agregado, en la etiqueta del selector de detalle).

### 20.5 Cuando no hay operación de apertura: `Transfer` y el caso "no determinado"

Una posición puede llegar a la cuenta por transferencia (interna entre cuentas propias, o desde otro bróker) en vez de por compra. El FIFO no encuentra lote de apertura porque no lo hay. Regla: si falta lote, se busca en `movements` una fila `TRANSFER_*` de ese `conid`/cuenta anterior a la fecha de cierre y se usa como origen (fecha, cantidad, precio derivado de `positionAmountInBase`). Si tampoco existe, la operación se marca `origen: no_determinado` — el importe (`fifoPnlRealized`) se muestra igual, siempre correcto; lo que no se muestra es un precio/fecha de entrada inventado.

Validado contra datos reales: la mayoría de los grandes desajustes de la reconstrucción FIFO (ADYEN, ETSY, JD, ZM, ALFEN, FTK, TSLA, 1S3, entre otros) trazan a una única transferencia INTERCOMPANY del 08/01/2021. Un caso adicional (`EIDF`) es una transferencia interna entre dos cuentas propias en 2023 — mismo tratamiento.

### 20.6 Splits

`CorporateAction` con `type="FS"` y descripción `"SPLIT N FOR M"`. Antes de procesar cada operación, se aplican los splits de ese `(cuenta, símbolo)` con fecha anterior a la operación, ajustando los lotes abiertos: `cantidad *= N/M`, `precio *= M/N`. Sin esto, un split rompe la continuidad de cantidad/precio y el FIFO compara peras con manzanas (comprobado: NVDA y AVGO, ambos split 10:1 en 2024, eran el mayor origen de desajuste antes de aplicar esta corrección).

### 20.7 Reparto de una operación de cierre entre varios lotes

Cuando un cierre consume más de un lote de apertura distinto (FIFO por lote, no por episodio — decisión explícita: más preciso, valida contra `fifoPnlRealized`), `fifoPnlRealized` y la comisión de esa operación de cierre son **un único número por ejecución**, no uno por lote. Se reparten entre las filas resultantes **proporcionalmente a la cantidad emparejada de cada lote** — la suma de las filas repartidas sigue siendo exactamente el número de IBKR, no una aproximación.

### 20.8 Alcance del periodo: dos referencias, no una

Decisión explícita: los metadatos de la operación (fecha/precio/comisión de entrada) son siempre los **reales**, aunque la posición se abriera antes del periodo seleccionado. El importe de ganancia atribuido al periodo, en cambio, está **acotado al periodo**:

- Si entrada y salida caen dentro del periodo: el importe es `fifoPnlRealized` (o su reparto proporcional, §20.7) tal cual — cae dentro del caso del §20.2, IBKR es la fuente.
- Si la posición ya estaba abierta antes del inicio del periodo (entrada real anterior a `d0`): el importe del periodo **no puede ser** `fifoPnlRealized` completo — incluiría revalorización de antes del periodo. Se recalcula como `cantidad_emparejada × multiplicador × (precio_salida − precio_en_d0)`, con `precio_en_d0` tomado de `positions` (arrastre del último valor conocido, igual que la matriz FX de §7). Es aritmética simple sobre precios ya conocidos, no una reconstrucción de FIFO — no rompe §20.2.
- Si la posición sigue abierta al final del periodo: mismo cálculo, con `precio_en_d1` (o el precio de mercado más reciente) como referencia de salida, y estado `abierta`.

### 20.9 Estructura de las dos tablas

**Detalle** (una fila por operación, incluye `cuenta` — no tiene sentido consolidarla, cada operación ocurrió en una cuenta concreta):

```
cuenta, símbolo, subyacente, dirección (largo/corto), estado (abierta/cerrada),
fecha_entrada, precio_entrada, comisión_entrada, origen_entrada (trade | transfer | no_determinado),
fecha_salida, precio_salida, comisión_salida,
ganancia_local (§20.8), pct_revalorización (mismo criterio de referencia que la ganancia), divisa
```

**Agregado por ticker** (sin `cuenta` — suma todas las que apliquen si hay varias cuentas en la vista consolidada):

```
símbolo (o subyacente), revalorización_local, dividendos_local, comisiones_local,
total_local, capital_base_local, pct_revalorización (= revalorización_local / capital_base_local,
ponderado por capital — nunca media simple de los % individuales), divisa
```

Dividendos: `CashTransaction` con `type` en {Dividends, Payment In Lieu Of Dividends}, netos de Withholding Tax, atribuidos por `symbol`/`conid`, en el periodo seleccionado. Comisiones del agregado: suma de las comisiones de entrada/salida de las operaciones incluidas — ya están en cada fila de detalle, no es un cálculo nuevo.

---

## 21. Cartera — posiciones abiertas y efectivo

Pestaña nueva. A diferencia de Operaciones (§20), no es una vista por periodo — es una foto a la última fecha disponible de cada cuenta: qué tienes ahora y cuánto vale.

### 21.1 Fuente del importe: `OpenPosition`, no nuestro FIFO

`OpenPosition` de IBKR ya trae `costBasisPrice`, `costBasisMoney` y `fifoPnlUnrealized` — su propia contabilidad de coste base y plusvalía latente, con sus propios ajustes de splits y transferencias ya aplicados. Igual que §20.2 con `fifoPnlRealized`, no se recalcula: se usa tal cual. Esto evita heredar aquí los casos `no_determinado` de §20.5 — comprobado contra la última fecha de las 5 cuentas (18/08/2026, 27 posiciones): cobertura 100 % de ambos campos, en STK y FUT.

La fecha de entrada mostrada es un dato secundario de trazabilidad, no la base del cálculo: sale del motor de Operaciones (lote abierto más antiguo para esa cuenta/contrato) y puede faltar si la posición no tiene ningún lote con origen determinado — el importe de plusvalía no depende de ello.

**Corrección posterior — respaldo cuando `OpenPosition` no trae el dato.** El 100 % de cobertura de §21.1 es sobre el extracto usado para construir esto; en producción, cuentas con formas de posición menos comunes pueden llegar sin `costBasisPrice`/`fifoPnlUnrealized` en alguna fila — comprobado: el usuario lo vio en vivo (precio de entrada y plusvalía en blanco para alguna posición real).

- **Precio de entrada**: si falta `costBasisPrice`, se usa el precio del mismo lote FIFO que ya calcula el motor de Operaciones para esa cuenta/contrato (el que ya se usaba sólo para la fecha, §21.1 párrafo anterior) — mismo `Trade`/`Transfer`, no un dato nuevo. Contrastado contra un caso real con ambas fuentes disponibles: 305,405 (FIFO) frente a 305,42 (IBKR) — diferencia de precisión, no de método.
- **Plusvalía latente**: si falta `fifoPnlUnrealized`, **no se enmascara con `0,0`** — eso mostraría una posición "ni gana ni pierde" cuando en realidad no se sabe, justo el tipo de número fabricado que este proyecto evita en todos los demás cálculos. Se recalcula como `signo × cantidad × (precio_actual − precio_entrada)`, y sólo para acciones: en futuros/opciones el multiplicador importa, no se guarda en `positions`, y no vale la pena arriesgar un número mal escalado por una fila suelta.
- Si ni IBKR ni el motor FIFO tienen precio de entrada (posición realmente sin origen determinado, §20.5), el campo queda `None` genuino — no se fabrica un número, y esa fila concreta puede seguir sin plusvalía. Caso ya raro por construcción; con el respaldo, más raro todavía.

### 21.2 Futuros y opciones: en la tabla, fuera del pastel y del patrimonio total

`positionValue` de un futuro es nocional, no capital inmovilizado (§2, `NON_NAV_CATEGORIES`). Decisión explícita: el mismo criterio se extiende a opciones. Ambos aparecen en la tabla de posiciones con su plusvalía latente, pero no entran en el pastel de "% sobre el patrimonio" ni en el total de equity que lo acompaña — incluirlos por su nocional exageraría el peso real de una posición con poco margen comprometido.

### 21.3 Efectivo como posición

`ds.cash` a la última fecha de cada cuenta, una fila por `(cuenta, divisa)` en la **tabla de detalle** (mismo esquema que las posiciones: símbolo = código de divisa, tipo = "Efectivo", sin coste/plusvalía). En el **pastel**, en cambio, todas las líneas de efectivo —cualquier cuenta, cualquier divisa— se combinan en una única porción "Efectivo", convertida al tipo de cambio vigente. Detalle y pastel responden preguntas distintas: "¿cuánto tengo y dónde" (tabla, por cuenta/divisa) vs. "qué peso tiene el efectivo en mi patrimonio" (pastel, una cifra).

### 21.4 Divisa: siempre la base de la cuenta, no la de análisis

A diferencia del resto del panel, Cartera **no seguía** el selector "Moneda de análisis" de la barra lateral — usa siempre `ds.base_currency`. Mismo principio que §20.1 aplicado aquí: "qué tengo y cuánto vale" es una pregunta de estado, no de rendimiento comparado en una divisa u otra.

### 21.5 Exposición total (nocional bruto, largo y corto sin compensar)

Segunda cifra junto a Patrimonio: `exposición total = patrimonio + Σ|nocional de cada futuro/opción|`. Decisión explícita sobre largo/corto: se suma el **valor absoluto**, sin que un corto compense a un largo. Razón: un futuro corto expone al mercado tanto como uno largo — netearlos escondería nocional real (un largo de 10.000 € y un corto de 10.000 € sumarían "0 € de exposición extra" cuando en realidad hay 20.000 € en juego), justo el problema que esta cifra existe para destapar. Las acciones no se tocan: su valor ya es real, largo o corto, y ya está en Patrimonio.

### 21.6 Estructura

**Bloque combinado** (no dos secciones separadas): cajas de KPI apiladas verticalmente a la izquierda (Patrimonio, Exposición total, Posiciones abiertas, Líneas de efectivo) y el pastel a la derecha, en el mismo bloque — se leen juntos.

Tabla de detalle, con `cuenta` (no tiene sentido consolidarla — mismo criterio que el detalle de Operaciones):

```
cuenta, ticker, nombre, tipo (acción/opciones/futuros/efectivo), divisa, dirección,
cantidad, fecha entrada (mejor esfuerzo), precio entrada (costBasisPrice),
precio actual, plusvalía latente (fifoPnlUnrealized), %, en pastel (sí/no)
```

Pastel: agrupado por símbolo consolidando cuentas (no por fila de la tabla — "% de cada activo", no "% de cada cuenta-posición"; el efectivo además consolida divisas, §21.3), top 8 + "Otros", en `ds.base_currency` vía la matriz FX de §7.

---

## 22. Ajustes de presentación (Cartera, Efecto divisa, Informe)

Orden de pestañas: **MÉTRICAS · CARTERA · EFECTO DIVISA · OPERACIONES · INFORME**.

### 22.1 Cartera: pastel legible y tabla completa

Máximo 6 porciones (top 5 + "Otros" — antes 8+1): por encima de 6 categorías un pie/donut deja de leerse de un vistazo. Etiquetas con `textposition="outside"` de Plotly — la línea de cada porción a su etiqueta la dibuja Plotly, no es una leyenda lateral en caja. La tabla de detalle de abajo es la alternativa accesible (datos exactos, no sólo el color de una porción). Se quitó la caja "Líneas de efectivo": no aportaba sobre lo que ya dice "Patrimonio" + la tabla.

Tabla: el nombre del activo va en `.title()` ("Apple Inc", no "APPLE INC" — IBKR lo manda en mayúsculas), la columna "En pastel" se eliminó (ya lo dice "Tipo": acción/efectivo sí, opciones/futuros no) y las columnas de texto llevan ancho explícito por `column_config`. Sin ese ancho, "Nombre" se comía el espacio y las columnas de precio de entrada y plusvalía quedaban fuera de pantalla en una ventana normal — visibles sólo tras scroll horizontal, es decir, invisibles en la práctica.

**Corrección posterior — `st.dataframe` no hace scroll, descarta columnas.** El ajuste anterior (ancho `"small"` en algunas columnas de texto, el resto en automático) no bastaba: comprobado en vivo con el árbol de accesibilidad de la rejilla, `st.dataframe(..., width='stretch')` con `column_config` parcial **no deja las columnas que sobran detrás de un scroll horizontal — las descarta sin más**, ni se pintan ni entran en el DOM accesible, `scrollWidth` no las contabiliza. A los ~700-800 px de un contenedor real (ventana normal, barra lateral abierta) esto se comía "Precio entrada" y "Plusvalía" en Cartera, y "Ganancia"/"%" en Operaciones — que es justo lo que se reportó como "no aparece". Corrección real: ancho en **píxeles explícitos en todas las columnas** (no solo `"small"` en unas pocas) para que la suma quede por debajo del contenedor, columnas menos esenciales fuera de la tabla (Cartera: "Dir." y "Precio actual"; la cantidad ya lleva el signo del corto), y las columnas más importantes (Ganancia, %) puestas **antes** que las opcionales en el orden del `DataFrame` — por si algún caso con muchas columnas variables a la vez sigue sin caber, que se pierda lo menos importante, no lo más.

Nota aparte, sin corrección aplicada (no reproducido de forma fiable con interacción humana normal): en la primera pintura tras cambiar de pestaña, la rejilla (glide-data-grid, basada en canvas) a veces sólo pinta la primera columna hasta que hay un evento de scroll/rueda — un *repaint* que el scroll natural de la página dispara solo.

### 22.2 Efecto divisa — pestaña propia

Pestaña independiente (no una sección dentro de Métricas): cascada Estrategia/Divisa/Cruce/Total **más** el bloque de Riesgo de divisa (§14.4: volatilidades y correlación estrategia/divisa, reparto de varianza), que se movió aquí desde Métricas — las dos caras de la misma pregunta viven juntas.

Cero cálculo nuevo — reutiliza `att` de §7. El bloque "Cruce" (= `total − estrategia − divisa`, el término `estrategia × divisa`) existe para que la cascada cierre exacta contra el Total real: Estrategia y Divisa se **componen** geométricamente, no se suman, y omitir el cruce dejaría una cascada que no cuadra contra el número real — justo el tipo de descuadre que este proyecto no se permite.

Sin cajita de rentabilidad ni interruptor "resultado real / sin efecto divisa": la propia cascada ya enseña Estrategia y Total a la vez, así que la cifra suelta era redundante — y al quitarla, el interruptor se quedaba sin nada que gobernar.

El rango del eje Y se fija a mano con margen sobre el máximo acumulado. Plotly no reserva sitio para las etiquetas `outside` por su cuenta, y sin ese margen las cifras de las barras más altas (Cruce y Total) se cortaban contra el borde superior.

### 22.3 Informe — pestaña nueva

Resumen/Rentabilidad/Riesgo/Operaciones del periodo seleccionado en una sola vista — no introduce ningún cálculo nuevo, reordena lo que Métricas/Operaciones/Cartera ya calculan para el mismo periodo:

- **Resumen**: valor de la cartera (`T.portfolio().equity_total_analysis_ccy`) y rentabilidad del periodo (`att.total`).
- **Rentabilidad**: Estrategia/Divisa/Total, una columna por divisa (EUR, USD) — `E.attribute()` llamado con cada una como divisa de análisis. La Estrategia no es idéntica en las dos columnas: el FX se congela contra una divisa de análisis distinta en cada una (§7), no es el mismo número repetido.
- **Riesgo**: drawdown/volatilidad/Sharpe/Sortino del Total (`M.from_series`), beta y correlación frente al benchmark principal (`B.relative_metrics`, mismo cálculo que usa la sección de comparación de benchmark), concentración = peso de las 5 mayores posiciones (excluyendo efectivo y "Otros") sobre el patrimonio.
- **Operaciones**: compras/ventas cuentan **ejecuciones** de `Trade` en el periodo (§20.3), no operaciones FIFO — una posición que entra en varios lotes cuenta varias veces, igual que en un extracto de bróker. Dividendos y comisiones convertidos a la divisa de análisis fila a fila con el tipo de cambio de su propia fecha (mismo patrón que `to_analysis` de §21).

---

*Quant4all — Especificación de referencia v2.4 | 19/08/2026*
