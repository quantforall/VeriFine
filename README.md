# VeriFine

Auditoría de rentabilidad y riesgo para cuentas de Interactive Brokers
(*Quant4all* internamente). Calcula el TWR de la cartera y lo descompone en
**Estrategia** (rendimiento puro del sistema con los tipos de cambio congelados)
y **Efecto divisa** (el resto), para no confundir "mi sistema dejó de funcionar"
con "la moneda se comió la rentabilidad".

El documento normativo es [`Quant4all_spec_referencia_v2.4.md`](Quant4all_spec_referencia_v2.4.md).
Toda decisión de cálculo vive ahí; este README sólo orienta.

## Módulos

| Fichero | Responsabilidad | Spec |
|---|---|---|
| `q4_parser.py` | XML de IBKR → 4 tablas canónicas (positions, cash, fx, movements). | §1–§2 |
| `q4_engine.py` | TWR, escenario FX-neutral y atribución Estrategia/Divisa. | §4–§7 |
| `q4_metrics.py` | Rentabilidad, riesgo y ventanas móviles. | §14 |
| `q4_benchmark.py` | Comparación contra Yahoo Finance (S&P 500 TR, etc.). | §15 |
| `q4_trades.py` | Operaciones: P&L por ticker (FIFO, splits, transferencias). | §20 |
| `q4_ingest.py` | Cliente del Flex Web Service (token + Query ID). | §18 |
| `q4_sync.py` | Backfill reanudable e incremental diario con canario de regresión. | §19 |
| `q4_selfcheck.py` | Auto-chequeo de integridad al cargar datos (T4/T5/T6b/§9). | §10 |
| `q4_daily.py` | Entrypoint del job diario (launchd/cron); ver `deploy/`. | §19 |
| `q4_license.py` | Control de acceso por suscripción (Substack), sin servidor propio. | — |
| `app.py` | Panel VeriFine en Streamlit (sólo presentación). | §17 |

## Ejecutar el panel

```bash
pip install -r requirements.txt
Q4_RAW_DIR=./raw streamlit run app.py
```

Carga los extractos de `Q4_RAW_DIR` (por defecto `./raw`) y recalcula todo desde
ahí. Tema oscuro en `.streamlit/config.toml`.

## Job diario

`q4_daily.py` sincroniza con IBKR, recalcula y corre el canario de regresión.
Instalación del disparador (launchd, macOS) y uso multi-cuenta en
[`deploy/README.md`](deploy/README.md).

## Tests

```bash
pip install -r requirements.txt
Q4_GOLDEN_DIR=/ruta/a/tus/xml pytest -q
```

61 tests. `test_golden.py` / `test_metrics.py` reproducen §13 desde los XML
crudos reales (no versionados, dato sensible; se leen de `Q4_GOLDEN_DIR`, por
defecto `~/Downloads`, y se **saltan** si no están). El resto
(`test_parser`, `test_ingest`, `test_benchmark`, `test_selfcheck`) corre offline.

## Datos y credenciales

- El **XML crudo es inmutable** (§18.7): un bug de parseo se corrige
  reprocesando, nunca volviendo a descargar. Todo `raw*/`, `data/`, cachés,
  estado y credenciales están gitignorados.
- El **token de IBKR es una credencial**: vive en `.ibkr_credentials`
  (gitignoreado) o en `IBKR_FLEX_TOKEN`/`IBKR_QUERY_ID`, nunca en logs ni en el
  repo.

## Estado

Validado de extremo a extremo **contra el servicio real de IBKR**:

- Motor: los TWR EUR anuales 2023–2026 reproducen el dato de IBKR a 0,00003 pb.
- Backfill completo **2021→2026** (serie continua de 1466 días, 0 conflictos de
  NAV). Resueltos los ±21 pb de la estrategia de 2023 (era un artefacto de
  escalado, §11.8/§13 corregidos en v2.3).
- Ingesta e incremental endurecidos contra el comportamiento real (1003 por
  `td` sin cierre, cortes de bloque en día hábil, 1018/1025, reintento de red).
- **Consolidación multi-cuenta validada** contra 5 cuentas reales (T7/T8, §16.2).
- **Auto-chequeo de integridad** en cada carga: si el NAV no reconstruye por
  cuenta, la atribución no cierra o hay saltos sin flujo, avisa en vez de mostrar
  números mal.

Cinco bugs de parseo descubiertos y corregidos con datos reales de distintas
formas de cartera: nocional de futuros, efectivo en divisa base, bootstrap
multi-cuenta, dedup de transferencias internas y doble conteo de opciones.

### Pendiente

Backlog completo en [`docs/roadmap.md`](docs/roadmap.md). En corto:

- **Instalar el disparador launchd** del job diario (ver `deploy/README.md`).
- **Regenerar el token** de IBKR si ha caducado (los tokens del Flex Web Service
  expiran).
- Formas de cartera aún no vistas (base no-EUR, otros derivados de nocional):
  el auto-chequeo las marcaría; validar cuando aparezcan.
- **`U6646571` fuera de `raw/`**: se descartó al sustituir el crudo por el
  histórico con el Query ampliado (§20.3) porque venía de una sincronización
  de prueba posterior a otra cuenta. Pendiente rehacer su extracción con el
  Query ya corregido y añadirla.
- `OptionEAE` (ejercicios/asignaciones/vencimiento de opciones) se parsea en
  el Query pero el motor de Operaciones (§20) aún no lo usa — con sólo 8
  operaciones de opciones en el histórico no se ha necesitado todavía.
- Sharpe/Sortino de §13.2 se imprimieron con CAGR = N/252; el motor usa
  calendario (§14.3, correcto), así que salen algo más altos.
