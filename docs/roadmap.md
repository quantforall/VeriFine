# VeriFine — Roadmap / cosas por hacer

Backlog vivo. Ordenado de "antes de usarlo en serio" a "proyectos de v2".

## Operativo (antes de usar en producción)

- [ ] **Regenerar el token de IBKR** y actualizar `.ibkr_credentials` — dio `1015`
  (token inválido), probablemente caducado. Los tokens del Flex Web Service
  expiran.
- [ ] **Instalar el disparador launchd** del job diario (ver
  [`deploy/README.md`](../deploy/README.md)).
- [ ] **Meter el histórico multi-cuenta al pipeline**: backfill + job diario de
  las cuentas ligadas (hoy solo validado sobre un bloque cargado a mano; la
  lógica de consolidación T7/T8 sí está validada).

## Panel / UX (mejoras v1.x)

- [ ] **"Todo el histórico por defecto"** en la opción de *Conectar con IBKR*
  (empezado y parado). Requiere que el backfill arranque de una fecha temprana y
  **salte los bloques anteriores a la apertura** de la cuenta.
- [ ] **Segundo benchmark de estilo** por defecto (Nasdaq-100 / MTUM), §15.5:
  contra el S&P 500 a secas el alfa engaña en una cartera growth/momentum. El
  selector ya lo permite; falta ponerlo por defecto.
- [ ] **Responsive móvil (375 px)**: las stat-cards usan grid `auto-fit` (reflowean),
  pero no se ha verificado a fondo en móvil.
- [ ] **Pestaña propia de Dividendos.** Hoy los dividendos sólo se ven como una
  línea (`dividendos_local`) dentro del agregado por ticker de Operaciones
  (§20.9) — cuentan como rendimiento en el TWR (§3) pero no tienen vista
  dedicada. Una pestaña separada podría mostrar: dividendo por ticker/fecha,
  yield sobre coste, evolución mensual/anual, retención fiscal (`Withholding
  Tax`) descontada vs. bruto, y el total ya usado hoy dentro de Operaciones.
  Fuente: `CashTransaction` con `type` en {Dividends, Payment In Lieu Of
  Dividends}, ya parseada — es una vista nueva sobre datos existentes, no un
  cálculo nuevo (mismo criterio que Informe, §22.3).

## Otros brókers (v2)

- [ ] **DeGiro.** El motor es reutilizable (todo trabaja sobre las 4 tablas
  canónicas), pero DeGiro **no tiene API oficial**. Camino sensato: empezar por
  los **exports CSV** (transacciones + cartera) en vez de la API no oficial
  (frágil, y necesita usuario/contraseña = acceso total). Retos reales:
  reconstruir el **NAV y la composición diaria** (IBKR los da ya; DeGiro no) y
  que **no hay TWR oficial** contra el que validar (perderíamos el *ground truth*
  que aquí nos salvó cinco veces). Primer paso: conseguir un export real de
  DeGiro y ver su granularidad, como hicimos con el XML de IBKR.
- [ ] **Ingesta genérica multi-bróker**: un parser por bróker → las 4 tablas; el
  resto del pipeline queda igual.

## Validación / robustez (v2)

- [ ] **Formas de cartera aún no vistas**: base no-EUR, otros derivados de
  nocional (FOP, CFD). El auto-chequeo (`q4_selfcheck`) las marcaría; validar y
  ajustar el parser cuando aparezcan (como se hizo con futuros y opciones).
- [ ] **Limpiar `scratch/`** (andamiaje de exploración) o convertir a pytest lo
  que valga (los tests print-based de ingest/sync/benchmark).

## Producto hospedado / social — "escenario B" (v2 grande)

- [ ] Estilo Kinfo: **perfiles públicos de rendimiento verificado, seguir a
  otros, multi-usuario, leaderboards.** Es lo que a VeriFine le falta frente a un
  agregador social.
- [ ] Requisitos de §18.4 (no negociables para hospedar): **token cifrado en
  reposo**, autenticación de usuarios, **aislamiento de datos por cuenta**,
  descarga en servidor. No es un ajuste, es un proyecto con su propia fase de
  seguridad.
