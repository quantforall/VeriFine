# scratch/ — andamiaje de exploración (no versionado)

Estos scripts son el trabajo previo con el que se derivaron los golden values
de §13. No forman parte del producto y no entran en el repo (`.gitignore`).
Se conservan como referencia del razonamiento.

| Fichero | Qué contiene | Estado |
|---|---|---|
| `quant4all_motor_validacion.py` | Motor de validación por bloques + **corrección de escalado del día bootstrap** (escala los cubos no-EUR para cuadrar el NAV cuando falta el ConversionRate). | **Contiene un fix que el motor de producción NO tiene.** Portar a `q4_parser`/`q4_engine` resuelve los −21 pb de la estrategia de 2023 (§11.8). |
| `quant4all_metricas_riesgo.py` | Métricas de riesgo con el mismo fix bootstrap. Usa CAGR = N/252 (convenio antiguo, §13.2). | Superado por `q4_metrics` (CAGR calendario, §14.3). |
| `engine2.py` | Calibración de `w` y de la fecha de flujo (reportDate vs settleDate). | Cerrado: `w=1`, `reportDate` (§4, §8.1). |
| `usd.py` | Comprobación de la forma cerrada USD sin flujos (§6.1). | Cerrado (T3). |
| `y2025.py` | Cadena TWR EUR 2025 vs cadena de twr diarios de IBKR. | Cerrado (T1b). |
| `test_bm.py` | Test offline del benchmark con serie sintética. | Print-based; convertir a pytest en la fase de benchmark. |
| `test_ingest.py` | Cliente Flex con transporte simulado (validación de query, backoff, redacción del token, errores fatales). | Print-based; convertir a pytest en la fase IBKR. |
| `test_sync.py` | Orquestador: backfill reanudable, incremental, watermark, canario, conflictos. | Print-based; convertir a pytest en la fase IBKR. |

Nota: casi todos apuntan a rutas `/mnt/user-data/uploads/` que ya no existen y
a nombres de fichero antiguos. Las cifras que imprimen algunos (p. ej. TWR USD
2025 = 16,7112 %) son **pre-validación** y las corrige §16.4.
