"""
VeriFine (Quant4all) — panel de rentabilidad y riesgo (Streamlit).

    pip install streamlit pandas plotly
    streamlit run app.py

Dos vías de entrada: subir XML o conectar con token + Query ID.
Toda la lógica vive en q4_parser / q4_engine / q4_metrics / q4_benchmark. Este
fichero es sólo presentación: si algo se calcula aquí, está en el sitio
equivocado. El sistema de diseño (Dark Mode OLED, Enterprise/Gateway) se aplica
vía .streamlit/config.toml + el CSS inyectado más abajo.
"""

from __future__ import annotations

import os
import json
import time
import glob
import shutil
import base64
import datetime as dt

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go

import q4_parser as P
import q4_engine as E
import q4_metrics as M
import q4_benchmark as B
import q4_license as L
import q4_selfcheck as SC
import q4_trades as T

st.set_page_config(page_title="VeriFine", page_icon=":bar_chart:", layout="wide")

# --------------------------------------------------------------------------
# Sistema de diseño — tokens semánticos (no hex sueltos en componentes)
# --------------------------------------------------------------------------
# Dos paletas completas, no solo "invertir" la oscura: el claro necesita
# tonos más OSCUROS de verde/rojo/azul/ámbar que el oscuro para mantener
# contraste sobre blanco (un #22C55E que resalta sobre #020617 se ve lavado
# sobre #FFFFFF) — mismo criterio en toda la tabla, aprox. un escalón más
# oscuro en la escala Tailwind (500 -> 600) para cada acento.
_PALETTE_DARK = dict(
    BG="#020617", PANEL="#0F172A", CARD="#0E1223",
    FG="#F8FAFC", MUTED="#94A3B8", BORDER="#334155",
    POS="#22C55E", NEG="#EF4444", SERIES="#38BDF8", BENCH="#F59E0B",
    GRID="rgba(51,65,85,0.45)",
)
_PALETTE_LIGHT = dict(
    BG="#F8FAFC", PANEL="#F1F5F9", CARD="#FFFFFF",
    FG="#0F172A", MUTED="#64748B", BORDER="#E2E8F0",
    POS="#16A34A", NEG="#DC2626", SERIES="#0284C7", BENCH="#D97706",
    GRID="rgba(148,163,184,0.35)",
)
# Los nombres sueltos (BG, PANEL, CARD...) son los que usa el resto del
# fichero (Plotly, pandas.Styler, PIE_COLORS...) — arrancan en oscuro, el
# tema por defecto, y _apply_theme_palette() los REASIGNA en caliente al
# principio de main() en cuanto se sabe qué tema tiene el navegador (ver su
# docstring: el CSS puro no basta para lo que Python pinta de antemano,
# como las celdas de una tabla o las líneas de un gráfico).
BG, PANEL, CARD = _PALETTE_DARK["BG"], _PALETTE_DARK["PANEL"], _PALETTE_DARK["CARD"]
FG, MUTED, BORDER = _PALETTE_DARK["FG"], _PALETTE_DARK["MUTED"], _PALETTE_DARK["BORDER"]
POS, NEG, SERIES = _PALETTE_DARK["POS"], _PALETTE_DARK["NEG"], _PALETTE_DARK["SERIES"]
BENCH = _PALETTE_DARK["BENCH"]
GRID = _PALETTE_DARK["GRID"]


def _apply_theme_palette(theme: str) -> None:
    """Reasigna BG/PANEL/CARD/FG/MUTED/BORDER/POS/NEG/SERIES/BENCH/GRID (y
    PIE_COLORS, que se recalcula de los mismos) al vuelo, según el tema que
    ha detectado el componente theme_watcher en ESTE rerun.

    Por qué hace falta esto y no basta con CSS: el CSS (THEME_CSS,
    [data-vf-theme="light"]) cubre TODO lo que el navegador pinta —
    inputs, botones, calendario, slider, tarjetas — porque esas reglas usan
    var(--bg) etc. y el navegador las reevalúa solo al cambiar de tema. Pero
    dos cosas las decide PYTHON de antemano, antes de que el navegador sepa
    nada: el color de cada celda de st.dataframe (pandas.Styler genera un
    style= por celda en el HTML, y Streamlit lo traduce a píxeles de
    <canvas> tal cual se lo den) y el color de cada línea de un gráfico de
    Plotly (va incrustado en el propio JSON de la figura). Sin esto, tablas
    y gráficos se quedarían siempre en un tema fijo pasara lo que pasara
    con el resto de la app.

    Llamar ANTES de construir ninguna tabla o gráfico en main() — Python
    resuelve los nombres globales en el momento de USARLOS, no al definir
    la función que los usa, así que esto vale mientras se llame a tiempo."""
    global BG, PANEL, CARD, FG, MUTED, BORDER, POS, NEG, SERIES, BENCH, GRID, PIE_COLORS
    p = _PALETTE_LIGHT if theme == "light" else _PALETTE_DARK
    BG, PANEL, CARD = p["BG"], p["PANEL"], p["CARD"]
    FG, MUTED, BORDER = p["FG"], p["MUTED"], p["BORDER"]
    POS, NEG, SERIES = p["POS"], p["NEG"], p["SERIES"]
    BENCH = p["BENCH"]
    GRID = p["GRID"]
    # Paleta cualitativa del pastel de Cartera (§21): SERIES/BENCH/POS ya
    # existen y se reutilizan primero; el resto son tonos de acento fijos
    # (no hace falta variante por tema — su saturación media ya se lee bien
    # tanto en blanco como en el OLED oscuro).
    PIE_COLORS = [SERIES, BENCH, POS, "#A78BFA", "#F472B6", "#2DD4BF", "#FB923C", "#818CF8", MUTED]


PIE_COLORS = [SERIES, BENCH, POS, "#A78BFA", "#F472B6", "#2DD4BF", "#FB923C", "#818CF8", MUTED]

# Por defecto, una ruta estable en el home del usuario — no relativa al
# directorio de la app: si se reemplaza/actualiza la carpeta del código
# (nueva versión entregada a un cliente), los datos ya sincronizados no se
# quedan huérfanos ni se pisan con los de otra copia del programa.
RAW_DIR = os.environ.get("Q4_RAW_DIR",
                          os.path.join(os.path.expanduser("~"), "VeriFine", "raw"))
LICENSE_PATH = os.path.join(RAW_DIR, ".license.json")

# Mismo fichero/formato que ya usa q4_daily.py ({"token":..,"query_id":..}),
# pero en RAW_DIR en vez de la raíz del proyecto — para que sobreviva a una
# actualización del código igual que el resto de datos del usuario (§RAW_DIR
# más arriba). Local, en el disco del propio usuario: no pasa por ningún
# servidor. Opt-in vía el checkbox "Recordar en este equipo" de más abajo.
IBKR_CREDS_PATH = os.path.join(RAW_DIR, ".ibkr_credentials")


def _load_ibkr_creds() -> tuple[str, str]:
    if os.path.exists(IBKR_CREDS_PATH):
        try:
            d = json.load(open(IBKR_CREDS_PATH))
            return str(d.get("token", "")), str(d.get("query_id", ""))
        except (json.JSONDecodeError, OSError):
            pass
    return "", ""


def _save_ibkr_creds(token: str, qid: str) -> None:
    os.makedirs(RAW_DIR, exist_ok=True)
    json.dump({"token": token, "query_id": qid}, open(IBKR_CREDS_PATH, "w"))


def _clear_ibkr_creds() -> None:
    if os.path.exists(IBKR_CREDS_PATH):
        os.remove(IBKR_CREDS_PATH)

# Componente a medida (protocolo crudo de Streamlit, sin build): detecta si
# el navegador soporta elegir una carpeta real (File System Access API —
# solo Chrome/Edge/Opera) y, si la elige, devuelve su nombre a Python. Fase
# local (Camino A, paso 1 de 2): confirma la elección; todavía no escribe
# los extractos ahí — ver components/folder_picker/index.html.
_folder_picker = components.declare_component(
    "folder_picker",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "components", "folder_picker"))
# Invisible (0px): detecta si el navegador tiene Streamlit en claro u
# oscuro (Settings -> "Choose app theme") y lo cuenta a los dos lados que
# lo necesitan — ver su docstring en index.html y _apply_theme_palette()
# más arriba.
_theme_watcher = components.declare_component(
    "theme_watcher",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "components", "theme_watcher"))
# Candado de sincronización: por encima de esto se asume que el proceso que
# lo dejó ya no existe (colgado o muerto), no que sigue corriendo de verdad.
# Más que de sobra para un backfill normal (minutos), corto para no dejar el
# candado puesto un día entero por un fallo real.
LOCK_STALE_S = 900

THEME_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');

:root {{
  --bg:{_PALETTE_DARK["BG"]}; --panel:{_PALETTE_DARK["PANEL"]}; --card:{_PALETTE_DARK["CARD"]};
  --fg:{_PALETTE_DARK["FG"]}; --muted:{_PALETTE_DARK["MUTED"]};
  --border:{_PALETTE_DARK["BORDER"]}; --color-positive:{_PALETTE_DARK["POS"]};
  --color-negative:{_PALETTE_DARK["NEG"]}; --color-neutral:{_PALETTE_DARK["FG"]}; --ring:#FFFFFF;
  --series:{_PALETTE_DARK["SERIES"]}; --bench:{_PALETTE_DARK["BENCH"]};
}}
/* El oscuro de arriba es el que arranca por defecto (nadie ha tocado el
   tema todavía, o theme_watcher aún no ha llegado a avisar de nada — ver
   su componente). En cuanto el navegador tiene Streamlit en claro,
   theme_watcher pone data-vf-theme="light" en <html> y ESTE bloque toma
   el relevo: todo lo demás de esta hoja de estilos usa var(--bg) etc, así
   que no hace falta duplicar ninguna regla más abajo — solo estos
   tokens. */
html[data-vf-theme="light"] {{
  --bg:{_PALETTE_LIGHT["BG"]}; --panel:{_PALETTE_LIGHT["PANEL"]}; --card:{_PALETTE_LIGHT["CARD"]};
  --fg:{_PALETTE_LIGHT["FG"]}; --muted:{_PALETTE_LIGHT["MUTED"]};
  --border:{_PALETTE_LIGHT["BORDER"]}; --color-positive:{_PALETTE_LIGHT["POS"]};
  --color-negative:{_PALETTE_LIGHT["NEG"]}; --color-neutral:{_PALETTE_LIGHT["FG"]}; --ring:#0F172A;
  --series:{_PALETTE_LIGHT["SERIES"]}; --bench:{_PALETTE_LIGHT["BENCH"]};
}}

/* !important en estas reglas base a propósito, no por costumbre: hasta
   ahora coincidían por casualidad con los colores puestos en
   config.toml (incluso sin forzarlas, "ganaban" porque ambos valores
   eran el mismo hex) — al quitar esos colores personalizados de
   config.toml (experimento del tema de fábrica, ver su cabecera) quedó
   al descubierto que [data-testid="stSidebar"] en concreto SÍ tiene una
   regla nativa de Streamlit más específica que la nuestra: sin
   !important el sidebar se quedaba con el gris de fábrica de Streamlit
   en vez de nuestro --panel. Con !important, nuestro CSS manda siempre,
   tenga Streamlit puestos colores personalizados o no. */
.stApp {{ background: var(--bg) !important; }}
html, body, [data-testid="stAppViewContainer"], .stMarkdown, p, span, label, div {{
  font-family: 'Fira Sans', system-ui, sans-serif;
  color: var(--fg);
}}
h1, h2, h3, h4, [data-testid="stMetricValue"] {{ font-family: 'Fira Code', ui-monospace, monospace; }}
[data-testid="stSidebar"] {{
  background: var(--panel) !important;
  border-right: 1px solid var(--border) !important;
}}

/* cifras financieras siempre tabulares para evitar layout shift */
.vf-value, [data-testid="stMetricValue"], [data-testid="stDataFrame"], .stDataFrame,
code, .vf-num {{ font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }}

/* focus rings visibles (accesibilidad) */
*:focus-visible {{ outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 4px; }}

/* --- marca --- */
.vf-brand {{ display:flex; align-items:center; gap:9px; font-family:'Fira Code',monospace;
  font-weight:700; font-size:20px; letter-spacing:.02em; color:var(--fg);
  padding:2px 0 10px; }}
.vf-brand .vf-ico {{ color:var(--color-positive); }}
.vf-title {{ font-size:30px; font-weight:700; margin:0;
  text-shadow:0 0 12px rgba(34,197,94,.12); }}
.vf-headline {{ display:flex; align-items:center; gap:12px; margin:2px 0 2px; }}
.vf-headline .vf-ico {{ color:var(--color-positive); }}
.vf-tagline {{ color:var(--muted); font-weight:400; font-size:0.6em; }}
.vf-sub {{ color:var(--muted); font-size:13px; font-family:'Fira Code',monospace; margin-bottom:6px; }}

/* --- stat cards (KPI) --- */
/* minmax 178px, no 200: las filas de CINCO tarjetas (Total y Estrategia)
   rompían 4+1 en una ventana normal (~984px de contenido), dejando la quinta
   sola en su fila. 5*178 + 4*14 de hueco = 946 <= 984, así que entran las
   cinco; en pantallas menores el auto-fit sigue reflowando igual. */
.vf-kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
  gap:14px; margin:10px 0 6px; }}
.vf-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:16px 18px; transition:border-color .2s ease; }}
.vf-card:hover {{ border-color:#475569; }}
/* align-items:flex-start, no center: mismo defecto que .vf-hint-bench
   (§vf-hint-bench) — con un título largo ("Acumulado total (EUR)", "Máx.
   drawdown total") el texto envuelve a dos líneas en tarjetas estrechas, y
   con "center" el icono se centraba respecto al bloque de dos líneas en
   vez de quedar pegado a la primera. margin-top en el icono para que en
   el caso normal (una sola línea) siga centrado con el texto.
   min-height es la parte que de verdad se veía mal (reportado con captura):
   el ancho de cada tarjeta —y por tanto si su título envuelve a una o dos
   líneas— depende del ancho de ventana (grid responsive, auto-fit), así
   que a un ancho dado unas tarjetas de la misma fila envuelven y otras no.
   Sin una altura mínima reservada para DOS líneas, el valor grande de
   abajo (.vf-value) arrancaba más arriba en las tarjetas de una línea que
   en las de dos — la fila entera se veía escalonada. Con min-height fijo,
   todas reservan el mismo hueco reciba una línea de título o dos, así que
   el valor arranca a la misma altura en toda la fila, a cualquier ancho. */
.vf-card-top {{ display:flex; align-items:flex-start; gap:8px; color:var(--muted);
  min-height:32px; }}
.vf-card-top .vf-ico {{ margin-top:1px; }}
.vf-label {{ font-size:11.5px; letter-spacing:.04em; text-transform:uppercase;
  font-family:'Fira Code',monospace; line-height:1.35; }}
.vf-value {{ font-family:'Fira Code',monospace; font-size:27px; font-weight:600; margin-top:10px; }}
/* fila del valor de Estrategia + insignia de veredicto: la insignia vive AQUÍ
   (junto al dato grande), no escondida en la línea pequeña de abajo — pedido
   explícito de visibilidad. */
.vf-value-row {{ display:flex; align-items:center; justify-content:space-between;
  gap:10px; margin-top:10px; }}
.vf-value-row .vf-value {{ margin-top:0; white-space:nowrap; }}
.vf-verdict-badge {{ display:inline-flex; align-items:center; justify-content:center;
  width:34px; height:34px; border-radius:50%; flex:0 0 auto; }}
.vf-verdict-badge.vf-pos-bg {{ background:rgba(34,197,94,0.18); color:var(--color-positive); }}
.vf-verdict-badge.vf-neg-bg {{ background:rgba(239,68,68,0.18); color:var(--color-negative); }}
.vf-hint {{ color:var(--muted); font-size:11px; margin-top:4px; font-family:'Fira Sans'; }}
/* referencia de benchmark DENTRO de una KPI card: misma muestra discontinua
   ámbar que en gráficos y tarjetas de comparación, para leerla como "el mismo
   dato, para el benchmark" sin necesidad de otro color en la leyenda.
   Fuente al 150 % de .vf-hint (11px -> 16.5px): es el segundo dato más
   importante de la tarjeta, no una nota al pie. */
/* align-items:flex-start, no center: con un valor largo (p. ej. "+117.90 %"
   de un acumulado a varios años) el texto envuelve a dos líneas en tarjetas
   estrechas — con "center" la muestra discontinua (altura 0) se centraba
   respecto al bloque de dos líneas y quedaba flotando entre ellas en vez de
   alineada con la primera, la única tarjeta de la fila que se veía distinta
   al resto (comprobado). Con flex-start queda pegada arriba, igual la
   envuelva o no. */
.vf-hint-bench {{ display:flex; align-items:flex-start; gap:7px; color:var(--bench);
  font-size:16.5px; font-family:'Fira Code',monospace; margin-top:9px;
  padding-top:9px; border-top:1px dashed var(--border); }}
.vf-hint-bench .vf-dash-swatch {{ margin-top:8px; }}  /* centrada en la altura de línea del texto */
.vf-pos {{ color:var(--color-positive); }}
.vf-neg {{ color:var(--color-negative); }}
.vf-neutral {{ color:var(--fg); }}
.vf-ico {{ flex:0 0 auto; }}

/* --- cabecera de sección con icono SVG --- */
.vf-sec {{ display:flex; align-items:center; gap:10px; margin:26px 0 8px; }}
.vf-sec .vf-ico {{ color:var(--color-positive); }}
.vf-sec h3 {{ font-size:18px; font-weight:600; margin:0; }}

/* muestra discontinua ámbar: convención de "esto es el benchmark", reutilizada
   en los gráficos y en la referencia de benchmark de las KPI cards
   (§color-not-only: la diferencia entre series nunca depende sólo del color). */
.vf-dash-swatch {{ display:inline-block; width:13px; height:0; border-top:2.5px dashed var(--bench);
  flex:0 0 auto; }}

/* Alertas justo debajo del título (conflictos de NAV, chequeos de
   integridad, aviso de fondeo): letra más pequeña, pedido explícito.
   Contenedor con key (.st-key-top-alerts) para no afectar a otras
   alertas de la página (errores de sincronización, etc.).
   El padding de stAlertContainer viene de una clase utilitaria de
   Streamlit en rem (relativa a la RAÍZ, no al font-size del propio
   contenedor) — bajarle el font-size no movía el padding un pixel
   (comprobado). Se fija el padding a mano, en la misma proporción que el
   texto de abajo, para que la caja encoja con la letra. */
.st-key-top-alerts [data-testid="stAlertContainer"] {{
  padding: 0.75rem !important;
}}
/* El texto (<p> etc.) trae su propia regla de Streamlit en rem, relativa a
   la RAÍZ, no al contenedor de arriba ya reducido — "inherit" no bastaba
   para pisarla de forma fiable (probado). 0.75rem coincide con el 75% de
   arriba porque ambos parten del mismo tamaño raíz, y sigue moviéndose
   con baseFontSize si se vuelve a tocar el tema. */
.st-key-top-alerts [data-testid="stMarkdownContainer"] p,
.st-key-top-alerts [data-testid="stMarkdownContainer"] li,
.st-key-top-alerts [data-testid="stMarkdownContainer"] strong,
.st-key-top-alerts [data-testid="stMarkdownContainer"] em,
.st-key-top-alerts [data-testid="stMarkdownContainer"] code {{
  font-size: 0.75rem !important;
}}

/* --- guía de configuración (onboarding IBKR, pestaña Configuración) --- */
.vf-guide-eyebrow {{ font-family:'Fira Code',monospace; font-size:12px; font-weight:600;
  letter-spacing:.14em; text-transform:uppercase; color:var(--color-positive); display:flex;
  align-items:center; gap:9px; margin:4px 0 16px; }}
.vf-guide-eyebrow::before {{ content:""; width:7px; height:7px; border-radius:2px;
  background:var(--color-positive); box-shadow:0 0 10px rgba(34,197,94,.6); flex:0 0 auto; }}
.vf-step {{ margin-top:36px; padding-top:26px; border-top:1px solid var(--border); }}
.vf-step:first-of-type {{ margin-top:8px; padding-top:0; border-top:none; }}
.vf-step-head {{ display:flex; align-items:center; gap:12px; margin-bottom:4px; }}
.vf-step-num {{ font-family:'Fira Code',monospace; font-size:12.5px; font-weight:600;
  color:var(--color-positive); background:rgba(34,197,94,.12); border:1px solid rgba(34,197,94,.35);
  border-radius:6px; padding:3px 9px; flex:0 0 auto; }}
.vf-step-head h3 {{ font-size:18px; font-weight:600; margin:0; display:flex;
  align-items:center; gap:9px; }}
.vf-step-head .vf-ico {{ color:var(--color-positive); }}
.vf-step-note {{ color:var(--muted); font-size:14px; margin:8px 0 16px; text-align:justify; }}
/* Párrafo de intro de la guía de alta ("Antes de sincronizar..."): mismo
   pedido de alineación justificada que los .vf-step-note de abajo, para
   que todo el bloque de texto de la guía se vea consistente en vez de
   que el primer párrafo desentone (a petición de Juan, con captura). */
.st-key-guide-intro p {{ text-align: justify; }}
.vf-checklist {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
  gap:8px; margin:6px 0 4px; }}
.vf-check-item {{ display:flex; align-items:flex-start; gap:8px; font-size:13px;
  background:var(--card); border:1px solid var(--border); border-radius:8px;
  padding:9px 11px; line-height:1.35; }}
.vf-check-item svg {{ color:var(--color-positive); flex:0 0 auto; margin-top:2px; }}
.vf-check-item .tag {{ display:block; font-family:'Fira Code',monospace; font-size:10.5px;
  color:var(--muted); margin-top:2px; }}
.vf-check-extra {{ color:var(--muted); font-size:12.5px; margin-top:10px; }}
.vf-callout {{ display:flex; gap:10px; align-items:flex-start; border-radius:8px;
  padding:12px 14px; font-size:13.5px; margin:12px 0; border:1px solid; }}
.vf-callout svg {{ flex:0 0 auto; margin-top:2px; }}
.vf-callout p {{ margin:0; }}
.vf-callout-info {{ background:rgba(56,189,248,.08); border-color:rgba(56,189,248,.35); }}
.vf-callout-info svg {{ color:var(--series); }}
.vf-callout-warn {{ background:rgba(245,158,11,.08); border-color:rgba(245,158,11,.35); }}
.vf-callout-warn svg {{ color:var(--bench); }}
.vf-kv-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
  gap:8px; margin:10px 0 4px; }}
.vf-kv-item {{ background:var(--card); border:1px solid var(--border); border-radius:8px;
  padding:8px 11px; font-size:12.5px; }}
.vf-kv-item .k {{ display:block; color:var(--muted); font-size:10.5px;
  text-transform:uppercase; letter-spacing:.04em; margin-bottom:3px; }}
.vf-kv-item .v {{ font-family:'Fira Code',monospace; font-weight:600; }}
.vf-kv-item .v.attn {{ color:var(--bench); }}

/* El menú "☰" (Settings → "Choose app theme") ahora se deja VISIBLE a
   propósito — antes se ocultaba porque la app solo sabía pintarse en
   oscuro y cambiar de tema la rompía. Ya no: theme_watcher (ver su
   declare_component más arriba) detecta claro/oscuro en vivo y
   _apply_theme_palette() + [data-vf-theme="light"] (§:root) adaptan tanto
   el CSS como lo que pinta Python (tablas, gráficos). Reforzado en
   .streamlit/config.toml con [client] toolbarMode = "auto" (antes
   "minimal", que escondía el menú entero). */

/* La barra de arriba del todo: es la cabecera PROPIA de la app
   ([data-testid="stHeader"], 52px, position:absolute sobre el contenido),
   y en Streamlit Cloud es donde se inyectan los iconos de gestión del
   dueño (estrella, editar, GitHub, "Deploy"). En local sale transparente
   y se ve el fondo oscuro de la app por debajo, así que nunca dio la
   cara; en Cloud sale BLANCA — mismo patrón que el resto de contenedores
   nativos que ya hubo que forzar aquí. Sí forma parte del DOM de la app,
   de modo que este CSS la alcanza (los iconos de Cloud van dentro de
   stToolbarActions, también aquí dentro). */
[data-testid="stHeader"] {{ background-color: var(--bg) !important; }}
[data-testid="stToolbar"], [data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"], [data-testid="stStatusWidget"] {{
  background-color: transparent !important;
}}
[data-testid="stHeader"] *, [data-testid="stToolbar"] * {{
  color: var(--fg) !important;
}}

/* Streamlit Cloud (comprobado en vivo en verifine.streamlit.app, distinto
   de lo que se ve en local) NO pinta el fondo oscuro en el CONTENEDOR de
   varios widgets pese a base="dark" en config.toml — probable diferencia
   de versión de Streamlit entre local y Cloud. Confirmado por CSS
   computada: stTextInputRootElement y stDateInputField salían en blanco
   puro (255,255,255) con el texto ya en color de tema oscuro (casi
   blanco) — blanco sobre blanco, invisible; stBaseButton-secondary casi
   igual. Se fuerza aquí en vez de fiarse del motor de temas de Streamlit.
   [data-baseweb=...] son los atributos que BaseWeb (la librería de
   componentes que usa Streamlit) pone en el contenedor de cada widget —
   cubre inputs/selects aunque cambien el data-testid entre versiones. */
[data-testid="stTextInputRootElement"],
[data-testid="stDateInputField"],
[data-testid="stNumberInputField"],
[data-testid="stSelectboxRootElement"],
[data-testid="stMultiSelectRootElement"],
[data-baseweb="input"], [data-baseweb="base-input"],
[data-baseweb="select"], [data-baseweb="textarea"] {{
  background-color: var(--card) !important;
  border-color: var(--border) !important;
}}

/* El bloque de arriba forzaba el FONDO y el borde de los campos, pero no
   el color del TEXTO: ese se quedaba a merced del tema, y en Cloud eso
   significa el gris oscuro del tema claro sobre el fondo oscuro que
   nosotros sí forzamos — la fecha por defecto o lo que se escribe se
   confundía con el fondo (reportado por Juan). Los <input> no heredan el
   color del contenedor, así que hay que apuntarlos directamente.
   -webkit-text-fill-color hace falta además para lo que rellena el
   gestor de contraseñas (el campo Token), donde Chrome ignora `color`. */
[data-testid="stTextInputRootElement"] input,
[data-testid="stDateInputField"], [data-testid="stNumberInputField"],
[data-baseweb="input"] input, [data-baseweb="base-input"] input,
[data-baseweb="select"] input, [data-baseweb="textarea"] textarea {{
  color: var(--fg) !important;
  -webkit-text-fill-color: var(--fg) !important;
}}
[data-testid="stTextInputRootElement"] input::placeholder,
[data-testid="stDateInputField"]::placeholder,
[data-baseweb="input"] input::placeholder,
[data-baseweb="select"] input::placeholder {{
  color: var(--muted) !important;
  -webkit-text-fill-color: var(--muted) !important;
}}

/* Autocompletado NATIVO del navegador (no es Streamlit ni Cloud): cuando
   Chrome reconoce un valor ya escrito antes en un campo — reportado por
   Juan con captura en "Query ID" —, pinta su propio fondo de sugerencia
   por encima del nuestro. `background-color` no sirve para pisarlo:
   Chrome ignora esa propiedad en un input autorellenado y solo respeta
   unas pocas, entre ellas box-shadow — de ahí el truco clásico de un
   inset gigante del tamaño del campo, en vez de un fondo normal.
   La transición del color a 5000s es el mismo truco: Chrome anima SU
   fondo con un fade propio que no se puede desactivar directamente;
   alargar la transición a algo mayor que cualquier sesión real la deja
   inmóvil en la práctica. */
[data-testid="stTextInputRootElement"] input:-webkit-autofill,
[data-testid="stDateInputField"]:-webkit-autofill,
[data-testid="stNumberInputField"]:-webkit-autofill,
[data-baseweb="input"] input:-webkit-autofill,
[data-baseweb="base-input"] input:-webkit-autofill,
[data-baseweb="select"] input:-webkit-autofill {{
  -webkit-box-shadow: 0 0 0 1000px var(--card) inset !important;
  -webkit-text-fill-color: var(--fg) !important;
  caret-color: var(--fg) !important;
  transition: background-color 5000s ease-in-out 0s;
}}
/* El valor que muestra un selectbox no es un <input>, es un div aparte.
   El multiselect queda FUERA a propósito: sus chips ya llevan texto
   oscuro sobre verde unas reglas más abajo, y un color general aquí se
   los comería. */
[data-testid="stSelectbox"] [data-baseweb="select"] {{
  color: var(--fg) !important;
}}
/* Slider: el número que va encima del pomo y las etiquetas de los
   extremos. En Cloud el pomo sale además en el rojo por defecto en vez de
   nuestro verde, mismo caso que las pestañas o los chips.
   La barra de relleno se deja como esté a propósito: BaseWeb la pinta con
   un linear-gradient calculado al vuelo que codifica la POSICIÓN del
   rango elegido — sobrescribirlo con un color fijo borraría esa
   información y dejaría la barra plana. */
[data-testid="stSliderThumbValue"] {{ color: var(--color-positive) !important; }}
[data-testid="stSliderTickBar"], [data-testid="stSliderTickBar"] span {{
  color: var(--muted) !important;
}}
[data-testid="stSlider"] [role="slider"] {{
  background-color: var(--color-positive) !important;
}}
[data-testid="stBaseButton-secondary"] {{
  background-color: var(--card) !important;
  border-color: var(--border) !important;
  color: var(--fg) !important;
}}
[data-testid="stBaseButton-secondary"]:hover {{
  background-color: var(--panel) !important;
  border-color: #475569 !important;
}}

/* El multiselect ("Cuentas", "Añadir más al detalle") tiene, además, un
   div intermedio SIN data-testid con fondo blanco propio (comprobado en
   vivo: hijo directo de .react-aria-ComboBox, la clase de la librería que
   usa Streamlit por debajo — esa sí es estable entre versiones, a
   diferencia de sus clases "st-emotion-cache-xxxxx" generadas, que
   cambian). */
.react-aria-ComboBox, .react-aria-ComboBox > div {{
  background-color: var(--card) !important;
  border-color: var(--border) !important;
}}

/* El calendario desplegable de st.date_input (se abre al pulsar el campo,
   no es el campo en sí — ese ya va cubierto arriba con stDateInputField)
   tiene el MISMO problema en Streamlit Cloud: fondo blanco puro y el
   círculo del día elegido en el rojo por defecto de Streamlit. No expone
   ningún data-testid en las celdas — solo [data-baseweb="calendar"] en el
   contenedor y, en la celda del día elegido, un aria-label que empieza
   por "Selected" (confirmado inspeccionando el DOM real; el círculo en sí
   es un ::after de esa celda, no un fondo normal — por eso hace falta
   apuntar directamente al pseudo-elemento). */
[data-baseweb="calendar"], [data-baseweb="popover"] {{
  background-color: var(--card) !important;
  color: var(--fg) !important;
  border-color: var(--border) !important;
}}
/* La regla de arriba solo pinta el CONTENEDOR exterior — background-color
   no se hereda en CSS, así que la cabecera (mes/año y "Mo Tu We...") se
   quedaba en su fondo blanco propio (reportado por Juan: cuerpo del
   calendario ya oscuro, cabecera seguía en blanco). Sin data-testid ni
   data-baseweb propios en esa cabecera — se fuerza transparente en TODOS
   los descendientes, dejando que se vea el fondo oscuro del contenedor
   por debajo; el círculo verde del día elegido es un ::after (regla de
   abajo), no un background-color normal, así que esto no lo toca. */
[data-baseweb="calendar"] * {{
  background-color: transparent !important;
  color: var(--fg) !important;
}}
/* El selector universal de arriba NO alcanza pseudo-elementos, y BaseWeb
   pinta con ellos justo lo que seguía saliendo mal: las celdas VACÍAS del
   mes (los huecos antes del día 1 y después del último) llevan un ::after
   con content:"" y el color de fondo del tema — comprobado en local:
   rgb(2,6,23), que aquí es nuestro --bg y pasa desapercibido, pero en
   Cloud se resuelve en BLANCO. Esos eran los rectángulos blancos sueltos
   dentro del calendario. Se anulan todos y se vuelve a afirmar debajo
   solo el del día elegido, que sí queremos ver. */
[data-baseweb="calendar"] *::before,
[data-baseweb="calendar"] *::after {{
  background-color: transparent !important;
}}
[data-baseweb="calendar"] [role="gridcell"][aria-label^="Selected"]::after {{
  background-color: var(--color-positive) !important;
}}
/* Texto oscuro encima del círculo verde: el blanco de --fg sobre #22C55E
   se queda en ~2:1 de contraste (el número del día elegido casi no se
   leía). Mismo tono que ya se usa en los chips del multiselect. */
[data-baseweb="calendar"] [role="gridcell"][aria-label^="Selected"],
[data-baseweb="calendar"] [role="gridcell"][aria-label^="Selected"] * {{
  color: #04140A !important;
}}
/* Los días fuera de rango (en modo prueba el campo lleva min_value, así
   que TODO lo anterior al límite de 6 meses sale deshabilitado) los
   atenúa BaseWeb bajándoles el color: comprobado en una app de prueba con
   min_value, gris rgb(163,168,184) frente al blanco de los elegibles, y
   aria-label que empieza por "Not available" en vez de "Choose". El
   selector universal de color de arriba se llevaba por delante ese
   atenuado y los dejaba idénticos a los elegibles — parecían pulsables sin
   serlo. Se restaura aquí. */
[data-baseweb="calendar"] [role="gridcell"][aria-label^="Not available"],
[data-baseweb="calendar"] [role="gridcell"][aria-label^="Not available"] * {{
  color: var(--muted) !important;
}}

/* primaryColor (#22C55E) tampoco se aplica en Streamlit Cloud en varios
   sitios nativos — sale el rojo por defecto de Streamlit (#FF4B4B) en su
   lugar: pestaña activa, botón activo de un segmented_control, chips de
   un multiselect y la casilla marcada de un checkbox. Mismo patrón que el
   fondo blanco de arriba: se fuerza aquí en vez de fiarse del tema. */
[data-testid="stTab"][aria-selected="true"] {{
  border-bottom-color: var(--color-positive) !important;
  color: var(--color-positive) !important;
}}
.react-aria-SelectionIndicator {{
  background-color: var(--color-positive) !important;
}}
[data-testid="stButtonGroup"] button[aria-pressed="true"] {{
  background-color: rgba(34,197,94,.1) !important;
  border-color: var(--color-positive) !important;
  color: var(--color-positive) !important;
}}
[data-testid="stMultiSelectTagsContainer"] span[role="group"] {{
  background-color: var(--color-positive) !important;
  color: #04140A !important;
}}
[data-testid="stMultiSelectTagsContainer"] span[role="group"] svg {{
  color: #04140A !important;
}}
[data-testid="stCheckbox"] [data-selected="true"] * {{
  background-color: var(--color-positive) !important;
}}

/* Mismo patrón otra vez, ahora en la CABECERA de un expander (el "▷
   Conexión y licencia" plegable): reportado en el móvil, la barra entera
   sale en blanco con el texto casi invisible — el fondo oscuro no llega
   al <summary> nativo. st.expander usa <details>/<summary>; el <summary>
   no tiene su propio data-testid, así que va por descendiente dentro de
   stExpander (sí lo tiene). */
[data-testid="stExpander"] summary {{
  background-color: var(--card) !important;
  color: var(--fg) !important;
}}
[data-testid="stExpander"] summary:hover {{
  background-color: var(--panel) !important;
}}
[data-testid="stExpander"] summary svg {{
  color: var(--fg) !important;
}}
/* El propio <details> (el cuerpo desplegado) y stExpanderDetails, mismo
   motivo que el resto de contenedores nativos de más arriba. */
[data-testid="stExpander"] details,
[data-testid="stExpanderDetails"] {{
  background-color: var(--card) !important;
  border-color: var(--border) !important;
}}

@media (prefers-reduced-motion: reduce) {{
  * {{ transition:none !important; animation:none !important; }}
}}
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)

# Iconos SVG (Feather/Lucide, misma familia lineal, sin emojis). Sólo el interior.
ICONS = {
    "trending-up": '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    "trending-down": '<polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/><polyline points="17 18 23 18 23 12"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "bar-chart": '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
    "layers": '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "target": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    "line-chart": '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
    "thumb-up": '<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>',
    "thumb-down": '<path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/>',
    "pie-chart": '<path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/>',
    "arrows-exchange": '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/>'
                       '<polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "file-text": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
                '<polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>'
                '<line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "alert-circle": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
                    '<line x1="12" y1="16" x2="12.01" y2="16"/>',
}


def svg(name: str, size: int = 18) -> str:
    return (f'<svg class="vf-ico" width="{size}" height="{size}" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def section_header(icon: str, title: str):
    st.markdown(f'<div class="vf-sec">{svg(icon, 20)}<h3>{title}</h3></div>',
                unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Licencia (suscripción de Substack) — ver q4_license.py
# --------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_valid_codes_cached() -> list[str] | None:
    # Cacheado una hora: evita pedir el manifiesto en cada rerun de
    # Streamlit (un clic en cualquier widget vuelve a ejecutar main()).
    return L.fetch_valid_codes()


TRIAL_MONTHS = 6  # histórico máximo sincronizable sin licencia activa


def license_gate() -> str:
    """Nunca bloquea del todo — la conversión pasa por dejar probar la app,
    no por dejarla fuera. Devuelve "full" (licencia válida: sin límites) o
    "trial" (sin licencia — o caducada, o nunca hubo). sidebar_source() usa
    "trial" para capar la sincronización a TRIAL_MONTHS meses, y main() para
    bloquear Efecto divisa/Operaciones/Informe.

    El campo de licencia se pinta SIEMPRE, no solo cuando falta — así se
    puede introducir o renovar el código en cualquier momento, no solo la
    primera vez. Lo que precarga y guarda en disco es el ÚLTIMO CÓDIGO
    VÁLIDO conocido, no lo último que se haya tecleado: un intento fallido
    no borra el que ya funcionaba.

    Se llama DENTRO del expander colapsable de main() — por eso usa st.X
    en vez de st.sidebar.X (así respeta el contenedor activo; st.sidebar.X
    se saltaría el expander e iría directo a la raíz del sidebar)."""
    saved = L.LicenseState.load(LICENSE_PATH)  # último código VÁLIDO conocido

    st.markdown(f'<div class="vf-sec" style="margin:4px 0 2px;">{svg("target", 16)}'
               '<h3 style="font-size:13px;">Licencia</h3></div>', unsafe_allow_html=True)
    typed = st.text_input(
        "Código de licencia", value=saved.code,
        help="Lo tienes en el último correo de pago de tu suscripción de Substack.")
    if typed != saved.code:
        _fetch_valid_codes_cached.clear()  # código nuevo: no esperar a que caduque la caché de 1h
    st.button("Verificar licencia")  # el propio cambio de campo ya dispara el rerun

    attempt = L.LicenseState(code=typed, last_ok=saved.last_ok)
    ok, level, msg = L.evaluate(attempt, _fetch_valid_codes_cached())

    if ok and level != "warning":
        # Validado de verdad contra el manifiesto (o licencia desactivada,
        # §evaluate): éste pasa a ser el último código válido guardado.
        attempt.save(LICENSE_PATH)
        st.caption("Licencia activa ✓")
        return "full"

    if ok:  # level == "warning": modo de gracia offline, por el código YA
        # guardado — no se ha comprobado lo tecleado, así que no se toca
        # el fichero (evaluate() tampoco cambia last_ok en este caso).
        st.warning(msg)
        return "full"

    # No autoriza: NO se toca el último código válido guardado — un
    # intento fallido (typo, código de otro mes) no debe borrar el bueno.
    st.info(f"**Modo de prueba** — histórico limitado a {TRIAL_MONTHS} meses y "
           "Efecto divisa/Operaciones/Informe bloqueadas hasta verificar la "
           "licencia. El código está en el último correo de pago de tu "
           "suscripción de Substack.")
    return "trial"


def _tab_gate(license_mode: str) -> bool:
    """Efecto divisa/Operaciones/Informe son de pago (a petición expresa):
    sin licencia activa no muestran nada, solo el aviso. Métricas, Cartera
    y Configuración quedan fuera de esta guarda — se ven siempre."""
    if license_mode == "full":
        return True
    st.info("Disponible con licencia activa. El código está en el último correo de pago "
            "de tu suscripción de Substack — despliega **Conexión y licencia**, en el "
            "sidebar, e introdúcelo ahí.")
    return False


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------

@st.cache_data(show_spinner="Parseando extractos…")
def load_paths(paths: tuple[str, ...]) -> P.Dataset:
    return P.load(list(paths))


def _queue_folder_write(paths: list[str], state_path: str) -> None:
    """Encola ficheros para que _connection_fields() se los pase al
    componente de carpeta en el PRÓXIMO rerun (Camino A, fase "guardar al
    salir", 1 de 3 — ver components/folder_picker/index.html). Incluye
    siempre el estado de sincronización además de los XML, para que una
    futura restauración (fase 3) tenga también windows_done/watermark, no
    solo los extractos.

    Si el usuario no ha elegido carpeta (o su navegador no la soporta,
    Safari/Firefox), el componente ignora esto sin más — RAW_DIR en el
    servidor sigue siendo la copia real, esto es solo una copia de más."""
    files = []
    for p in paths + ([state_path] if os.path.exists(state_path) else []):
        try:
            with open(p, "rb") as f:
                files.append({"name": os.path.basename(p),
                             "content_b64": base64.b64encode(f.read()).decode("ascii")})
        except OSError:
            continue
    if files:
        existing = st.session_state.get("_pending_folder_writes", [])
        st.session_state["_pending_folder_writes"] = existing + files


def _connection_fields() -> tuple[str, str]:
    """Token, Query ID, "Recordar" y el selector de carpeta. Se llama DENTRO
    del expander colapsable de main() (igual que license_gate(), ver su
    docstring) — de ahí st.X en vez de st.sidebar.X."""
    # Un único camino: conectar con IBKR. Sin opción de subir XML a mano —
    # lo ya descargado en sesiones anteriores se sigue leyendo solo, más
    # abajo, sin que haga falta tocar el token para volver a verlo.
    st.caption("El token da acceso de lectura a tus extractos.")
    saved_token, saved_qid = _load_ibkr_creds()
    token = st.text_input("Token", type="password", value=saved_token)
    qid = st.text_input("Query ID", value=saved_qid)
    remember = st.checkbox(
        "Recordar en este equipo", value=bool(saved_token or saved_qid),
        help="Guarda el token y el Query ID en tu propio disco "
             f"({IBKR_CREDS_PATH}) para no tener que repetirlos cada vez. "
             "Nunca sale de tu ordenador.")
    if remember:
        if token and qid and (token, qid) != (saved_token, saved_qid):
            _save_ibkr_creds(token, qid)
    elif saved_token or saved_qid:
        _clear_ibkr_creds()

    # "files_to_write" solo va en la llamada si hay algo pendiente (§
    # _queue_folder_write, encolado tras la última sincronización) — sin
    # esto, el componente reenviaría los mismos ficheros en cada rerun.
    picker_kwargs = {"key": "folder_picker"}
    pending = st.session_state.pop("_pending_folder_writes", None)
    if pending:
        picker_kwargs["files_to_write"] = pending

    # Fase 3 (restaurar al entrar): mientras el servidor no tenga extractos
    # — contenedor efímero de Streamlit Cloud recién reiniciado, o primera
    # carga de esta sesión — le pedimos al navegador lo que tenga guardado
    # en la carpeta recordada. Ver want_restore en components/folder_picker/
    # index.html.
    picker_kwargs["want_restore"] = not bool(glob.glob(os.path.join(RAW_DIR, "*.xml")))

    # "Borrar todo" (§_danger_zone) deja esto pendiente para el rerun
    # siguiente al suyo propio — session_state.clear() no lo borra porque
    # se pone DESPUÉS de esa llamada.
    if st.session_state.pop("_wipe_folder_pending", False):
        picker_kwargs["wipe_folder"] = True

    picker = _folder_picker(**picker_kwargs)
    if picker and picker.get("picked"):
        st.session_state["chosen_folder_name"] = picker["name"]
        # La carpeta confirmada es un ESPEJO de RAW_DIR, no solo de lo
        # nuevo: en cada rerun (cualquier clic en cualquier sitio de la
        # app, no hace falta que sea aquí) se compara la lista actual de
        # RAW_DIR contra la última que se mandó a la carpeta EN ESTA
        # SESIÓN, y si ha cambiado se reenvía TODA la lista otra vez.
        #
        # Antes esto era un envío de una sola vez (una bandera de sesión):
        # si esa única entrega se perdía — la sincronización termina bien
        # en el servidor pero el st.rerun() que la lleva al componente cae
        # sobre una conexión ya muerta por una espera larga a IBKR (visto
        # en la práctica: Juan recargó la página y los extractos SÍ
        # estaban en RAW_DIR, pero nunca llegaron a la carpeta) — no había
        # ningún reintento; ese fichero se quedaba fuera de la carpeta
        # para siempre. Reenviar la lista entera en cada cambio es seguro
        # y barato porque el propio componente se salta ahora los .xml que
        # ya tiene (mismo nombre = mismo contenido, llevan un digest del
        # contenido en el nombre — ver writeFiles() en index.html): lo
        # único que de verdad viaja es lo que de verdad falte allí.
        if qid:
            state_path = os.path.join(RAW_DIR, f"state_{qid}.json")
            current = tuple(sorted(glob.glob(os.path.join(RAW_DIR, "*.xml"))))
            if current and current != st.session_state.get("_folder_sync_snapshot"):
                st.session_state["_folder_sync_snapshot"] = current
                _queue_folder_write(list(current), state_path)
                st.rerun()
    if picker and picker.get("wrote") is not None:
        wrote, skipped = picker["wrote"], picker.get("skipped", 0)
        if wrote > 0 or skipped > 0:
            bits = []
            if wrote:
                bits.append(f"{wrote} nuevo(s)")
            if skipped:
                bits.append(f"{skipped} ya estaba(n)")
            st.caption(f"✓ Carpeta al día: " + ", ".join(bits) + ".")
        if picker.get("errors"):
            st.caption("⚠ Algunos no se pudieron guardar: " +
                      "; ".join(picker["errors"][:3]))
    if picker and picker.get("wiped") is not None:
        st.caption(f"🗑 Carpeta vaciada: {picker['wiped']} fichero(s) borrados.")
        if picker.get("wipe_errors"):
            st.caption("⚠ Algunos no se pudieron borrar: " +
                      "; ".join(picker["wipe_errors"][:3]))
    if picker and picker.get("restored_files"):
        _restore_from_folder(picker["restored_files"])

    return token, qid


def _restore_from_folder(files: list[dict]) -> None:
    """Fase 3/3 de Camino A: escribe en RAW_DIR lo que el navegador acaba de
    leer de la carpeta recordada (ver readAllForRestore() en
    components/folder_picker/index.html) — solo llega cuando el servidor
    empezó sin datos Y el navegador ya tenía carpeta con permiso confirmado.

    Guarda de sesión obligatoria: Streamlit NO limpia el valor devuelto por
    un componente entre reruns — sin `_restore_done`, el siguiente rerun
    (el que provoca el propio st.rerun() de aquí abajo) volvería a ver el
    mismo `restored_files` y reintentaría para siempre."""
    if st.session_state.get("_restore_done"):
        return
    st.session_state["_restore_done"] = True
    os.makedirs(RAW_DIR, exist_ok=True)
    written = 0
    for f in files:
        name = f.get("name", "")
        # Nombres inesperados (rutas, ocultos) nunca deberían llegar —
        # readAllForRestore() ya filtra por patrón, esto es cinturón y
        # tirantes contra escribir fuera de RAW_DIR.
        if not name or "/" in name or "\\" in name or name.startswith("."):
            continue
        dest = os.path.join(RAW_DIR, name)
        if os.path.exists(dest):
            continue  # no pisar nada que el servidor ya tenga
        try:
            with open(dest, "wb") as fh:
                fh.write(base64.b64decode(f["content_b64"]))
            written += 1
        except (OSError, KeyError, ValueError, TypeError):
            continue
    if written:
        st.toast(f"↺ Restaurados {written} fichero(s) desde tu carpeta.", icon="✅")
        st.rerun()


def _danger_zone():
    """Borra TODO (extractos, estado de sincronización, credenciales
    recordadas, licencia, y — si hay carpeta elegida — su contenido
    también) — para empezar de cero. Mismo sitio que _connection_fields():
    dentro del expander colapsable de main()."""
    st.divider()
    st.caption("Borra todos los extractos descargados, el estado de "
              "sincronización, el token/Query ID recordados en este equipo, "
              "el código de licencia y — si has elegido carpeta — los "
              "extractos que tengas guardados ahí. No se puede deshacer — "
              "la próxima vez hay que sincronizar desde cero.")
    confirm_wipe = st.checkbox("Confirmo que quiero borrarlo todo", key="confirm_wipe_all")
    if st.button("Borrar todo y empezar de nuevo", disabled=not confirm_wipe):
        shutil.rmtree(RAW_DIR, ignore_errors=True)
        st.session_state.clear()
        # DESPUÉS de clear() a propósito — si no, se borraría a sí mismo
        # antes de que _connection_fields() llegue a leerlo en el próximo
        # rerun (ver picker_kwargs["wipe_folder"] ahí).
        st.session_state["_wipe_folder_pending"] = True
        st.rerun()


def _incremental_parse_fn(raw_path: str) -> tuple[dict[str, float], list[str]]:
    """(nav_por_fecha, fechas) del bloque recién descargado — mismo
    callback que necesita q4_sync.daily_job(), ver q4_daily.py:parse_fn."""
    block = P.load([raw_path])
    return P.nav_series(block), block.dates


def _incremental_recompute_fn() -> dict[str, float]:
    """TWR EUR de cada año CERRADO sobre TODO el histórico (canario §19.6),
    para q4_sync.daily_job() — mismo cálculo que q4_daily.py:recompute_fn,
    duplicado a propósito: son entrypoints independientes (uno es el cron,
    éste la app interactiva), no comparten proceso."""
    all_raws = sorted(glob.glob(os.path.join(RAW_DIR, "*.xml")))
    ds_all = P.load(all_raws)
    dates = ds_all.dates
    this_year = dt.date.today().year
    out: dict[str, float] = {}
    for y in sorted({int(d[:4]) for d in dates}):
        if y >= this_year:
            continue
        prior = [d for d in dates if d < f"{y}0101"]
        in_year = [d for d in dates if d[:4] == str(y)]
        if not prior or not in_year:
            continue
        try:
            out[str(y)] = E.build_series(ds_all, prior[-1], in_year[-1]).total() * 100
        except Exception:
            continue
    return out


def _run_incremental_sync(token: str, qid: str, state_path: str):
    """Sincronización incremental manual: el mismo q4_sync.daily_job() que
    usa el cron (q4_daily.py), disparado a mano — para quien no tiene el
    disparador de launchd/cron instalado y quiere ponerse al día sin pedir
    otra vez todo el histórico."""
    from q4_ingest import FlexClient
    from q4_sync import daily_job

    lock_path = f"{state_path}.lock"
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < LOCK_STALE_S:
            st.error(f"Ya hay una sincronización en marcha para esta query "
                     f"(empezó hace {int(age)}s). Espera a que termine.")
            return

    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "w") as f:
        f.write(str(time.time()))
    try:
        client = FlexClient(token=token, query_id=qid, raw_dir=RAW_DIR)
        with st.status("Sincronizando desde el último día…", expanded=True) as status:
            bar = st.progress(0.0)
            try:
                # Antes esto era un único status.write() estático seguido de
                # una llamada bloqueante entera — mientras IBKR genera el
                # informe (asíncrono en su lado, puede ser la parte más
                # larga) no había ninguna señal de que algo seguía en marcha.
                # on_progress da 3 pasos fijos (ver daily_job() en q4_sync.py)
                # en vez de dejarlo mudo.
                def _tick(i, n, msg):
                    bar.progress(i / n)
                    status.write(f"({i}/{n}) {msg}")

                res = daily_job(client, state_path, qid, _incremental_parse_fn,
                                _incremental_recompute_fn, on_progress=_tick)
                bar.progress(1.0)
                if res["status"] == "ok":
                    status.update(label="Sincronización completa", state="complete")
                    wm = res.get("watermark", "")
                    wm_fmt = (dt.datetime.strptime(wm, "%Y%m%d").strftime("%d/%m/%Y")
                              if wm else "—")
                    st.session_state.pop("paths", None)  # recargar de disco, no lo de la sesión
                    st.success(f"Al día: {wm_fmt}. "
                              f"{len(res.get('new_dates', []))} sesión(es) nueva(s).")
                    if res.get("golden_drift"):
                        st.warning("Los años cerrados han cambiado: " +
                                  " · ".join(res["golden_drift"]))
                    if res.get("raw_path"):
                        _queue_folder_write([res["raw_path"]], state_path)
                        st.rerun()  # sin esto, el fichero no llega al componente hasta el próximo clic
                elif res["status"] == "no_new_data":
                    status.update(label="Ya estabas al día", state="complete")
                    st.info("Sin sesiones nuevas — nada que traer.")
                else:
                    status.update(label="No se pudo sincronizar", state="error")
                    st.error(res.get("reason", res["status"]))
            except Exception as e:
                status.update(label="Error en la sincronización", state="error")
                st.error(str(e))
    finally:
        if os.path.exists(lock_path):
            os.remove(lock_path)


def _history_start_field(qid: str, license_mode: str) -> pd.Timestamp:
    """"Importar histórico desde" — dentro del expander colapsable de
    main() (igual que license_gate()/_connection_fields(): st.X, no
    st.sidebar.X). El resto de lo relacionado con esto (aviso de qué fecha
    quedó ya fijada, "Reanclar a otra fecha de inicio") se queda en
    sidebar_source(), fuera del expander — Streamlit no permite anidar un
    expander dentro de otro, y "Reanclar" ya es uno."""
    from q4_sync import SyncState
    state_path = os.path.join(RAW_DIR, f"state_{qid}.json")
    existing_start = SyncState.load(state_path, qid).history_start if qid else ""

    trial_floor = None
    if license_mode == "trial":
        trial_floor = (pd.Timestamp.today().normalize()
                       - pd.DateOffset(months=TRIAL_MONTHS)).date()
        st.caption(f"Modo de prueba: histórico limitado a los últimos "
                  f"{TRIAL_MONTHS} meses (desde "
                  f"{trial_floor.strftime('%d/%m/%Y')}). Con licencia "
                  "activa se levanta el límite.")

    default_start = pd.Timestamp(existing_start) if existing_start else pd.Timestamp("2023-01-01")
    if trial_floor is not None:
        default_start = max(default_start, pd.Timestamp(trial_floor))
        return st.date_input("Importar histórico desde", default_start,
                             min_value=trial_floor, format="DD/MM/YYYY")
    return st.date_input("Importar histórico desde", default_start, format="DD/MM/YYYY")


def sidebar_source(license_mode: str, token: str, qid: str, start: pd.Timestamp) -> P.Dataset | None:
    from q4_sync import SyncState  # noqa: E402  (import local: sólo hace falta aquí)

    # Un fichero de estado POR QUERY (state_<queryId>.json), no uno solo
    # compartido: con un único ./raw/state.json, sincronizar una segunda
    # query mezclaba su histórico con el de la primera (o, con la guarda que
    # había antes, simplemente lo bloqueaba). Cada query lleva su propio
    # `history_start`/`windows_done`/`watermark` en su propio fichero, así
    # que dos cuentas conviven sin pisarse — los XML crudos ya iban
    # namespaced por query_id en el nombre de fichero (q4_ingest.FlexClient),
    # esto alinea el estado con eso.
    state_path = os.path.join(RAW_DIR, f"state_{qid}.json")

    # `state.history_start` gobierna la rejilla de DESCARGA (ver
    # q4_sync.backfill: siempre planifica contra ese valor fijo, nunca contra
    # lo que se escriba aquí, así que teclear una fecha distinta ya NO genera
    # ventanas duplicadas/solapadas). Este campo es libre y controla algo
    # aparte: qué parte de lo YA descargado se carga en esta sesión — así
    # puedes seguir haciendo pruebas "sólo con 2026" aunque el histórico
    # completo arranque antes.
    existing_start = SyncState.load(state_path, qid).history_start if qid else ""

    # Alerta del último día sincronizado + botón de sincronización
    # incremental manual: el mismo q4_sync.daily_job() que ya usa el cron
    # (q4_daily.py), pero disparado a mano — pide solo desde ese día hasta
    # el último cierre disponible (con el solape de 10 días de siempre,
    # §19.3), mucho más rápido que "Sincronizar" cuando ya se tiene el
    # histórico y solo hace falta ponerse al día.
    state_now = SyncState.load(state_path, qid) if qid else None
    if state_now and not state_now.watermark and state_now.windows_done:
        # Instalación existente que solo ha usado "Sincronizar" (nunca ha
        # corrido q4_daily.py): sembrar el watermark desde lo ya
        # descargado, igual que hace q4_daily.py en su primera ejecución —
        # si no, esta sección nunca llegaría a aparecer.
        try:
            state_now.watermark = load_paths(
                tuple(w[2] for w in state_now.windows_done)).dates[-1]
            state_now.save(state_path)
        except Exception:
            pass
    if state_now and state_now.watermark:
        wm_d = dt.datetime.strptime(state_now.watermark, "%Y%m%d").date()
        st.sidebar.info(f"Último día sincronizado: {wm_d.strftime('%d/%m/%Y')}.")
        if st.sidebar.button("Sincronizar desde ese día", disabled=not (token and qid)):
            _run_incremental_sync(token, qid, state_path)

    # `start` ya viene calculado de _history_start_field(), dentro del
    # expander "Conexión y licencia" (§main) — aquí solo se usa. La única
    # razón para seguir sabiendo si hay modo de prueba es el aviso de más
    # abajo, en el botón "Sincronizar".
    trial_floor = ((pd.Timestamp.today().normalize() - pd.DateOffset(months=TRIAL_MONTHS)).date()
                   if license_mode == "trial" else None)

    if existing_start:
        d = dt.datetime.strptime(existing_start, "%Y%m%d").date()
        st.sidebar.caption(f"La descarga arranca en {d.strftime('%d/%m/%Y')} "
                           "(primera sincronización de esta query; no cambia "
                           "aunque pongas otra fecha en «Importar histórico "
                           "desde», arriba en «Conexión y licencia»). Lo que "
                           "pongas ahí sí decide qué parte se carga en el panel.")
        with st.sidebar.expander("Reanclar a otra fecha de inicio"):
            st.caption("Borra el punto de partida y las ventanas descargadas "
                       "de ESTA query — no toca los XML ya guardados en "
                       "disco, sólo deja de usarlos. El próximo "
                       "'Sincronizar' vuelve a pedir el histórico completo "
                       "desde cero, con la fecha de «Importar histórico "
                       "desde».")
            confirm = st.checkbox("Confirmo que quiero reiniciar el "
                                  "histórico de esta query")
            if st.button("Reiniciar histórico", disabled=not confirm):
                SyncState(query_id=qid).save(state_path)
                st.session_state.pop("paths", None)
                st.rerun()

    if st.sidebar.button("Sincronizar", disabled=not (token and qid)):
        from q4_ingest import FlexClient, validate_query
        from q4_sync import backfill

        if trial_floor is not None:
            st.warning(f"Sin licencia activa: la descarga se limita a los últimos "
                       f"{TRIAL_MONTHS} meses (desde {trial_floor.strftime('%d/%m/%Y')}). "
                       "Verifica tu licencia en el sidebar para levantar el límite.")

        # Candado por query: el limitador de ritmo de FlexClient (1 req/s,
        # 10/min) vive EN MEMORIA, dentro de esa instancia — no es global. Si
        # dos ejecuciones piden a la vez contra la misma query (dos pestañas,
        # una página recargada mientras la sincronización anterior seguía
        # corriendo en el servidor, un script suelto tipo probe_ibkr.py a la
        # vez que la app), cada una respeta su propio ritmo pero IBKR ve la
        # suma — eso basta para un 1025 sin que nadie haya reintentado a
        # mano. El candado es un fichero con marca de tiempo: si ya hay uno
        # reciente, se rechaza el clic en vez de sumar otra ejecución.
        lock_path = f"{state_path}.lock"
        if os.path.exists(lock_path):
            age = time.time() - os.path.getmtime(lock_path)
            if age < LOCK_STALE_S:
                st.error(
                    f"Ya hay una sincronización en marcha para esta query "
                    f"(empezó hace {int(age)}s, en esta u otra pestaña/sesión). "
                    "Pedir a la vez desde dos sitios es lo que suele acabar en "
                    "un 1025. Espera a que termine; si sabes que en realidad "
                    f"no hay ninguna corriendo (se quedó colgada), borra "
                    f"{lock_path} y reintenta.")
                return None

        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        with open(lock_path, "w") as f:
            f.write(str(time.time()))
        try:
            client = FlexClient(token=token, query_id=qid, raw_dir=RAW_DIR)
            # st.status en el CUERPO PRINCIPAL, no en la barra lateral: es el
            # sitio donde luego aparece el dashboard, así que el usuario ve
            # el avance justo donde va a mirar en vez de tener que fijarse en
            # la barra lateral. Un log visible y persistente de lo que se
            # está haciendo en cada momento, para que nunca parezca colgada
            # aunque el parseo de XML grandes tarde minutos.
            with st.status("Conectando con IBKR…", expanded=True) as status:
                bar = st.progress(0.0)
                # Línea que se ACTUALIZA en su sitio (st.empty), no una más
                # por intento — con un bloque grande, get_statement() puede
                # sondear a IBKR 12 veces antes de rendirse (hasta ~8 min) y,
                # hasta ahora, no mandaba NADA mientras tanto. Ese silencio
                # es tiempo de sobra para que un proxy intermedio (Streamlit
                # Cloud está detrás de uno) dé la conexión del navegador por
                # muerta aunque el proceso siga vivo en el servidor y acabe
                # guardando bien — exactamente el síntoma que reportó Juan:
                # "recargué la página y los datos ya estaban". Cada intento
                # manda ahora tráfico al websocket, evitando ese corte.
                poll_line = st.empty()

                def _poll_tick(i, n, fase):
                    verbo = "Pidiendo la referencia" if fase == "send" else "Esperando a IBKR"
                    poll_line.markdown(f"↻ {verbo} (intento {i}/{n})…")

                try:
                    status.write("Validando la query…")
                    probe = client.fetch(on_progress=_poll_tick)
                    poll_line.empty()
                    v = validate_query(open(probe).read())
                    if not v["ok"]:
                        status.update(label="Query inválida", state="error")
                        st.error(v["note"])
                        return None
                    status.write(f"Cuenta {', '.join(v['accounts'])} · query correcta")

                    state = SyncState.load(state_path, qid)
                    before_n = len(state.windows_done)

                    def _prog(i, n, fd, td):
                        bar.progress(i / n)
                        status.write(f"Descargando bloque {i}/{n}: {fd} → {td}")

                    backfill(client, state, state_path, start.strftime("%Y%m%d"),
                             on_progress=_prog, on_poll_progress=_poll_tick)
                    poll_line.empty()
                    bar.progress(1.0)
                    # Si no había ventanas pendientes (todo ya descargado de una
                    # sincronización anterior), on_progress no se llama nunca y el
                    # log se queda mudo entre "query correcta" y el final — eso es
                    # justo lo que se leyó como "no pasa nada". Decirlo explícito.
                    fetched = len(state.windows_done) - before_n
                    status.write("Ya tenías todo el histórico pedido: nada nuevo "
                                 "que descargar." if fetched == 0 else
                                 f"Descarga completa: {fetched} bloque(s) nuevo(s).")

                    # `windows_done` es un histórico ACUMULADO (útil para el
                    # incremental diario), pero lo que se carga en ESTA sesión
                    # debe respetar lo pedido en "Histórico desde": sólo los
                    # bloques cuyo cierre (td) cae en o después de esa fecha.
                    cutoff = start.strftime("%Y%m%d")
                    paths = tuple(w[2] for w in state.windows_done if w[1] >= cutoff)
                    status.write(f"Parseando {len(paths)} extractos…")
                    st.session_state["paths"] = paths
                    load_paths(paths)          # parsea aquí, con feedback visible
                    # Sin `expanded=False`: si se colapsa solo, el único rastro de
                    # que ha ido bien es la etiqueta de la caja — fácil de leer
                    # como "no ha pasado nada" (fue justo lo que reportaron). Se
                    # deja abierta y además una confirmación aparte que no se
                    # colapsa con la caja.
                    status.update(label="Sincronización completa", state="complete",
                                  expanded=True)
                    st.success(f"Listo: {fetched} bloque(s) nuevo(s) · "
                              f"{len(paths)} extractos cargados en el panel.")
                    if fetched:
                        new_paths = [w[2] for w in state.windows_done[before_n:]]
                        _queue_folder_write(new_paths, state_path)
                        st.rerun()  # sin esto, no llega al componente hasta el próximo clic
                except Exception as e:
                    status.update(label="Error en la sincronización", state="error",
                                  expanded=True)
                    st.error(str(e))
                    return None
        finally:
            if os.path.exists(lock_path):
                os.remove(lock_path)
    if st.session_state.get("paths"):
        return load_paths(st.session_state["paths"])

    # Nada sincronizado todavía EN ESTA SESIÓN (recarga de página, sesión
    # nueva): si ya hay extractos en el almacén de una sincronización
    # anterior, cargarlos solos — el panel no debe quedarse en blanco
    # esperando a que se reintroduzca el token sólo para volver a ver lo
    # que ya se tenía.
    local = sorted(glob.glob(os.path.join(RAW_DIR, "*.xml"))) or \
        sorted(glob.glob("./data/*.xml"))
    if local:
        st.sidebar.caption(f"Usando {len(local)} extractos del almacén ({RAW_DIR})")
        return load_paths(tuple(local))
    return None


# --------------------------------------------------------------------------
# Presentación
# --------------------------------------------------------------------------

def pct(v, sign=True, dec=2):
    if v is None:
        return "n/d"
    return f"{'+' if sign and v > 0 else ''}{v:.{dec}f} %"


def fmt_date(d: str | None) -> str:
    """AAAAMMDD -> DD/MM/AAAA. Las fechas de IBKR llegan compactas y así salían
    crudas en las tablas ("20260817"), ilegibles de un vistazo."""
    return f"{d[6:]}/{d[4:6]}/{d[:4]}" if d and len(d) == 8 and d.isdigit() else (d or "—")


def kpi_cards(items: list[dict]):
    """Stat cards. item = {label, value, tone in {pos,neg,neutral}, icon, hint?, bench?}.

    `bench` = {"ticker": "SPY", "value": "+119,30 %", "better": True|False|None}
    añade, dentro de la misma tarjeta, el mismo dato para el benchmark —
    muestra discontinua ámbar, igual convención que en los gráficos — para leer
    estrategia y benchmark de un vistazo sin bajar de página. `better` pinta una
    insignia junto al VALOR de la Estrategia (no en la línea pequeña de abajo,
    para que sea visible de un vistazo): pulgar arriba verde si la bate, pulgar
    abajo rojo si la empeora; None no pinta insignia (p. ej. sin dato anualizable).

    El tono da color, pero el valor SIEMPRE lleva su signo (+/−): la dirección no
    depende sólo del color (accesibilidad / daltonismo)."""
    cards = ""
    for it in items:
        badge = ""
        if it.get("bench") and it["bench"].get("better") is not None:
            better = it["bench"]["better"]
            cls, icon_name = ("vf-pos-bg", "thumb-up") if better else ("vf-neg-bg", "thumb-down")
            badge = f'<span class="vf-verdict-badge {cls}">{svg(icon_name, 18)}</span>'
        value_html = (f'<div class="vf-value-row"><span class="vf-value vf-{it["tone"]}">'
                     f'{it["value"]}</span>{badge}</div>' if badge else
                     f'<div class="vf-value vf-{it["tone"]}">{it["value"]}</div>')

        extra = f'<div class="vf-hint">{it["hint"]}</div>' if it.get("hint") else ""
        if it.get("bench"):
            b = it["bench"]
            extra += (f'<div class="vf-hint-bench"><span class="vf-dash-swatch"></span>'
                     f'<span>{b["ticker"]} {b["value"]}</span></div>')
        cards += (
            f'<div class="vf-card"><div class="vf-card-top">{svg(it["icon"])}'
            f'<span class="vf-label">{it["label"]}</span></div>{value_html}{extra}</div>')
    st.markdown(f'<div class="vf-kpis">{cards}</div>', unsafe_allow_html=True)


def _tone(v) -> str:
    if v is None:
        return "neutral"
    return "pos" if v >= 0 else "neg"


def _style_fig(fig: go.Figure, height: int = 340, ytitle: str | None = None) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=6, r=6, t=30, b=6),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Fira Sans, sans-serif", color=FG, size=13),
        hoverlabel=dict(font=dict(family="Fira Code, monospace"), bgcolor=CARD,
                        bordercolor=BORDER),
        legend=dict(orientation="h", y=1.14, x=0, font=dict(size=12)),
        xaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, linecolor=BORDER),
        yaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, title=ytitle),
    )
    return fig


def equity_chart(s_total: E.Series, s_strat: E.Series, currency: str,
                 bench: dict | None = None):
    d = [pd.to_datetime(x, format="%Y%m%d") for x in s_total.dates]
    fig = go.Figure()
    # tres series distinguibles por COLOR y por ESTILO de línea (no sólo color)
    # hovertemplate a 2 decimales: sin él, Plotly muestra el float con toda
    # su precisión (10+ decimales) al pasar el ratón.
    fig.add_scatter(x=d, y=s_total.index(), name=f"Total {currency}",
                    line=dict(color=SERIES, width=2),
                    hovertemplate="%{y:.2f}<extra></extra>")
    fig.add_scatter(x=d, y=s_strat.index(), name="Estrategia (FX neutral)",
                    line=dict(color=POS, width=2, dash="dash"),
                    hovertemplate="%{y:.2f}<extra></extra>")
    if bench:
        fig.add_scatter(x=d, y=bench["index"], name=bench["name"],
                        line=dict(color=BENCH, width=1.75, dash="dot"),
                        hovertemplate="%{y:.2f}<extra></extra>")
    _style_fig(fig, height=360, ytitle="Índice TWR (100 = inicio)")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Índice TWR normalizado a 100, no patrimonio: los depósitos y "
               "retiradas no aparecen como saltos. La divergencia entre ambas "
               "líneas es el efecto divisa acumulado."
               + (f" El benchmark ({bench['name']}) se compara contra la "
                  "Estrategia, sistema contra sistema y en su divisa local (§15.2)."
                  if bench else ""))


def _drawdown_from_index(idx: list[float]) -> list[float]:
    """Drawdown diario (%) = I(t)/máx previo − 1 sobre un índice TWR (§14.1)."""
    out, peak = [], -1.0
    for v in idx:
        peak = max(peak, v)
        out.append((v / peak - 1) * 100)
    return out


def drawdown_chart(s_total: E.Series, s_strat: E.Series, currency: str,
                   bench: dict | None = None):
    d = [pd.to_datetime(x, format="%Y%m%d") for x in s_total.dates]
    fig = go.Figure()
    fig.add_scatter(x=d, y=_drawdown_from_index(s_total.index()), name=f"Total {currency}",
                    line=dict(color=SERIES, width=1.5), fill="tozeroy",
                    fillcolor="rgba(56,189,248,0.10)",
                    hovertemplate="%{y:.2f} %<extra></extra>")
    fig.add_scatter(x=d, y=_drawdown_from_index(s_strat.index()), name="Estrategia (FX neutral)",
                    line=dict(color=POS, width=1.5, dash="dash"),
                    hovertemplate="%{y:.2f} %<extra></extra>")
    if bench:
        fig.add_scatter(x=d, y=_drawdown_from_index(bench["index"]), name=bench["name"],
                        line=dict(color=BENCH, width=1.5, dash="dot"),
                        hovertemplate="%{y:.2f} %<extra></extra>")
    _style_fig(fig, height=360, ytitle="Drawdown (%)")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Drawdown = caída desde el máximo anterior del índice TWR, sobre "
               "serie diaria (§14.1). La brecha entre Total y Estrategia es la "
               "caída que aportó la divisa, no las posiciones."
               + (f" {bench['name']} en línea punteada, para ver de un vistazo si "
                  "tu peor caída fue mayor o menor que la del mercado."
                  if bench else ""))


def yearly_bar_chart(yt: pd.DataFrame, bench_col: str | None = None):
    """Comparativa por año: barra horizontal ordenada por AÑO (más reciente
    arriba). Verde/rojo por signo, con el valor rotulado (+/−) para no depender
    del color. Si hay columna de benchmark, se marca como un TICK vertical
    ámbar sobre la barra de su mismo año — el patrón "bullet chart target":
    un punto de referencia, no una segunda barra, para no duplicar la lectura."""
    d = yt.dropna(subset=["Estrategia"]).copy()
    if d.empty:
        return
    # orden por año ascendente -> Plotly lo dibuja de abajo arriba, así que el
    # año más reciente queda arriba.
    d = d.sort_values(by="Año", key=lambda s: s.str[:4].astype(int))
    colors = [POS if v >= 0 else NEG for v in d["Estrategia"]]

    # El alto de cada barra debe ser SIEMPRE el mismo, filtres los años que
    # filtres. Plotly reparte el área de dibujo entera entre las categorías
    # del eje Y, así que si el alto total fuera un "suelo" fijo (p. ej.
    # max(190, 46*n)), con pocas barras ese suelo se repartía entre menos
    # categorías y las engordaba. La solución es sumar, no acotar: un margen
    # fijo para ejes/leyenda (CHROME_PX, no depende de n) más una franja fija
    # por barra (BAR_PX) — el área de dibujo crece siempre en línea recta con
    # n y el grosor de cada barra no cambia nunca.
    BAR_PX, CHROME_PX = 46, 100
    BENCH_TICK_PX = 17          # largo del tick ("line-ns") del benchmark
    # Ancho de la barra: un 33 % más que igualar el tick del benchmark (que es
    # lo que había antes) — a petición expresa, ya no van sincronizados.
    # `width` de go.Bar es la fracción del hueco por categoría (BAR_PX) que
    # ocupa la barra; el resto del hueco es el espacio ENTRE barras
    # consecutivas (gap = 1 - width), así que para dejarlo a la mitad se
    # calcula sobre ese gap, no sobre el ancho directamente.
    bar_width_prev = (BENCH_TICK_PX / BAR_PX) * 1.33
    bar_width = 1 - (1 - bar_width_prev) / 2

    fig = go.Figure(go.Bar(
        y=d["Año"], x=d["Estrategia"], orientation="h", marker_color=colors,
        name="Estrategia", width=bar_width,
        text=[f"{v:+.1f} %" for v in d["Estrategia"]], textposition="outside",
        textfont=dict(family="Fira Code, monospace", color=FG),
        # sin "+" en el spec: con él, Plotly (comprobado en 3.1.0) deja de
        # redondear y muestra el float con toda su precisión — el signo ya se
        # ve por el color de la barra y por la etiqueta de fuera (esa sí en
        # Python puro con f"{v:+.1f}", ajena al bug de hovertemplate).
        hovertemplate="%{y}: %{x:.2f} %<extra></extra>", cliponaxis=False))

    has_bench = bool(bench_col and bench_col in d.columns and d[bench_col].notna().any())
    if has_bench:
        db = d.dropna(subset=[bench_col])
        fig.add_trace(go.Scatter(
            x=db[bench_col], y=db["Año"], mode="markers", name=bench_col,
            marker=dict(symbol="line-ns", size=BENCH_TICK_PX, line=dict(color=BENCH, width=3)),
            hovertemplate=f"{bench_col}: " + "%{x:.2f} %<extra></extra>"))

    _style_fig(fig, height=CHROME_PX + BAR_PX * len(d) + (40 if has_bench else 0), ytitle=None)
    fig.update_layout(showlegend=has_bench, xaxis_title="Rentabilidad de la estrategia (%)")
    if has_bench:
        fig.update_layout(
            margin=dict(t=46),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, font=dict(size=12)))
    fig.update_yaxes(type="category")
    # margen a ambos lados: deja sitio a las etiquetas fuera de barra (sobre todo
    # la de una barra negativa) para que no pisen las etiquetas del eje. Si hay
    # marcador de benchmark, entra también en el rango para que no quede cortado.
    vals = [d["Estrategia"].min(), d["Estrategia"].max(), 0.0]
    if has_bench:
        vals += [db[bench_col].min(), db[bench_col].max()]
    xmin, xmax = min(vals), max(vals)
    span = max(xmax - xmin, 1.0)
    fig.update_xaxes(range=[xmin - span * 0.22, xmax + span * 0.22])
    st.plotly_chart(fig, use_container_width=True)
    if has_bench:
        st.caption(f"El tick ámbar es {bench_col} ese mismo año, como referencia sobre la barra "
                   "de la Estrategia — no es una segunda barra, para no duplicar la lectura.")


def analyzable_dates(ds: P.Dataset, accounts: list[str] | None) -> list[str]:
    """Fechas desde el primer día con NAV positivo.

    §9: antes de que la cuenta se fondee, V=0 y el retorno diario es indefinido
    (0/0). El análisis arranca cuando la cartera tiene valor, no en el primer
    día de la extracción.
    """
    nav = P.nav_series(ds, accounts)
    dates = sorted(nav)
    funded = next((d for d in dates if nav[d] > 0), None)
    return [d for d in dates if funded is not None and d >= funded]


def _bench_window_return(bo: dict | None, dates: list[str], a: str, b: str) -> float:
    """Rentabilidad acumulada (%) del benchmark ya descargado (`bo`), recortada
    a la ventana [a,b] sin volver a pedir red (reindexa sobre el precio en caché).

    Devuelve NaN (no None) en cualquier caso sin datos: así la columna sale
    float en el DataFrame incluso cuando el benchmark entero falló, y `style()`
    la formatea como el resto de columnas numéricas ("—"), no como texto "None".
    """
    if not bo:
        return float("nan")
    w = [d for d in dates if a <= d <= b]
    if len(w) < 2:
        return float("nan")
    try:
        rx = B.reindex_to_engine(bo["px"], w)
    except Exception:
        return float("nan")
    return (float((1 + rx.returns).prod()) - 1) * 100


def years_table(ds: P.Dataset, currency: str, rf: float, dates: list[str],
                accounts: list[str] | None = None, bo: dict | None = None) -> pd.DataFrame:
    rows = []
    for y in sorted({d[:4] for d in dates}):
        prior = [d for d in dates if d < y + "0101"]
        year_dates = [d for d in dates if d.startswith(y)]
        a = prior[-1] if prior else dates[0]      # sin cierre previo, arranca en el 1er día fondeado
        if not year_dates or a >= year_dates[-1]:
            continue
        b = year_dates[-1]
        try:
            att = E.attribute(ds, a, b, analysis_currency=currency, accounts=accounts)
        except Exception:
            continue
        m = M.from_series(att.series_strategy, rf=rf)
        partial = not m.annualizable
        rows.append({
            "Año": y + (" YTD" if partial else ""),
            f"Total {currency}": att.total * 100,
            "Estrategia": att.strategy * 100,
            "Divisa": att.fx * 100,
            "Vol": m.vol, "Sharpe": m.sharpe, "Máx. DD": m.max_dd,
            f"{bo['name']}" if bo else "Benchmark": _bench_window_return(bo, dates, a, b),
        })
    return pd.DataFrame(rows)


def trailing_table(ds: P.Dataset, currency: str, rf: float, dates: list[str],
                   accounts: list[str] | None = None, bo: dict | None = None) -> pd.DataFrame:
    bench_col = bo["name"] if bo else "Benchmark"
    rows = []
    for w in M.trailing_windows(dates):
        if not w["available"]:
            rows.append({"Ventana": w["label"], "Acum. total": None,
                         "Acum. estrategia": None, "Anual. estrategia": None,
                         "Vol": None, "Máx. DD": None, bench_col: None,
                         "Nota": f"Sin datos · requiere histórico desde {w['needs']}"})
            continue
        att = E.attribute(ds, w["start"], w["end"], analysis_currency=currency, accounts=accounts)
        m = M.from_series(att.series_strategy, rf=rf)
        rows.append({"Ventana": w["label"], "Acum. total": att.total * 100,
                     "Acum. estrategia": att.strategy * 100,
                     "Anual. estrategia": m.cagr, "Vol": m.vol,
                     "Máx. DD": m.max_dd,
                     bench_col: _bench_window_return(bo, dates, w["start"], w["end"]),
                     "Nota": ""})
    return pd.DataFrame(rows)


@st.cache_data(show_spinner="Descargando benchmark de Yahoo…")
def fetch_bench(ticker: str, start: str, end: str) -> pd.Series:
    return B.fetch_benchmark(ticker, start, end)


def bench_overlay(ticker: str, engine_dates: list[str], rf: float = 0.0) -> dict | None:
    """Precio del benchmark elegido, descargado UNA vez y reindexado al
    calendario del motor: de aquí salen la línea superpuesta en los gráficos,
    las columnas de comparación en las tablas y las tarjetas de riesgo. Si
    Yahoo falla, devuelve None — el panel sigue funcionando sin la comparativa,
    nunca se rompe por un fallo de red de un tercero.

    `rf` con el mismo valor que el usado para la Estrategia (§ Sharpe): si no,
    el Sharpe del benchmark y el de la Estrategia no serían comparables entre
    sí pese a mostrarse uno al lado del otro."""
    try:
        px = fetch_bench(ticker, engine_dates[0], engine_dates[-1])
        rx = B.reindex_to_engine(px, engine_dates)
    except Exception:
        return None
    idx, c = [100.0], 100.0
    for r in rx.returns:
        c *= 1 + r
        idx.append(c)
    metrics = M.compute(rx.returns.tolist(), engine_dates[0], engine_dates[-1], rf=rf)
    return dict(name=B.BENCHMARKS.get(ticker, {}).get("name", ticker),
               dates=engine_dates, index=idx, returns=rx.returns, metrics=metrics,
               reliable_vol=rx.reliable_vol, pct_ff=rx.pct_forward_filled, px=px)


def efecto_divisa_view(ds: P.Dataset, accounts: list[str] | None, currency: str, d0: str, d1: str):
    """§22.2 — pestaña EFECTO DIVISA. Desglose en cascada. Cero cálculo nuevo —
    att.total/att.strategy/att.fx ya salen de attribute() (§7), aquí sólo se
    presentan. Sin cajita de rentabilidad ni interruptor: la propia cascada ya
    enseña Estrategia y Total a la vez (una cajita aparte sería redundante).

    (1+estrategia)(1+fx) = (1+total) por construcción (§7), NO estrategia+fx.
    La cascada suma un cuarto bloque "Cruce" (= total - estrategia - fx, el
    término estrategia×fx) para que las barras cierren exactas contra el
    Total real — nunca una aproximación lineal silenciosa (§20.2 aplica el
    mismo principio de cierre por construcción a otro cálculo)."""
    try:
        att = E.attribute(ds, d0, d1, analysis_currency=currency, accounts=accounts)
    except E.UndefinedReturn as e:
        st.error(f"La serie tiene un tramo no calculable (§9): {e}")
        return

    section_header("globe", "¿Cuánto te ha costado la divisa?")

    cruce = att.total - att.strategy - att.fx   # residuo exacto, no aproximado
    labels = ["Estrategia", "Divisa", "Cruce", "Total"]
    values = [att.strategy * 100, att.fx * 100, cruce * 100, att.total * 100]

    # Rango del eje Y con margen explícito: sin esto, la etiqueta "outside"
    # de la barra más alta (Cruce o Total, según el signo de cada tramo) se
    # corta contra el borde superior del área de trazado — Plotly no le deja
    # sitio solo porque el texto esté "outside", hay que dárselo a mano.
    running = [0.0]
    for v in values[:3]:
        running.append(running[-1] + v)
    top, bottom = max(running), min(running)
    pad = max((top - bottom) * 0.25, 3.0)

    fig = go.Figure(go.Waterfall(
        x=labels, y=values, measure=["relative", "relative", "relative", "total"],
        text=[pct(v) for v in values], textposition="outside",
        textfont=dict(family="Fira Code, monospace", size=12, color=FG),
        connector=dict(line=dict(color=BORDER, width=1)),
        increasing=dict(marker=dict(color=POS)), decreasing=dict(marker=dict(color=NEG)),
        totals=dict(marker=dict(color=SERIES))))
    fig.update_layout(showlegend=False, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Fira Sans, sans-serif", color=FG, size=13),
                      margin=dict(l=6, r=6, t=40, b=6), height=340,
                      xaxis=dict(gridcolor=GRID, linecolor=BORDER),
                      yaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, title="%",
                                range=[bottom - pad * 0.3, top + pad]))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("**Cruce** es el término que falta al sumar Estrategia + Divisa sin más "
              "(estrategia × divisa) — pequeño casi siempre, pero real: sin él, la cascada "
              "no cerraría exacta contra el Total. Estrategia y Divisa no se suman "
              "directamente (§7): se componen, y este bloque es esa diferencia.")

    section_header("globe", "Riesgo de divisa")
    rs = M.decompose_risk(att.series_total.returns, att.series_strategy.returns)
    kpi_cards([
        {"label": "Volatilidad total", "value": pct(rs.vol_total, sign=False), "tone": "neutral", "icon": "activity"},
        {"label": "Volatilidad estrategia", "value": pct(rs.vol_strategy, sign=False), "tone": "neutral", "icon": "activity"},
        {"label": "Volatilidad divisa", "value": pct(rs.vol_fx, sign=False), "tone": "neutral", "icon": "globe"},
        {"label": "Correlación estr./divisa", "value": f"{rs.correlation:+.3f}",
         "tone": _tone(rs.correlation), "icon": "activity"},
    ])
    st.caption(f"Reparto de varianza: estrategia {rs.share_strategy:.1f} %, "
               f"divisa {rs.share_fx:+.1f} %, cruzado {rs.share_cross:+.1f} %. "
               "Una correlación negativa significa que la divisa distorsiona el "
               "resultado mucho más de lo que añade riesgo.")


def benchmark_section(strat_series: E.Series, rf: float, tickers: list[str]):
    """§15 — compara la Estrategia (FX-neutral) contra cada benchmark elegido en
    la barra lateral, en su divisa local (USD), §15.2. La beta va siempre junto
    al alfa (§15.5). Incluye volatilidad y máx. drawdown DEL benchmark en
    solitario (no relativos), para leer el riesgo sin tener que calcularlo."""
    section_header("target", "Comparación con benchmark — detalle")
    if not tickers:
        st.caption("Elige un benchmark en la barra lateral para ver el detalle.")
        return

    dates = strat_series.dates
    r_strat = pd.Series(strat_series.returns, index=dates[1:])
    rows, flags = [], []
    for tk in tickers:
        try:
            px = fetch_bench(tk, dates[0], dates[-1])
            rx = B.reindex_to_engine(px, dates)
            m = B.relative_metrics(r_strat, rx.returns, rf=rf)
            bm = M.compute(rx.returns.tolist(), dates[0], dates[-1])
        except Exception as e:
            st.warning(f"{B.BENCHMARKS.get(tk, {}).get('name', tk)}: {e}")
            continue
        rows.append({
            "Benchmark": B.BENCHMARKS.get(tk, {}).get("name", tk),
            "Acum. estrategia": m["cum_portfolio"], "Acum. bench": m["cum_benchmark"],
            "Vol. bench": bm.vol, "Máx. DD bench": bm.max_dd,
            "Alfa": m["alpha"], "Beta": m["beta"],
            "Captura +": m["up_capture"], "Captura −": m["down_capture"],
            "Correlación": m["correlation"], "Tracking error": m["tracking_error"],
            "Info ratio": m["information_ratio"],
        })
        if not rx.reliable_vol:
            flags.append(f"{tk}: {rx.pct_forward_filled:.1f} % de sesiones arrastradas")
    if not rows:
        return
    # Cabeceras cortas + `help`: con los nombres largos la tabla se iba de
    # ancho y las últimas métricas (captura, tracking error) quedaban fuera.
    st.dataframe(style(pd.DataFrame(rows), ["Acum. estrategia", "Acum. bench", "Alfa"]),
                 width='stretch', hide_index=True,
                 column_config={
                     "Acum. estrategia": st.column_config.Column(
                         label="Acum. estr.", help="Acumulado de la Estrategia (FX-neutral)"),
                     "Acum. bench": st.column_config.Column(
                         label="Acum. bmk", help="Acumulado del benchmark"),
                     "Vol. bench": st.column_config.Column(
                         label="Vol.", help="Volatilidad del benchmark en solitario"),
                     "Máx. DD bench": st.column_config.Column(
                         label="Máx. DD", help="Máximo drawdown del benchmark en solitario"),
                     "Correlación": st.column_config.Column(label="Corr."),
                     "Tracking error": st.column_config.Column(
                         label="TE", help="Tracking error"),
                     "Info ratio": st.column_config.Column(
                         label="IR", help="Information ratio"),
                 })
    st.caption(
        f"Estrategia (FX-neutral) frente al benchmark en su divisa local (USD), §15.2 — "
        f"sistema contra sistema, sin efecto divisa. Volatilidad y máx. drawdown son del "
        f"benchmark en solitario; alfa y ratios con rf = {rf*100:.2f} %; "
        "la beta va al lado del alfa (§15.5).")
    if flags:
        st.caption("Volatilidad/tracking no fiables (calendarios distintos, §15.3): "
                   + " · ".join(flags) + ". Se recomienda intersección de calendarios.")


def style(df: pd.DataFrame, signed: list[str]):
    """Formatea las columnas numéricas; las 'signed' llevan signo explícito (+/−)
    ADEMÁS del color verde/rojo, para no depender sólo del color.

    El fondo/color base (antes de los verde/rojo por columna) se fuerza
    aquí a propósito: st.dataframe pinta las celdas en un <canvas>
    (glide-data-grid) que en Streamlit Cloud no coge el tema oscuro de
    config.toml — pero SÍ respeta `background-color` puesto vía
    pandas.Styler (`background` a secas, no; confirmado contra el propio
    repo de Streamlit). Como las 7 tablas de la app pasan por esta función,
    arreglarlo aquí las arregla todas a la vez, sin depender del motor de
    temas de Streamlit."""
    num = [c for c in df.columns if df[c].dtype.kind == "f"]

    def fmt(col):
        if col in signed:
            return lambda v: "—" if pd.isna(v) else f"{v:+,.2f}"
        return lambda v: "—" if pd.isna(v) else f"{v:,.2f}"

    sty = (df.style.format({c: fmt(c) for c in num})
           .set_properties(**{"background-color": CARD, "color": FG}))
    for c in signed:
        if c in df.columns:
            sty = sty.map(lambda v: "" if pd.isna(v) else
                          f"color:{POS if v >= 0 else NEG}", subset=[c])
    return sty


def operations_view(ds: P.Dataset, accounts: list[str] | None, d0: str, d1: str):
    """§20 — pestaña Operaciones. Divisa local del instrumento, no la divisa
    de análisis de Métricas: son dos preguntas distintas (§20.1)."""
    detail, aggs = T.build(ds, d0, d1, accounts=accounts)
    if not aggs:
        st.info("No hay operaciones en el periodo/cuentas seleccionadas. Esto requiere "
                "haber ampliado el Flex Query con la sección Operaciones (§20.3).")
        return

    section_header("bar-chart", "P&L por ticker")
    st.caption(
        "Importes en la **divisa local del instrumento** (no se convierten ni se les "
        "aplica el desglose de efecto divisa de Métricas). El importe siempre sale de "
        "`fifoPnlRealized` de IBKR cuando entrada y salida caen dentro del periodo; si el "
        "periodo recorta una posición abierta desde antes, se recalcula con el precio de "
        "referencia al inicio del periodo (§20.8) — nunca un `fifoPnlRealized` recortado.")

    agg_df = pd.DataFrame([dict(
        Ticker=a.symbol, Tipo=a.kind.capitalize(), Divisa=a.currency,
        Revalorización=a.revalorizacion_local, Dividendos=a.dividendos_local,
        Comisiones=a.comisiones_local, Total=a.total_local,
        **{"%": a.pct_return * 100 if a.pct_return is not None else None},
    ) for a in aggs])
    st.dataframe(
        style(agg_df, ["Revalorización", "Dividendos", "Comisiones", "Total", "%"]),
        width='stretch', hide_index=True,
        column_config={
            **{c: st.column_config.TextColumn(width="small")
               for c in ("Ticker", "Tipo", "Divisa")},
            # cabecera corta + `help`: "% (pond. capital)" ensanchaba la
            # columna por el título, no por el dato.
            "%": st.column_config.Column(
                help="Revalorización sobre el capital base del periodo, ponderado "
                     "por capital — no la media de los % de cada operación (§20.9)."),
        })
    st.caption(
        "**Tipo** separa acción, opciones y futuros aunque compartan ticker/subyacente "
        "(p. ej. IWM operado directamente vs. opciones sobre IWM): son instrumentos "
        "distintos, nunca se mezclan en el mismo emparejamiento ni en el mismo total.")

    section_header("layers", "Detalle por operación")
    labels = [f"{a.symbol} · {a.kind.capitalize()} ({a.currency}) · "
             f"{pct(a.pct_return * 100) if a.pct_return is not None else 'n/d'}"
             for a in aggs]
    idx = st.selectbox("Ticker", range(len(aggs)), format_func=lambda i: labels[i])
    chosen = aggs[idx]

    ops = chosen.operations
    n_nd = sum(1 for o in ops if o.entry_source == "no_determinado")
    if n_nd:
        st.warning(f"{n_nd} operación(es) de {chosen.symbol} sin Trade ni Transfer que "
                   "expliquen el origen (§20.5): el importe es igual de fiable "
                   "(`fifoPnlRealized`), pero no se puede mostrar precio/fecha de entrada real.")

    # Una columna cuyo valor es el mismo en TODAS las filas no distingue nada:
    # ocupa ancho y empuja fuera de pantalla lo que sí importa (Ganancia, %).
    # Esas se recogen en una nota bajo la tabla y se omiten como columna.
    # "Contrato" sólo difiere del ticker en futuros/opciones (varios
    # vencimientos bajo un subyacente); en acciones repite el ticker ya elegido
    # en el selector. "Origen" sólo interesa cuando no es "trade", y ese caso
    # ya sale avisado arriba.
    def _varies(attr):
        return len({getattr(o, attr) for o in ops}) > 1

    v_contrato, v_origen = _varies("contract_symbol"), _varies("entry_source")
    v_dir, v_estado = _varies("direction"), _varies("status")

    # Orden deliberado: Ganancia y % van ANTES que las columnas opcionales
    # (Contrato/Dir./Estado/Origen). st.dataframe con width='stretch' y ancho
    # insuficiente para el total pedido DESCARTA las columnas que sobran del
    # lado derecho —comprobado en vivo: ni se pintan ni entran en el árbol de
    # accesibilidad, no queda ni scroll—, así que si algo tiene que perderse
    # en un caso con muchas columnas opcionales a la vez, que sea Origen y no
    # Ganancia. Anchos en píxeles explícitos en todas: el auto-tamaño de
    # Streamlit más "small" en solo unas pocas es lo que producía el mismo
    # problema en Cartera (ver _positions_table).
    op_df = pd.DataFrame([dict(
        Cuenta=o.account,
        Entrada=fmt_date(o.entry_date) if o.entry_date else "n/d",
        **{"P. entrada": o.entry_price},
        Salida=fmt_date(o.exit_date),
        **{"P. salida": o.exit_price},
        Cantidad=o.quantity, Ganancia=o.gain_local,
        **{"%": o.pct_return * 100 if o.pct_return is not None else None},
        **{"Com. entrada": o.entry_commission_local, "Com. salida": o.exit_commission_local},
        **({"Contrato": o.contract_symbol} if v_contrato else {}),
        **({"Dir.": o.direction} if v_dir else {}),
        **({"Estado": o.status} if v_estado else {}),
        **({"Origen": o.entry_source} if v_origen else {}),
    ) for o in sorted(ops, key=lambda o: o.entry_date or "")])
    st.dataframe(
        style(op_df, ["Com. entrada", "Com. salida", "Ganancia", "%"]),
        width='stretch', hide_index=True,
        column_config={
            "Cuenta": st.column_config.TextColumn(width=80),
            "Entrada": st.column_config.TextColumn(width=75),
            "P. entrada": st.column_config.NumberColumn(width=80, help="Precio de entrada"),
            "Salida": st.column_config.TextColumn(width=75),
            "P. salida": st.column_config.NumberColumn(width=80, help="Precio de salida"),
            "Cantidad": st.column_config.NumberColumn(width=65),
            "Ganancia": st.column_config.NumberColumn(width=85),
            "%": st.column_config.NumberColumn(width=60),
            "Com. entrada": st.column_config.NumberColumn(width=80, help="Comisión de entrada"),
            "Com. salida": st.column_config.NumberColumn(width=80, help="Comisión de salida"),
            "Contrato": st.column_config.TextColumn(width=70),
            "Dir.": st.column_config.TextColumn(width=55),
            "Estado": st.column_config.TextColumn(width=65),
            "Origen": st.column_config.TextColumn(width=70),
        })

    o0 = ops[0]
    notas = [f"contrato **{o0.contract_symbol}**"] if not v_contrato and o0.contract_symbol else []
    notas += ([f"todas en **{o0.direction}**"] if not v_dir else [])
    notas += ([f"todas **{o0.status}s**"] if not v_estado else [])
    notas += ([f"origen **{o0.entry_source}**"] if not v_origen else [])
    if notas:
        st.caption("Común a todas las operaciones de este ticker (por eso no van "
                  "como columna): " + ", ".join(notas) + ".")


def portfolio_view(ds: P.Dataset, accounts: list[str] | None):
    """§21 — pestaña Cartera. Plusvalía latente de costBasisPrice/
    fifoPnlUnrealized de IBKR (no de nuestro FIFO). Siempre en la divisa
    BASE de la cuenta (ds.base_currency), no en la divisa de análisis de
    Métricas — son preguntas distintas (§20.1 aplica el mismo criterio a
    Operaciones)."""
    currency = ds.base_currency
    snap = T.portfolio(ds, accounts=accounts, analysis_currency=currency)
    if not snap.positions and not snap.cash:
        st.info("No hay posiciones abiertas ni efectivo en las cuentas seleccionadas.")
        return

    d = snap.as_of
    section_header("pie-chart", "Cartera")
    st.caption(f"A cierre del {d[6:]}/{d[4:6]}/{d[:4]} · posiciones y efectivo tal cual "
              "los reporta IBKR, en la divisa base de la cuenta — la plusvalía latente es "
              "su propio cálculo (`costBasisPrice`/`fifoPnlUnrealized`), no una "
              "reconstrucción nuestra.")

    col_kpis, col_pie = st.columns([2, 3])
    with col_kpis:
        # una caja por fila (no en rejilla): kpi_cards usa un grid
        # auto-fit(200px,1fr) — en una columna angosta cada tarjeta ya cae
        # sola en su fila sin CSS adicional.
        kpi_cards([{"label": f"Patrimonio ({currency})",
                    "value": f"{snap.equity_total_analysis_ccy:,.2f}",
                    "tone": "neutral", "icon": "target"}])
        kpi_cards([{"label": f"Exposición total ({currency})",
                    "value": f"{snap.exposure_total_analysis_ccy:,.2f}",
                    "tone": "neutral", "icon": "activity",
                    "hint": "Patrimonio + nocional bruto de futuros/opciones"}])
        kpi_cards([{"label": "Posiciones abiertas", "value": str(len(snap.positions)),
                    "tone": "neutral", "icon": "layers"}])
    with col_pie:
        if snap.pie:
            # Etiquetas FUERA de cada porción con línea propia (textposition
            # "outside" de Plotly), no una leyenda lateral en caja — y máximo
            # 6 porciones (top 5 + "Otros", ya recortado en q4_trades.portfolio).
            fig = go.Figure(go.Pie(
                labels=[s.label for s in snap.pie], values=[s.value_analysis_ccy for s in snap.pie],
                hole=0.55, sort=False, rotation=0,
                marker=dict(colors=PIE_COLORS[:len(snap.pie)], line=dict(color=BG, width=2)),
                textinfo="label+percent", textposition="outside", automargin=True,
                textfont=dict(family="Fira Sans, sans-serif", size=12.5, color=FG),
                outsidetextfont=dict(family="Fira Sans, sans-serif", size=12.5, color=FG),
                hovertemplate="%{label}: %{value:,.2f} " + currency + " (%{percent})<extra></extra>"))
            fig.update_layout(showlegend=False, paper_bgcolor="rgba(0,0,0,0)",
                              margin=dict(l=70, r=70, t=40, b=40), height=420)
            st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "**Patrimonio** = acciones + efectivo, en la divisa base de la cuenta. El efectivo "
        "va combinado en una sola porción del pastel al tipo de cambio vigente (desglosado "
        "por cuenta y divisa más abajo). **Exposición total** añade el nocional bruto de "
        "futuros y opciones —largo o corto, sin compensar entre sí— porque un corto expone "
        "al mercado igual que un largo; no entra en Patrimonio ni en el pastel porque no es "
        "capital inmovilizado (mismo criterio que el NAV, §2/NON_NAV_CATEGORIES). Ambos "
        "siguen apareciendo entre las posiciones, con su plusvalía latente.")

    # Posiciones y efectivo van en DOS tablas, no en una (§21.4). No es sólo
    # estético: el efectivo no tiene precio de entrada ni plusvalía, y esas
    # celdas vacías salían como "None" —comprobado: Streamlit pinta "None"
    # para todo NaN numérico, ignorando el formateo del Styler, `na_rep` y
    # `NumberColumn`; la única forma de no verlo es que la columna no exista—.
    # De paso, dos tablas estrechas caben sin scroll horizontal donde una de
    # 13 columnas no cabía, que es por lo que "Plusvalía" y "%" no se veían.
    if snap.positions:
        section_header("layers", "Posiciones")
        _positions_table(snap)
    if snap.cash:
        section_header("activity", "Efectivo")
        _cash_table(snap)


def _positions_table(snap):
    # Precio de entrada y Plusvalía llevaban semanas calculándose bien (ver
    # PositionRow) pero desaparecían de la tabla: comprobado en vivo que
    # st.dataframe, con width='stretch' y column_config parcial, DESCARTA
    # las últimas columnas cuando el ancho total pedido no cabe en el
    # contenedor — no las deja detrás de scroll, directamente no las pinta
    # ni las mete en el árbol de accesibilidad. A 704 px de contenedor (una
    # ventana normal con la barra lateral abierta) 11 columnas no cabían.
    # Arreglo real: menos columnas + ancho en píxeles explícito en TODAS,
    # no solo en las de texto, para que la suma quede por debajo del
    # contenedor en vez de fiarse de que el auto-tamaño acierte.
    # "Dir." se quita como columna: la cantidad ya lleva el signo (negativa
    # = corto), igual que en cualquier extracto de bróker — no hace falta
    # repetirlo. "Precio actual" se quita porque, de las cuatro, es la que
    # menos aporta ya con Plusvalía y % al lado; con más ancho disponible
    # se puede recuperar.
    pos_df = pd.DataFrame([dict(
        Cuenta=p.account, Ticker=p.symbol, Tipo=p.kind.capitalize(),
        Divisa=p.currency, Cantidad=p.quantity,
        Entrada=fmt_date(p.entry_date), **{"Precio entrada": p.entry_price},
        **{"Plusvalía": p.unrealized_gain_local},
        **{"%": p.pct_return * 100 if p.pct_return is not None else None},
    ) for p in snap.positions]).sort_values(["Tipo", "Ticker"])
    # "Cantidad" NO va en signed: un corto es negativo por convención, no una
    # pérdida — colorearlo en rojo confundiría dirección con resultado.
    st.dataframe(style(pos_df, ["Plusvalía", "%"]), width='stretch', hide_index=True,
                column_config={
                    "Cuenta": st.column_config.TextColumn(width=85),
                    "Ticker": st.column_config.TextColumn(width=60),
                    "Tipo": st.column_config.TextColumn(width=70),
                    "Divisa": st.column_config.TextColumn(width=55),
                    "Cantidad": st.column_config.NumberColumn(width=70),
                    "Entrada": st.column_config.TextColumn(width=80),
                    "Precio entrada": st.column_config.NumberColumn(width=90),
                    "Plusvalía": st.column_config.NumberColumn(width=90),
                    "%": st.column_config.NumberColumn(width=60),
                })
    st.caption("Cantidad negativa = posición corta. Precio actual y dirección explícita "
              "se han quitado de esta tabla para que quepan sin recortarse Precio de "
              "entrada y Plusvalía, que es lo que importa aquí.")


def _cash_table(snap):
    cash_df = pd.DataFrame([dict(Cuenta=c.account, Divisa=c.currency,
                                Saldo=c.balance_local) for c in snap.cash])
    st.dataframe(style(cash_df, []), width='stretch', hide_index=True,
                column_config={c: st.column_config.TextColumn(width="small")
                               for c in ("Cuenta", "Divisa")})


def _fx_sum_by_currency(rows: pd.DataFrame, amount_col: str, ds: P.Dataset,
                        target_currency: str) -> float:
    """Suma una columna en divisa local, convertida fila a fila a
    target_currency — mismo patrón que T.portfolio.to_analysis (§21), aquí
    para movimientos/operaciones en vez de posiciones."""
    if rows.empty:
        return 0.0
    fx = E.FX(P.fx_matrix(ds), ds.base_currency)
    return sum(
        float(a) * fx.rate(c, d) / fx.rate(target_currency, d)
        for a, c, d in zip(rows[amount_col], rows["currency"], rows["date"]))


def informe_view(ds: P.Dataset, accounts: list[str] | None, currency: str, rf: float,
                 d0: str, d1: str, bench_primary: str):
    """§22 — INFORME: Resumen/Rentabilidad/Riesgo/Operaciones del periodo
    seleccionado, en un solo vistazo. No calcula nada nuevo — reordena lo que
    ya calculan Métricas/Operaciones/Cartera para el mismo periodo (§20/§21)."""
    try:
        with st.spinner("Calculando informe…"):
            att = E.attribute(ds, d0, d1, analysis_currency=currency, accounts=accounts)
    except E.UndefinedReturn as e:
        st.error(f"La serie tiene un tramo no calculable (§9): {e}")
        return
    mt = M.from_series(att.series_total, rf=rf)
    snap = T.portfolio(ds, accounts=accounts, analysis_currency=currency)

    section_header("file-text", "Informe")
    st.caption(f"{d0[6:]}/{d0[4:6]}/{d0[:4]} — {d1[6:]}/{d1[4:6]}/{d1[:4]} · "
              f"moneda {currency} · generado a partir de Métricas, Operaciones y Cartera "
              "para este mismo periodo — nada se recalcula aparte.")

    section_header("target", "Resumen")
    kpi_cards([
        {"label": f"Valor de la cartera ({currency})",
         "value": f"{snap.equity_total_analysis_ccy:,.2f}", "tone": "neutral", "icon": "target"},
        {"label": "Rentabilidad del periodo", "value": pct(att.total * 100),
         "tone": _tone(att.total), "icon": "line-chart"},
    ])

    section_header("arrows-exchange", "Rentabilidad")
    # Estrategia/Divisa/Total por cada divisa de referencia, en tabla ancha.
    vals = {}
    for ccy in ("EUR", "USD"):
        try:
            a = E.attribute(ds, d0, d1, analysis_currency=ccy, accounts=accounts)
            vals[ccy] = (a.strategy, a.fx, a.total)
        except E.UndefinedReturn:
            vals[ccy] = (None, None, None)
    rent_df = pd.DataFrame([
        dict(Concepto="Estrategia", **{c: (vals[c][0] * 100 if vals[c][0] is not None else None)
                                       for c in vals}),
        dict(Concepto="Divisa", **{c: (vals[c][1] * 100 if vals[c][1] is not None else None)
                                   for c in vals}),
        dict(Concepto="Total", **{c: (vals[c][2] * 100 if vals[c][2] is not None else None)
                                  for c in vals}),
    ])
    st.dataframe(style(rent_df, list(vals.keys())), width='stretch', hide_index=True)
    st.caption("Estrategia/Divisa/Total vistos desde cada divisa por separado — la Estrategia "
              "no es idéntica en EUR y USD porque el FX se congela contra una divisa de "
              "análisis distinta en cada columna (§7).")

    section_header("activity", "Riesgo")
    top5 = sorted((s for s in snap.pie if s.label not in ("Efectivo", "Otros")),
                 key=lambda s: -s.value_analysis_ccy)[:5]
    conc = (sum(s.value_analysis_ccy for s in top5) / snap.equity_total_analysis_ccy * 100
           if snap.equity_total_analysis_ccy else None)
    beta = correlation = None
    if bench_primary:
        bo = bench_overlay(bench_primary, att.series_total.dates, rf=rf)
        if bo:
            r_tot = pd.Series(att.series_total.returns, index=att.series_total.dates[1:])
            try:
                rel = B.relative_metrics(r_tot, bo["returns"], rf=rf)
                beta, correlation = rel["beta"], rel["correlation"]
            except ValueError:
                pass
    kpi_cards([
        {"label": "Máx. drawdown", "value": pct(mt.max_dd, sign=False), "tone": "neg",
         "icon": "trending-down"},
        {"label": "Volatilidad", "value": pct(mt.vol, sign=False), "tone": "neutral",
         "icon": "activity"},
        {"label": "Sharpe", "value": f"{mt.sharpe:.2f}" if mt.sharpe is not None else "n/d",
         "tone": _tone(mt.sharpe), "icon": "target"},
        {"label": "Sortino", "value": f"{mt.sortino:.2f}" if mt.sortino is not None else "n/d",
         "tone": _tone(mt.sortino), "icon": "target"},
    ])
    kpi_cards([
        {"label": f"Beta vs. {B.BENCHMARKS.get(bench_primary, {}).get('name', bench_primary)}",
         "value": f"{beta:.2f}" if beta is not None else "n/d", "tone": "neutral", "icon": "activity"},
        {"label": "Correlación con el benchmark",
         "value": f"{correlation:+.2f}" if correlation is not None else "n/d",
         "tone": "neutral", "icon": "activity"},
        {"label": "Concentración (top 5 posiciones)",
         "value": pct(conc, sign=False) if conc is not None else "n/d",
         "tone": "neutral", "icon": "layers"},
    ])

    section_header("layers", "Operaciones")
    trades = ds.trades[ds.trades["asset_category"].isin(T.TRADED_CATEGORIES)]
    if accounts is not None:
        trades = trades[trades["account"].isin(accounts)]
    period_trades = trades[(trades["date"] >= d0) & (trades["date"] <= d1)]
    compras = period_trades[period_trades["open_close"].astype(str).str.contains("O")]
    ventas = period_trades[period_trades["open_close"].astype(str).str.contains("C")]
    costes = _fx_sum_by_currency(period_trades.rename(columns={"commission_local": "amount"}),
                                 "amount", ds, currency)

    mov = ds.movements
    if accounts is not None:
        mov = mov[mov["account"].isin(accounts)]
    div_rows = mov[mov["type"].isin(T.DIVIDEND_TYPES) & (mov["date"] >= d0) & (mov["date"] <= d1)]
    dividendos = _fx_sum_by_currency(div_rows.rename(columns={"amount_local": "amount"}),
                                     "amount", ds, currency)

    kpi_cards([
        {"label": "Compras (ejecuciones)", "value": str(len(compras)), "tone": "neutral", "icon": "trending-up"},
        {"label": "Ventas (ejecuciones)", "value": str(len(ventas)), "tone": "neutral", "icon": "trending-down"},
        {"label": f"Dividendos ({currency})", "value": f"{dividendos:+,.2f}", "tone": "pos", "icon": "activity"},
        {"label": f"Comisiones ({currency})", "value": f"{costes:+,.2f}",
         "tone": "neg" if costes < 0 else "neutral", "icon": "activity"},
    ])
    st.caption("Compras/ventas cuentan ejecuciones de `Trade` en el periodo (§20.3), no "
              "operaciones FIFO — una posición que entra en varios lotes cuenta varias veces, "
              "igual que en tu extracto del bróker. Dividendos y comisiones, convertidos a "
              f"{currency} fila a fila con el tipo de cambio de su propia fecha.")


def _guide_step(num: str, icon: str, title: str, note: str):
    st.markdown(
        f'<div class="vf-step"><div class="vf-step-head">'
        f'<span class="vf-step-num">{num}</span>'
        f'<h3>{svg(icon, 18)}{title}</h3></div>'
        f'<p class="vf-step-note">{note}</p></div>',
        unsafe_allow_html=True)


def _guide_callout(tone: str, icon: str, html: str):
    st.markdown(f'<div class="vf-callout vf-callout-{tone}">{svg(icon, 16)}'
                f'<p>{html}</p></div>', unsafe_allow_html=True)


ONBOARDING_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "assets", "onboarding")


def _onboarding_screenshot(label: str, filename: str):
    """Capturas reales del Client Portal como apoyo — opcionales: si el
    fichero no está (aún) en assets/onboarding/, no rompe la guía, solo no
    muestra el expander."""
    path = os.path.join(ONBOARDING_ASSETS_DIR, filename)
    if not os.path.exists(path):
        return
    with st.expander(label):
        st.image(path, use_container_width=True)


# Nombres EXACTOS del Client Portal en español (capturas de una query ya
# validada contra datos reales), con su etiqueta XML entre paréntesis donde
# se sabe con certeza — es la que comprueba q4_ingest.validate_query() en el
# primer "Sincronizar", así que si el Client Portal está en otro idioma o
# IBKR renombra el menú, la app avisa igual señalando la etiqueta que falte
# (§18.5). "Secciones" del creador de queries, columna "Secciones":
_REQUIRED_FLEX_SECTIONS = [
    ("StmtFunds", "Estado de los fondos"),
    ("ChangeInNAV", "Cambio en NAV"),
    ("ChangeInPositionValues", "Resumen de cambios en el valor de la posición"),
    ("CashReport", "Informe de efectivo"),
    ("EquitySummaryInBase", "Valor liquidativo (NAV) en base"),
    ("OpenPositions", "Posiciones abiertas"),
    ("FxPositions", "Saldos en divisas"),
    ("PriorPeriodPositions", "Posiciones del periodo anterior"),
    ("Trades", "Operaciones (con todos los campos marcados)"),
    ("TradeTransfers", "Transferencias de operaciones entrantes y salientes"),
    ("FxTransactions", "Detalles PyG de divisas"),
    ("CashTransactions", "Transacciones en efectivo"),
    ("Transfers", "Transferencias"),
    ("CorporateAction", "Acciones corporativas"),
    ("OptionEAE", "Ejercicios, asignaciones y vencimiento de opciones"),
    ("SecurityInfo", "Información de instrumento financiero"),
]

# "Resumen de rendimiento ajustado al mercado en base" — a petición
# explícita, también obligatoria en la guía. Sin tag: a diferencia de las de
# arriba, no encontré su etiqueta XML confirmada en la spec (§18.7/§20.3) ni
# en q4_ingest.REQUIRED_SECTIONS, así que NO se añade a validate_query() —
# forzar ahí una etiqueta adivinada haría fallar el primer "Sincronizar" de
# todo el mundo si se equivoca. Aparece en el checklist como obligatoria
# igualmente; falta confirmar el tag real (mirando un XML de una query que
# la tenga activada) antes de que la app la compruebe de verdad.
_REQUIRED_FLEX_SECTIONS_NO_TAG = [
    "Resumen de rendimiento ajustado al mercado en base",
]

# Pestaña "Configuración general" del mismo creador de queries. (campo,
# valor, ¿merece resaltarse en ámbar?) — los tres resaltados son los que
# rompen algo si se dejan en su valor por defecto: sin "Separar por día" en
# Sí la query devuelve un único snapshot, no posiciones diarias; con
# "Mostrar alias" en Sí, VeriFine ya no reconoce las cuentas por su ID.
_QUERY_GENERAL_CONFIG = [
    ("Modelos", "Opcional", False),
    ("Formato", "XML", True),
    ("Período", "Últimos 365 días naturales", False),
    ("Formato fecha", "yyyyMMdd", False),
    ("Formato de tiempo", "HHmmss", False),
    ("Separador fecha/hora", "; (punto y coma)", False),
    ("Pérdidas y ganancias", "Predeterminado", False),
    ("¿Incluir pares de compensación/cancelación?", "No", False),
    ("¿Incluir tipos de cambio?", "Sí", True),
    ("¿Incluir campos de pista de auditoría?", "No", False),
    ("¿Mostrar alias en vez del ID de cuenta?", "No", True),
    ("¿Separar por día?", "Sí", True),
]


def configuracion_view():
    """Guía de alta: crear la Flex Query en IBKR, generar el token, y
    conectar. Es la primera pantalla que ve un cliente sin datos todavía
    (`ds is None` en `main()`), y también vive como pestaña "Configuración"
    para poder volver a consultarla más adelante."""
    st.markdown('<div class="vf-guide-eyebrow">Primera vez con VeriFine</div>',
               unsafe_allow_html=True)
    # st.markdown() en esta versión de Streamlit (1.50.0, ver requirements.txt)
    # no admite key= — el gancho para el CSS de justificado (§THEME_CSS,
    # .st-key-guide-intro) va en el st.container que lo envuelve.
    with st.container(key="guide-intro"):
        st.markdown("Antes de sincronizar hacen falta dos cosas: tu **licencia** y una "
                   "**Flex Query** de Interactive Brokers con su **token** de solo lectura. "
                   "La query se configura una vez por cuenta — la app la reutiliza tanto "
                   "para el histórico completo como para cada sincronización posterior.")

    _guide_step("01", "target", "Introduce tu licencia",
                "El código llega en el <strong>último correo de pago</strong> de tu "
                "suscripción de Substack. Pégalo en el campo <strong>Licencia</strong>, "
                "arriba del todo en el lateral, y pulsa «Verificar licencia». Sin él "
                "puedes seguir probando: sincronizas hasta 6 meses de histórico y ves "
                "Métricas y Cartera, pero Efecto divisa, Operaciones e Informe quedan "
                "bloqueadas hasta verificarlo.")

    _guide_step("02", "layers", "Crea la Flex Query en el Client Portal",
                "Inicia sesión en el <strong>Client Portal</strong> de IBKR → engranaje de "
                "Configuración → Configuración de informes → <strong>Consultas Flex "
                "(Flex Queries)</strong> → crear una nueva <strong>Activity Flex Query</strong>. "
                "Si gestionas varias cuentas, <strong>selecciona todas</strong> las que "
                "quieras auditar: VeriFine deja elegir después si ver el conjunto o "
                "cada cuenta por separado, pero solo con las que estén en la query.")
    st.markdown("En la pestaña **Secciones**, activa estas (con todos sus campos "
               "marcados donde lo pregunte). Los nombres son los literales del Client "
               "Portal en español — si lo tienes en otro idioma, no pasa nada: VeriFine "
               "comprueba el contenido descargado, no el nombre del menú.")
    st.markdown(
        '<div class="vf-checklist">' +
        "".join(
            f'<div class="vf-check-item">{svg("check", 15)}'
            f'<span>{label}<span class="tag">{tag}</span></span></div>'
            for tag, label in _REQUIRED_FLEX_SECTIONS
        ) +
        "".join(
            f'<div class="vf-check-item">{svg("check", 15)}<span>{label}</span></div>'
            for label in _REQUIRED_FLEX_SECTIONS_NO_TAG
        ) + '</div>',
        unsafe_allow_html=True)
    _guide_callout("info", "check",
                  "No hace falta acertar a la primera: en el primer «Sincronizar», "
                  "VeriFine comprueba las etiquetas del XML recibido y, si falta alguna "
                  "sección, te dice exactamente cuál — sin depender de en qué idioma "
                  "tengas el Client Portal. Única excepción: «Resumen de rendimiento "
                  "ajustado al mercado en base» todavía no se comprueba sola — pendiente "
                  "de confirmar su etiqueta XML exacta.")
    _onboarding_screenshot("Ver la pantalla de Secciones", "01-secciones.png")

    _guide_step("03", "file-text", "Formato y configuración general",
                "Todavía dentro de la misma query, en <strong>Formato</strong> y "
                "<strong>Configuración general</strong>. Dos de estos valores no son "
                "cosméticos: sin «Separar por día» en Sí la query devuelve un único "
                "cierre en vez de posiciones diarias, y con «Mostrar alias» en Sí "
                "VeriFine deja de reconocer tus cuentas.")
    st.markdown(
        '<div class="vf-kv-grid">' +
        "".join(
            f'<div class="vf-kv-item"><span class="k">{k}</span>'
            f'<span class="v{" attn" if attn else ""}">{v}</span></div>'
            for k, v, attn in _QUERY_GENERAL_CONFIG
        ) + '</div>',
        unsafe_allow_html=True)
    st.markdown('<p class="vf-check-extra">El período (365 días) no importa para el '
               "resultado — VeriFine pide siempre el rango exacto que necesita en cada "
               "descarga, esto solo fija un valor de partida válido.</p>",
               unsafe_allow_html=True)
    _onboarding_screenshot("Ver la pantalla de Formato y configuración general",
                           "02-formato-general.png")

    _guide_step("04", "arrows-exchange", "Genera el token del Flex Web Service",
                "En el mismo Client Portal: engranaje de Configuración → Configuración "
                "de informes → <strong>Servicio Flex Web (Flex Web Service)</strong> → "
                "Configurar → generar token. Da acceso de <strong>solo lectura</strong> "
                "a tus extractos — nunca a operar ni mover dinero.")
    _guide_callout("warn", "alert-circle",
                  "El token caduca (recomendado renovarlo cada 90 días). Si deja de "
                  "funcionar, «Sincronizar» lo dirá con un aviso claro de token caducado "
                  "— basta con generar uno nuevo aquí y pegarlo de nuevo abajo, sin "
                  "tocar la Flex Query.")

    _guide_step("05", "target", "Pega el token y el Query ID aquí",
                "Copia el token y el <strong>Query ID</strong> (aparece junto al nombre "
                "de tu query en el listado de Consultas Flex) en los campos de la "
                "izquierda y pulsa <strong>Sincronizar</strong>. VeriFine descarga el "
                "histórico completo la primera vez, y solo lo nuevo en las siguientes.")


# --------------------------------------------------------------------------
# Aplicación
# --------------------------------------------------------------------------

def main():
    # Invisible: detecta claro/oscuro (Settings -> "Choose app theme" de
    # Streamlit) y aplica la paleta correspondiente ANTES de construir nada
    # — tablas y gráficos deciden su color en Python, no lo puede arreglar
    # el CSS después (ver _apply_theme_palette()). En el primerísimo
    # render de la sesión el componente todavía no ha tenido ocasión de
    # avisar (necesita un viaje de ida y vuelta) y se cae en "dark" — el
    # mismo rerun automático que trae su primer valor ya corrige esto
    # solo, igual que el resto de componentes de esta app.
    _theme_result = _theme_watcher(key="theme_watcher")
    _detected_theme = (_theme_result or {}).get("theme", "dark")
    _apply_theme_palette(_detected_theme)

    # Título de página arriba del todo — SIEMPRE visible desde el primer
    # instante, antes de conectar o parsear nada: sin esto el cuerpo
    # principal queda en blanco durante la sincronización con IBKR (spinners
    # de parseo/cálculo incluidos) y da la sensación de que la app se ha
    # quedado colgada. Tamaño doble del original (30px -> 60px; el icono y
    # la coletilla, en em, escalan con él).
    st.markdown(
        f'<div class="vf-headline">{svg("target", 60)}'
        f'<h1 class="vf-title" style="font-size:60px">VeriFine '
        f'<span class="vf-tagline">| Audita tu cuenta de IBKR</span></h1></div>',
        unsafe_allow_html=True)
    st.sidebar.markdown(f'<div class="vf-brand">{svg("target", 22)}VeriFine</div>',
                        unsafe_allow_html=True)

    # Licencia/Token/Query ID/carpeta van en un bloque plegable — a
    # petición expresa, para no ocupar sitio en el sidebar en cada visita
    # una vez ya hay datos cargados. Plegado por defecto solo cuando YA hay
    # extractos en el almacén (comprobación barata por fichero, antes de
    # tocar nada de sesión); la primera vez, o tras "Borrar todo", se abre
    # solo porque hace falta rellenarlo.
    has_data = bool(glob.glob(os.path.join(RAW_DIR, "*.xml")))
    with st.sidebar.expander("Conexión y licencia", expanded=not has_data):
        license_mode = license_gate()
        token, qid = _connection_fields()
        start = _history_start_field(qid, license_mode)
        _danger_zone()

    ds = sidebar_source(license_mode, token, qid, start)
    if ds is None:
        configuracion_view()
        st.stop()

    # Quitados del sidebar a petición expresa: antes selectbox/number_input.
    # La moneda ya no es elegible — se fija sola a la moneda base de la
    # cuenta (ds.base_currency, de EquitySummaryInBase/StmtFunds, §1). 0 %
    # es el valor por defecto que ya traía el widget de tasa libre de
    # riesgo, así que ese cálculo no cambia para nadie.
    currency = ds.base_currency
    rf = 0.0
    accounts = st.sidebar.multiselect("Cuentas", ds.accounts, default=ds.accounts)

    # Benchmark: se elige AQUÍ, al principio, y de ahí en adelante aparece
    # como comparativa en Evolución, Riesgo, la tabla por año y las ventanas
    # móviles — un único selector gobierna todos los módulos.
    st.sidebar.markdown(
        f'<div class="vf-sec" style="margin:20px 0 2px;">{svg("target", 16)}'
        f'<h3 style="font-size:13px;">Benchmark</h3></div>', unsafe_allow_html=True)
    bench_keys = list(B.BENCHMARKS)
    default_idx = bench_keys.index("^SP500TR") if "^SP500TR" in bench_keys else 0
    bench_primary = st.sidebar.selectbox(
        "Comparar la estrategia contra", bench_keys, index=default_idx,
        format_func=lambda t: B.BENCHMARKS[t]["name"],
        help="Se superpone en los gráficos y en las tarjetas de riesgo de todo el panel.")
    bench_extra = st.sidebar.multiselect(
        "Añadir más al detalle (§15.5: uno amplio + uno de estilo)",
        [t for t in bench_keys if t != bench_primary],
        format_func=lambda t: B.BENCHMARKS[t]["name"])
    bench_tickers = [bench_primary] + bench_extra

    # Contenedor con key para poder dirigirles CSS específico (letra a la
    # mitad, ver .st-key-top-alerts en THEME_CSS) sin afectar a las alertas
    # de más abajo en la página (errores de sync, etc.).
    with st.container(key="top-alerts"):
        if ds.nav_conflicts:
            st.warning(f"{len(ds.nav_conflicts)} conflictos de NAV entre bloques solapados. "
                       "IBKR ha reexpresado datos históricos.")

        # Auto-chequeo de integridad sobre los datos del usuario (§9/T4/T5/T6b): caza
        # formas de cartera que el motor aún no valida antes de mostrar números mal.
        # Se calcula una vez por dataset (no depende del selector), se cachea en sesión.
        if st.session_state.get("_sc_id") != id(ds):
            with st.spinner("Verificando integridad de los datos…"):
                st.session_state["_sc"] = SC.run_checks(ds)
            st.session_state["_sc_id"] = id(ds)
        for issue in st.session_state["_sc"]:
            (st.error if issue["level"] == "error" else st.warning)(issue["msg"])
        if not st.session_state["_sc"]:
            st.caption("Comprobaciones de integridad superadas: el NAV reconstruye por "
                       "cuenta, la atribución cierra y es invariante a la moneda.")

        funded = analyzable_dates(ds, accounts or None)
        if len(funded) < 2:
            st.warning("No hay histórico con NAV positivo suficiente para analizar.")
            st.stop()

        if funded[0] > ds.dates[0]:
            st.info(f"Los datos arrancan con NAV positivo el {funded[0][6:]}/{funded[0][4:6]}/"
                    f"{funded[0][:4]}; los días previos al fondeo no son calculables (§9).")

    # Selector de periodo: slider + dos date_input sincronizados en ambos
    # sentidos (mover el slider actualiza las fechas y viceversa). El truco es
    # una única fuente de verdad en session_state y callbacks que escriben las
    # claves de los OTROS widgets antes del siguiente rerun; los widgets se
    # gobiernan por su clave (sin `value=`) para no chocar con session_state.
    lo_d = dt.datetime.strptime(funded[0], "%Y%m%d").date()
    hi_d = dt.datetime.strptime(funded[-1], "%Y%m%d").date()
    if lo_d < hi_d:
        if "sld" not in st.session_state or st.session_state.get("sld_bounds") != (lo_d, hi_d):
            st.session_state.sld = (lo_d, hi_d)
            st.session_state.din_from, st.session_state.din_to = lo_d, hi_d
            st.session_state.sld_bounds = (lo_d, hi_d)

        def _from_slider():
            a, b = st.session_state.sld
            st.session_state.din_from, st.session_state.din_to = a, b

        def _from_inputs():
            a, b = st.session_state.din_from, st.session_state.din_to
            if a > b:
                a, b = b, a
            st.session_state.sld = (a, b)
            st.session_state.din_from, st.session_state.din_to = a, b

        st.sidebar.slider("Periodo de análisis", min_value=lo_d, max_value=hi_d,
                          key="sld", on_change=_from_slider, format="DD/MM/YYYY")
        cA, cB = st.sidebar.columns(2)
        cA.date_input("Desde", min_value=lo_d, max_value=hi_d, key="din_from",
                      on_change=_from_inputs, format="DD/MM/YYYY")
        cB.date_input("Hasta", min_value=lo_d, max_value=hi_d, key="din_to",
                      on_change=_from_inputs, format="DD/MM/YYYY")
        lo_sel, hi_sel = st.session_state.sld
        lo_s, hi_s = lo_sel.strftime("%Y%m%d"), hi_sel.strftime("%Y%m%d")
        dates = [d for d in funded if lo_s <= d <= hi_s]
    else:
        dates = funded
    if len(dates) < 2:
        st.warning("Periodo demasiado corto: elige un rango con al menos dos sesiones.")
        st.stop()
    d0, d1 = dates[0], dates[-1]

    tab_metricas, tab_cartera, tab_divisa, tab_operaciones, tab_informe, tab_config = st.tabs(
        ["MÉTRICAS", "CARTERA", "EFECTO DIVISA", "OPERACIONES", "INFORME", "CONFIGURACIÓN"])

    with tab_cartera:
        portfolio_view(ds, accounts or None)

    with tab_divisa:
        if _tab_gate(license_mode):
            efecto_divisa_view(ds, accounts or None, currency, d0, d1)

    with tab_operaciones:
        if _tab_gate(license_mode):
            operations_view(ds, accounts or None, d0, d1)

    with tab_informe:
        if _tab_gate(license_mode):
            informe_view(ds, accounts or None, currency, rf, d0, d1, bench_primary)

    with tab_metricas:
        _metricas_tab(ds, currency, rf, accounts, dates, d0, d1, bench_primary, bench_tickers)

    with tab_config:
        configuracion_view()


def _metricas_tab(ds, currency, rf, accounts, dates, d0, d1, bench_primary, bench_tickers):
    try:
        with st.spinner("Calculando TWR y atribución…"):
            att = E.attribute(ds, d0, d1, analysis_currency=currency, accounts=accounts or None)
    except E.UndefinedReturn as e:
        st.error(f"La serie tiene un tramo no calculable (§9): {e}")
        st.stop()
    mt = M.from_series(att.series_total, rf=rf)
    ms = M.from_series(att.series_strategy, rf=rf)

    with st.spinner(f"Descargando {B.BENCHMARKS[bench_primary]['name']}…"):
        bo = bench_overlay(bench_primary, att.series_strategy.dates, rf=rf)
    bench_col = bo["name"] if bo else "Benchmark"

    # El título ya se pintó arriba del todo de la página (ver main()); aquí
    # sólo queda la línea de subtítulo, que sí depende de los datos cargados.
    st.markdown(
        f'<div class="vf-sub">{d0[6:]}/{d0[4:6]}/{d0[:4]} — {d1[6:]}/{d1[4:6]}/{d1[:4]} · '
        f'{len(att.series_total.returns)} sesiones · moneda {currency} · datos a cierre '
        f'de mercado</div>', unsafe_allow_html=True)

    cagr_t = pct(mt.cagr) if mt.cagr is not None else "n/d"
    cagr_s = pct(ms.cagr) if ms.cagr is not None else "n/d"
    sharpe_t = f"{mt.sharpe:.2f}" if mt.sharpe is not None else "n/d"
    kpi_cards([  # fila 1 — TOTAL (con efecto divisa). Sin benchmark: el benchmark
                # sólo se compara contra la Estrategia FX-neutral, nunca contra el
                # Total (§15.2) — mezclarlo aquí confundiría rendimiento con divisa.
        {"label": f"Acumulado total ({currency})", "value": pct(att.total * 100),
         "tone": _tone(att.total), "icon": "line-chart"},
        {"label": "Anualizado total", "value": cagr_t, "tone": _tone(mt.cagr),
         "icon": "activity"},
        {"label": "Sharpe total", "value": sharpe_t, "tone": _tone(mt.sharpe),
         "icon": "target"},
        {"label": "Volatilidad total", "value": pct(mt.vol, sign=False),
         "tone": "neutral", "icon": "activity"},
        {"label": "Máx. drawdown total", "value": pct(mt.max_dd, sign=False),
         "tone": "neg", "icon": "trending-down"},
    ])

    # fila 2 — ESTRATEGIA (FX-neutral), con el benchmark como referencia en
    # cada tarjeta: mismo dato, mismo orden, para comparar sin bajar de página,
    # más un tick/cruz según si la Estrategia bate o no al benchmark ahí.
    def _better(strat_val, bench_val, lower_is_better=False):
        """True si la Estrategia bate al benchmark en esta métrica, False si no,
        None si falta un dato (no se pinta ni tick ni cruz)."""
        if strat_val is None or bench_val is None:
            return None
        return strat_val < bench_val if lower_is_better else strat_val > bench_val

    if bo:
        bench_cum, bench_cagr = bo["index"][-1] - 100.0, bo["metrics"].cagr
        bench_vol, bench_dd = bo["metrics"].vol, bo["metrics"].max_dd
        bench_sharpe = bo["metrics"].sharpe
        bench_cum_s, bench_vol_s = pct(bench_cum), pct(bench_vol, sign=False)
        bench_cagr_s = pct(bench_cagr) if bench_cagr is not None else "n/d"
        bench_dd_s = pct(bench_dd, sign=False)
        bench_sharpe_s = f"{bench_sharpe:.2f}" if bench_sharpe is not None else "n/d"
    else:
        bench_cum = bench_cagr = bench_vol = bench_dd = bench_sharpe = None
        bench_cum_s = bench_cagr_s = bench_vol_s = bench_dd_s = bench_sharpe_s = None

    def _bench(value: str | None, better: bool | None) -> dict | None:
        return {"ticker": bench_primary, "value": value, "better": better} if value is not None else None

    sharpe_s = f"{ms.sharpe:.2f}" if ms.sharpe is not None else "n/d"
    kpi_cards([
        {"label": "Acumulado estrategia", "value": pct(att.strategy * 100),
         "tone": _tone(att.strategy), "icon": "trending-up",
         "bench": _bench(bench_cum_s, _better(att.strategy * 100, bench_cum))},
        {"label": "Anualizado estrategia", "value": cagr_s, "tone": _tone(ms.cagr),
         "icon": "activity", "bench": _bench(bench_cagr_s, _better(ms.cagr, bench_cagr))},
        {"label": "Sharpe estrategia", "value": sharpe_s, "tone": _tone(ms.sharpe),
         "icon": "target", "bench": _bench(bench_sharpe_s, _better(ms.sharpe, bench_sharpe))},
        {"label": "Volatilidad estrategia", "value": pct(ms.vol, sign=False),
         "tone": "neutral", "icon": "activity",
         "bench": _bench(bench_vol_s, _better(ms.vol, bench_vol, lower_is_better=True))},
        {"label": "Máx. drawdown estrategia", "value": pct(ms.max_dd, sign=False),
         "tone": "neg", "icon": "trending-down", "bench": _bench(bench_dd_s, _better(ms.max_dd, bench_dd))},
    ])
    st.caption(
        "**Total** = lo que rindió tu dinero, **incluido el efecto divisa**. "
        "**Estrategia** = el rendimiento puro del sistema con los tipos de cambio "
        "congelados, **sin efecto divisa**. La diferencia entre ambos es lo que "
        "aportó (o restó) la moneda. La referencia ámbar bajo cada tarjeta de "
        f"Estrategia es {B.BENCHMARKS[bench_primary]['name'] if bo else 'el benchmark'} "
        "en el mismo periodo.")

    section_header("line-chart", "Evolución")
    view = st.segmented_control("Gráfico", ["Curva de capital", "Drawdown"],
                                default="Curva de capital",
                                label_visibility="collapsed") or "Curva de capital"
    if view == "Curva de capital":
        equity_chart(att.series_total, att.series_strategy, currency, bench=bo)
    else:
        drawdown_chart(att.series_total, att.series_strategy, currency, bench=bo)
    if bo is None:
        st.caption(f"No se pudo descargar {B.BENCHMARKS[bench_primary]['name']} de Yahoo "
                   "Finance; el panel sigue sin la comparativa de benchmark.")

    section_header("bar-chart", "Rentabilidad de la estrategia por año")
    yt = years_table(ds, currency, rf, dates, accounts or None, bo=bo)
    yearly_bar_chart(yt, bench_col=bench_col)     # sólo años, sin el total

    section_header("layers", "Ventanas móviles")
    tt = trailing_table(ds, currency, rf, dates, accounts or None, bo=bo)
    st.dataframe(style(tt, ["Acum. total", "Acum. estrategia", "Anual. estrategia", bench_col]),
                 width='stretch', hide_index=True,
                 # el nombre del benchmark ("S&P 500 Total Return") como cabecera
                 # ensanchaba la columna por el título y empujaba "Nota" fuera.
                 column_config={bench_col: st.column_config.Column(
                     label="Benchmark", help=bench_col)})

    benchmark_section(att.series_strategy, rf, bench_tickers)


# Streamlit ejecuta el script con __name__ == "__main__", así que este guard
# no estorba y a la vez permite importar el módulo desde los tests.
if __name__ == "__main__":
    main()
