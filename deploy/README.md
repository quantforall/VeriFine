# Despliegue del job diario (§19)

`q4_daily.py` es el entrypoint que ejecuta la sincronización diaria: descarga el
incremental de IBKR, recalcula todo el histórico y corre el canario de regresión
contra los golden de §13. Es idempotente (el solape de 10 días se deduplica).

## Requisitos previos

1. **Backfill hecho**: `raw/` con los bloques y `raw/state.json`. `q4_daily.py`
   siembra el watermark y el canario desde ahí en su primera ejecución.
2. **Credenciales**: `.ibkr_credentials` en la raíz del proyecto
   (`{"token":"…","query_id":"…"}`, gitignoreado) o las variables de entorno
   `IBKR_FLEX_TOKEN` / `IBKR_QUERY_ID`.

## Probar a mano

```bash
Q4_RAW_DIR=./raw python3 q4_daily.py
```

Códigos de salida: `0` ok / sin datos nuevos · `2` falta backfill o error ·
`3` token caducado (acción del usuario) · `4` bloqueo temporal 1025 · `5` hueco
> 365 días (rehacer backfill) · `6` reintentar mañana.

Registros (todos en `Q4_RAW_DIR`, gitignoreados): `daily.log` (traza completa),
`notifications.log` (avisos), `launchd.out.log` / `launchd.err.log`.

## Instalar el disparador (launchd, macOS)

Se ejecuta a media mañana CET: recoge con margen el cierre US de la víspera
(§19.8). El plist ya trae las rutas de esta máquina y el `python3` correcto.

```bash
cp deploy/com.verifine.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.verifine.daily.plist
```

Comprobar / lanzar a mano / desinstalar:

```bash
launchctl list | grep com.verifine.daily          # ¿cargado?
launchctl start com.verifine.daily                 # ejecutar ya (prueba)
launchctl unload ~/Library/LaunchAgents/com.verifine.daily.plist   # quitar
```

Cambiar la hora: edita `StartCalendarInterval` en el plist y recarga
(`unload` + `load`).

## Varias cuentas

Cada cuenta usa su propio almacén y su propio agente: copia el plist con otro
`Label` (p. ej. `com.verifine.daily.U11843602`), apunta `Q4_RAW_DIR` a un
directorio distinto y pon ahí su `.ibkr_credentials`. Así no se pisan.

## Nota

El plist lleva rutas absolutas de esta máquina (`/Users/juan/Desktop/VeriFine`)
y el intérprete `python3` resuelto al generarlo. Si mueves el proyecto o cambias
de Python, regenera el plist con las rutas nuevas.
