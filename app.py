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
import re
import json
import time
import glob
import shutil
import uuid
import base64
import logging
import threading
import datetime as dt

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go
from streamlit.runtime.scriptrunner import add_script_run_ctx

import q4_parser as P
import q4_engine as E
import q4_metrics as M
import q4_benchmark as B
import q4_license as L
import q4_selfcheck as SC
import q4_trades as T
import q4_drive as D
import q4_storage as ST
import q4_probe as PR

log = logging.getLogger("q4.app")

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

# Ruta LOCAL de trabajo de ESTA sesión — un directorio temporal, ver
# _drive_gate()/q4_storage.init_session_storage(). El valor de aquí es solo
# el que rige antes de que _drive_gate() la resuelva (arranque en frío de
# main()) o en el escape de desarrollo Q4_STORAGE_BACKEND=local (ver más
# abajo) — en producción, la copia persistente de verdad es la carpeta
# "VeriFine" en el Drive del usuario, nunca este directorio.
RAW_DIR = os.environ.get("Q4_RAW_DIR",
                          os.path.join(os.path.expanduser("~"), "VeriFine", "raw"))
LICENSE_PATH = os.path.join(RAW_DIR, "license.json")

# Mismo formato que ya usa q4_daily.py ({"token":..,"query_id":..}). Vive en
# RAW_DIR (el scratch dir de la sesión, espejado a Drive por
# _save_ibkr_creds/_clear_ibkr_creds más abajo) — nunca en el navegador ni
# en ningún disco que no sea el Drive del propio usuario.
IBKR_CREDS_PATH = os.path.join(RAW_DIR, "ibkr_credentials.json")


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
    ST.sync_up(st.session_state.get("_drive_folder"), RAW_DIR, "ibkr_credentials.json")


def _clear_ibkr_creds() -> None:
    if os.path.exists(IBKR_CREDS_PATH):
        os.remove(IBKR_CREDS_PATH)
    ST.sync_delete(st.session_state.get("_drive_folder"), "ibkr_credentials.json")


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

# Alto de la cabecera fija de Streamlit — de fábrica son 52px; se agranda
# para que quepa el título grande (icono+"VeriFine"+"by Quant4all"+
# coletilla, ver _inject_header_title()) casi a su tamaño original del
# cuerpo (90%) en vez de encogerlo a la fuerza para caber en 52px (a
# petición expresa). Usado tanto en THEME_CSS (la propia cabecera y el
# padding-top que compensa el contenido de abajo) como en
# _inject_header_title() (para centrar verticalmente lo que inyecta).
_HEADER_H = 68

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
  font-weight:700; font-size:20px; letter-spacing:.02em; color:var(--color-positive);
  padding:2px 0 10px; }}
.vf-brand .vf-ico {{ color:var(--fg); }}
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
/* El CTA de Substack (paso "Introduce tu licencia" de la guía) NO usa
   estas clases — va por components.html() en su propio <iframe>, con su
   CSS inline (ver _clickable_logo) porque var(--...) no cruza al iframe y
   un <style> con @keyframes dentro de st.markdown(unsafe_allow_html=True)
   salía truncado. Nada que definir aquí. */
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
/* Cabecera agrandada (52px de fábrica -> {_HEADER_H}px) a petición
   expresa, para que quepa el título grande (icono+"VeriFine"+"by
   Quant4all"+coletilla, ver _inject_header_title()) casi a su tamaño
   original del cuerpo (90%), no encogido a la fuerza para caber en 52px.
   El contenido de la página asume el alto de fábrica para su
   padding-top — se compensa la diferencia más abajo (stMain/block-container)
   para que la cabecera más alta no tape la primera línea. */
[data-testid="stHeader"] {{ height:{_HEADER_H}px !important; }}
[data-testid="stToolbar"] {{ padding-top:{(_HEADER_H - 52) // 2}px !important; }}
[data-testid="stAppViewContainer"] > .main {{
  padding-top:{_HEADER_H - 52}px !important;
}}
/* El título de la cabecera fija ("VeriFine by Quant4all | Audita tu
   cuenta de IBKR", con el icono) YA NO va aquí como ::before de una sola
   cadena — con tres tamaños/colores de texto y un <svg>, un único
   `content` de texto plano no puede darlos (::before/::after son sólo DOS
   huecos, y tampoco aceptan HTML). Ver _inject_header_title(), que lo
   mete como HTML real desde un <iframe> que alcanza el documento padre. */

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
/* Slider: solo las etiquetas de los extremos (legibilidad, nada que ver
   con el color de acento). El pomo, el número que lleva encima y la
   barra de relleno se dejan TAL CUAL los pinta Streamlit — rojo nativo,
   a petición expresa de Juan — así que aquí ya no se fuerza ni el pomo ni
   stSliderThumbValue a nuestro verde como se hacía antes. La barra de
   relleno en sí NUNCA se ha tocado: BaseWeb la pinta con un
   linear-gradient calculado al vuelo que codifica la posición del rango
   elegido — forzarle un color fijo la dejaría plana. */
[data-testid="stSliderTickBar"], [data-testid="stSliderTickBar"] span {{
  color: var(--muted) !important;
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
/* Los días fuera de rango (p. ej. el selector "Periodo de análisis": con
   licencia gratuita lleva min_value porque el periodo ANALIZABLE sí está
   capado a 12 meses, aunque la descarga no lo esté — ver FREE_MONTHS) los
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
    "cloud": '<path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>',
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

def _fetch_valid_codes_cached() -> list[str] | None:
    """Una vez por SESIÓN, no en cada rerun de Streamlit (un clic en
    cualquier widget vuelve a ejecutar main()) — pero sí una vez por
    sesión/carga nueva de la app, a petición expresa (Juan cambió el
    código en el Gist y la app lo seguía dando por válido): antes esto se
    cacheaba con @st.cache_data(ttl=3600), que es un caché de TODO EL
    PROCESO del servidor compartido entre TODAS las sesiones/usuarios —
    cambiar el Gist podía tardar hasta una hora en notarse, y encima
    afectaba a cualquiera que entrara mientras tanto, no solo a quien
    tenía la pestaña abierta. session_state es por sesión: cada carga
    nueva de la app (pestaña nueva, recarga) vuelve a comprobar de verdad.

    Solo se recuerda un fetch que SÍ ha tenido éxito (lista real, no
    None) — si falla (red, timeout), se reintenta en el siguiente rerun en
    vez de quedarse atascado en modo de gracia el resto de la sesión."""
    cached = st.session_state.get("_license_valid_codes")
    if cached is not None:
        return cached
    codes = L.fetch_valid_codes()
    if codes is not None:
        st.session_state["_license_valid_codes"] = codes
    return codes


FREE_MONTHS = 12  # histórico máximo ANALIZABLE con licencia GRATIS (código
                  # VF-FREE del email de bienvenida de Substack) — la
                  # descarga/sincronización no tiene tope (§_history_start_field).
                  # Sin ningún código, no hay tramo: el análisis queda
                  # bloqueado del todo (ver license_gate/_drive_gate).

SUBSTACK_URL = "https://quant4all.substack.com/"  # ver configuracion_view(), paso 02


def license_gate() -> str:
    """Devuelve "full" (licencia de pago: sin límites), "free" (código
    gratis del email de bienvenida de Substack: 12 meses, periodo/cuentas
    fijos — ver el bloqueo de interactividad en main()) o "blocked" (sin
    código, o código inválido: ningún panel de análisis se muestra, ver
    _drive_gate/main — ese mensaje de invitación a suscribirse vive en
    configuracion_view(), no aquí).

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
        help="El de pago está en el último correo de pago de tu suscripción de Substack; "
            "el gratis, en el email de bienvenida al suscribirte (ver pestaña Configuración).")
    if typed != saved.code:
        st.session_state.pop("_license_valid_codes", None)  # código nuevo: comprobarlo de verdad ya
    st.button("Verificar licencia")  # el propio cambio de campo ya dispara el rerun

    attempt = L.LicenseState(code=typed, last_ok=saved.last_ok, last_tier=saved.last_tier)
    ok, tier, level, msg = L.evaluate(attempt, _fetch_valid_codes_cached())

    if ok and level != "warning":
        # Validado de verdad contra el manifiesto (o licencia desactivada,
        # §evaluate): éste pasa a ser el último código válido guardado.
        attempt.save(LICENSE_PATH)
        ST.sync_up(st.session_state.get("_drive_folder"), RAW_DIR, "license.json")
        if tier == "free":
            st.caption("Licencia gratuita activa ✓")
            st.info(f"Con la licencia gratuita, el análisis se limita a los últimos "
                    f"{FREE_MONTHS} meses, y el periodo/las cuentas quedan fijos (no se "
                    "pueden cambiar). La licencia completa quita ambos límites.")
        else:
            st.caption("Licencia completa activa ✓")
        return tier

    if ok:  # level == "warning": modo de gracia offline, por el código YA
        # guardado — no se ha comprobado lo tecleado, así que no se toca
        # el fichero (evaluate() tampoco cambia last_ok en este caso).
        st.warning(msg)
        return tier

    # No autoriza: NO se toca el último código válido guardado — un
    # intento fallido (typo, código de otro mes, fallo de red al verificar)
    # no debe borrar el bueno. Se muestra el motivo REAL (`msg`, de
    # evaluate()) — antes se descartaba y siempre salía el mismo texto
    # genérico, así que un fallo al pedir el Gist (red, URL mal puesta en
    # Secrets, lo que sea) era indistinguible de un código realmente
    # erróneo, sin ninguna pista para depurarlo.
    st.error(msg)
    return "blocked"


def _tab_gate(license_mode: str) -> bool:
    """Efecto divisa/Operaciones/Informe son de pago (a petición expresa):
    sin al menos licencia gratuita no muestran nada, solo el aviso.
    Métricas, Cartera y Configuración quedan fuera de esta guarda — se ven
    siempre (aunque con "blocked" tampoco se llega hasta aquí, ver main:
    ese caso corta antes con configuracion_view())."""
    if license_mode != "blocked":
        return True
    st.info("Disponible con licencia activa (gratuita o completa) — despliega "
            "**Conexión y licencia**, en el sidebar, e introduce tu código.")
    return False


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------

def _stable_cache_key(paths: tuple) -> str:
    """Sustituye cada ruta por su nombre de fichero antes de que
    `st.cache_data` la hashee — ver docstring de `load_paths` para el
    motivo. Conserva el ORDEN tal cual (nunca `sorted`): el dedup de
    `P.load()` resuelve solapes con `keep="last"`, así que dos órdenes
    distintos del MISMO conjunto de ficheros pueden, en teoría, dar un
    resultado distinto — si esto sonara igual para ambos, se estaría
    devolviendo el resultado de un orden para una sesión que pidió el otro.

    Devuelve un `str`, no una `tuple`: `hash_funcs` está registrado sobre
    el tipo `tuple` (para interceptar `paths` antes de que Streamlit lo
    recorra), y devolver aquí otra tupla hace que Streamlit vuelva a
    aplicar ESTA MISMA función sobre el resultado (mismo tipo => vuelve a
    calificar para el override) — recursión infinita en la práctica. Un
    `str` no vuelve a calificar, así que corta ahí.

    INVARIANTE DE AISLAMIENTO (Fase 0 del plan de escalabilidad — revisada
    explícitamente porque esta caché es de PROCESO, compartida entre TODAS
    las sesiones/usuarios de Streamlit Cloud): dos sesiones sólo pueden
    acertar la misma entrada si sus `paths` dan el MISMO nombre de fichero.
    Todo XML de producción lo escribe `q4_ingest.FlexClient.fetch()` como
    `{query_id}_{tag}_{timestamp}_{sha256(contenido)[:12]}.xml` — el nombre
    ya incorpora el Query ID de IBKR del usuario (credencial privada, ver
    `q4_storage.py` sobre cómo se guarda) Y un hash del contenido real. Para
    que dos sesiones de USUARIOS DISTINTOS colisionaran aquí, haría falta
    que compartieran `query_id` (algo que depende de que IBKR no reutilice
    ese ID entre cuentas — no verificado contra la documentación de IBKR,
    pero query_id ya es en sí mismo un secreto por-cuenta) Y que el
    contenido descargado fuera bit a bit idéntico. No hay ninguna vía en la
    app (no hay `st.file_uploader`, ver app.py) para que un nombre de
    fichero arbitrario/no generado por FlexClient entre en `paths`. Ver
    `test_stable_cache_key_does_not_collide_across_different_content` en
    test_app_cache.py."""
    return "\x00".join(os.path.basename(p) for p in paths)


class _RawPaths(tuple):
    """Envoltorio de sólo TIPO sobre una tupla de rutas de crudos.

    `hash_funcs` registra por tipo, no por nombre de parámetro — funciones
    como `_cached_attribute()` reciben `paths` Y OTRA tupla (`accounts`) a la
    vez, así que registrar el override sobre `tuple` a secas lo aplicaría
    también a `accounts` (inofensivo hoy — `os.path.basename()` es un no-op
    sobre un ID de cuenta sin "/", pero es un accidente del que depender, no
    una garantía). Con este tipo propio, sólo `paths` califica."""
    pass


# Límites de las cachés de proceso de más abajo — sin esto, cada caché crece
# SIN TOPE mientras el proceso viva: una entrada por cada combinación de
# (histórico, cuentas, periodo, divisa...) que CUALQUIER usuario haya
# tocado alguna vez, nunca se libera sola. Con un usuario no importa; con
# varios cientos de usuarios *registrados* compartiendo el mismo proceso
# (Streamlit Cloud no da un proceso por usuario), sí.
#
# Medido con una simulación de concurrencia real (ver sesión de rendimiento):
# en reposo cada usuario nuevo en caché cuesta ~2-3 MB, pero bajo
# concurrencia de verdad (varios usuarios calculando A LA VEZ, no uno detrás
# de otro) el pico de memoria TRANSITORIA por usuario ronda los ~40 MB —
# nada se libera hasta que cada cálculo concurrente termina. Estos límites
# no evitan ese pico puntual, pero sí evitan que la caché de FONDO crezca
# sin parar con usuarios que ya no están activos.
#
# PENDIENTE DE REVISIÓN (Fase 2 del plan de escalabilidad): estos números
# son un punto de partida razonable, no una cifra derivada de carga real a
# 300 sesiones — bajo esa carga, el LRU de `max_entries` puede expulsar
# entradas todavía activas antes de que su TTL expire, justo cuando más
# falta hace la caché. NO subirlos a ciegas (eso es sólo más RAM por
# proceso, no más usuarios fluidos con margen) — ajustar con los datos que
# dé la instrumentación de q4_probe (Fase 0) bajo un load test real (Fase 5).
_CACHE_TTL = "12h"            # más que de sobra para que un usuario activo
                              # el mismo día siga encontrando su caché
                              # caliente (el objetivo de toda esta sesión);
                              # bastante para no acumular usuarios de hace
                              # días/semanas que no han vuelto.
_CACHE_MAX_DATASETS = 100     # load_paths()/_cached_selfcheck(): una entrada
                              # por histórico (usuario) — la más pesada.
_CACHE_MAX_SERIES_INPUTS = 200      # + una dimensión (cuentas)
_CACHE_MAX_ATTRIBUTE = 500          # la más "multiplicada": años_table()/
                                    # trailing_table() piden muchos (start,end)
                                    # distintos por cada usuario/cuentas/divisa
_CACHE_MAX_TRADES_PORTFOLIO = 200   # una vista a la vez por usuario, no un
                                    # historial de años como _cached_attribute
_CACHE_MAX_BENCH = 100        # fetch_bench(): dato de MERCADO, no por
                              # usuario — el mismo (ticker, periodo) sirve a
                              # todos, así que ni crece con el número de
                              # usuarios ni hace falta guardarlo tanto
                              # tiempo (ya tiene su propia caché en disco,
                              # ver q4_benchmark.py).
_CACHE_TTL_BENCH = "6h"


@st.cache_data(show_spinner="Parseando extractos…",
               hash_funcs={tuple: _stable_cache_key},
               max_entries=_CACHE_MAX_DATASETS, ttl=_CACHE_TTL)
def load_paths(paths: tuple[str, ...], _on_progress=None) -> P.Dataset:
    """Cachea por NOMBRE de fichero, no por ruta completa — `paths` trae el
    RAW_DIR de la sesión (un `tempfile.mkdtemp()` distinto cada vez, ver
    `q4_storage.init_session_storage`), así que con la ruta completa como
    clave esta caché de proceso NUNCA acertaba entre dos sesiones distintas
    (recargar la pestaña, abrir otra, que el contenedor se reciclara) —
    cada una repetía el `P.load()` completo (merge/dedup/bootstrap) aunque
    fuera exactamente el mismo histórico de siempre. El nombre de fichero sí
    es estable entre sesiones porque el XML crudo es inmutable por diseño
    (mismo argumento que `q4_parser.parse_file_cached`).

    `_on_progress` — con el guión bajo a propósito: `st.cache_data` excluye
    de la clave de caché cualquier parámetro que empiece por `_` (no lo
    hashea, ni falla si no es hasheable — una función no lo es). Así se
    puede pasar un callback de progreso sin invalidar ni fragmentar la
    caché entre llamadas que sólo difieren en quién quiere feedback visual
    ahora mismo (ver el uso en sidebar_source(), justo tras el backfill)."""
    with PR.timed("load_paths", n_files=len(paths)):
        return P.load(list(paths), on_progress=_on_progress)


def _sync_up_parsed_cache(paths: tuple[str, ...]) -> None:
    """Sube a Drive los `.parsed.json` que `P.load()` acaba de (re)generar
    junto a cada crudo de `paths` (ver `q4_parser.parse_file_cached`) — así
    la próxima sesión (recarga, otra pestaña, contenedor reciclado) los
    encuentra ya hidratados y se salta el parseo de XML entero.

    Sólo sube los que Drive NO tenga ya (una llamada a `list_files()` para
    saberlo, no N) — llamar a esto pasa en CADA carga, incluida la de "ya
    había datos, sólo mostrarlos" (líneas de abajo); sin ese filtro se
    resubiría sin cambios el mismo fichero en cada sesión, para siempre.

    `.parsed.json` vive en la subcarpeta JSON/ de Drive (ver q4_storage.py:
    `_target_subdir`), no en la raíz — de ahí `drive.subfolder("JSON")` en
    vez de `drive` a secas para saber qué hay ya."""
    if not paths:
        return
    drive = st.session_state.get("_drive_folder")
    if drive is None:
        return
    raw_dir = os.path.dirname(paths[0])
    known = drive.subfolder(ST.JSON_SUBDIR).list_files()
    names = [n for n in (os.path.basename(p) + ".parsed.json" for p in paths)
             if n not in known]
    if names:
        ST.sync_up(drive, raw_dir, *names)


@st.cache_data(show_spinner=False, hash_funcs={_RawPaths: _stable_cache_key},
               max_entries=_CACHE_MAX_SERIES_INPUTS, ttl=_CACHE_TTL)
def _cached_series_inputs(paths: _RawPaths, accounts: tuple[str, ...] | None) -> E.SeriesInputs:
    """Envoltorio cacheado de E.precompute_series_inputs() — ver su docstring
    y la de E.SeriesInputs. Clave en `(paths, accounts)` SIN start/end/divisa
    a propósito: estas piezas no dependen de ninguno de los dos (la
    conversión de divisa pasa después, dentro de build_series()).

    Esto es lo que hace que years_table() (una llamada a _attr() por año) y
    trailing_table() (una por ventana móvil) dejen de recalcular fx_matrix/
    nav_series/currency_buckets/accruals/flows sobre TODO el histórico una
    vez por cada año/ventana — con esto se calculan UNA VEZ por (histórico,
    cuentas) mostrado y se reutilizan mientras sólo cambie el periodo."""
    ds = load_paths(paths)
    return E.precompute_series_inputs(ds, list(accounts) if accounts else None)


@st.cache_data(show_spinner=False, hash_funcs={_RawPaths: _stable_cache_key},
               max_entries=_CACHE_MAX_ATTRIBUTE, ttl=_CACHE_TTL)
def _cached_attribute(paths: _RawPaths, start: str, end: str,
                      currency: str | None, accounts: tuple[str, ...] | None) -> E.Attribution:
    """Envoltorio cacheado de E.attribute() — la llamada más repetida y más
    cara del motor: aparece en las 5 pestañas de resultados (una vez suelta
    en cada una, y encima una vez POR AÑO/POR VENTANA dentro de
    years_table()/trailing_table()) y hasta ahora se recalculaba entera en
    CUALQUIER interacción en CUALQUIER sitio de la app — st.tabs() no es
    perezoso: aunque solo se vea una pestaña, las 5 ejecutan su cuerpo
    entero en cada rerun (pedido por Juan: "va un poco lento").

    Clave de caché en `paths` (tupla de ficheros, ya la usa load_paths())
    en vez del propio Dataset — un objeto Python arbitrario grande sería
    lento de hashear en cada llamada, aparte de frágil. `accounts` como
    tupla por lo mismo: las listas ya casan con el hasher de Streamlit,
    pero una tupla dijo siempre "igual que el resto del fichero".

    `paths` va envuelto en `_RawPaths` (no una tupla a secas) para que
    `hash_funcs` lo hashee por NOMBRE de fichero, igual que `load_paths()`
    — mismo motivo (RAW_DIR es un tempdir distinto por sesión, ver su
    docstring): sin esto, `load_paths(paths)` de abajo ya devolvía rápido
    entre sesiones, pero ESTA caché seguía fallando siempre igual y
    recalculando E.attribute() entero en cada sesión nueva."""
    with PR.timed("cached_attribute_miss", n_files=len(paths), accounts=len(accounts or ())):
        ds = load_paths(paths)          # ya cacheado aparte — prácticamente gratis si no cambia
        inputs = _cached_series_inputs(paths, accounts)   # ídem — una vez por (histórico, cuentas)
        return E.attribute(ds, start, end, analysis_currency=currency,
                           accounts=list(accounts) if accounts else None,
                           precomputed=inputs)


def _attr(start: str, end: str, currency: str | None,
         accounts: list[str] | None) -> E.Attribution:
    """Atajo para las 6 llamadas a E.attribute() de las vistas — pasa por
    _cached_attribute() en vez de llamar al motor directo. Toma
    st.session_state["paths"] en vez de `ds` (que las vistas SÍ tienen a
    mano) a propósito: es la clave de caché barata que ya usa load_paths(),
    no hace falta arrastrar `ds` hasta aquí para no usarlo."""
    return _cached_attribute(_RawPaths(st.session_state["paths"]), start, end, currency,
                             tuple(accounts) if accounts else None)


@st.cache_data(show_spinner="Verificando integridad de los datos…",
               hash_funcs={_RawPaths: _stable_cache_key},
               max_entries=_CACHE_MAX_DATASETS, ttl=_CACHE_TTL)
def _cached_selfcheck(paths: _RawPaths) -> list[dict]:
    """Envoltorio cacheado de SC.run_checks() — mismo patrón que
    _cached_attribute()/_cached_trades()/_cached_portfolio(): una vez por
    histórico, no en cada rerun.

    Sustituye al guard `id(ds) != st.session_state.get("_sc_id")` que había
    antes (ver git blame): `ds` sale de `load_paths()`, un `@st.cache_data`,
    y Streamlit SIEMPRE devuelve una copia nueva del valor cacheado en cada
    llamada — incluso en un acierto de caché — para que nadie mute el
    objeto compartido (confirmado: `f(1) is f(1)` da `False` con
    `@st.cache_data`). Eso significa que `id(ds)` cambiaba en TODOS los
    reruns, no solo cuando cambiaba el dataset — el guard nunca acertaba, y
    SC.run_checks() (varios recorridos completos del histórico por cuenta,
    más dos E.attribute() enteros para T4/T5, sin compartir nada con
    _cached_series_inputs) se repetía en CADA interacción, no una vez por
    dataset como decía el comentario original. Medido: ~75 ms por rerun en
    un dataset sintético de 6 años/2 cuentas — en una cuenta real, más."""
    with PR.timed("cached_selfcheck_miss", n_files=len(paths)):
        ds = load_paths(paths)
        return SC.run_checks(ds)


# Fase 4 del plan de escalabilidad (precálculo dentro de la sesión): la
# caché de _cached_selfcheck() de arriba es de PROCESO — rápida entre
# sesiones MIENTRAS el proceso siga vivo, pero no sobrevive a un
# reciclado/redeploy del contenedor de Streamlit Cloud. Este artefacto en
# el Drive del propio usuario sí — un fichero pequeño (JSON de
# resultados, no el histórico) por carpeta VeriFine, no uno por sesión.
_SELFCHECK_ARTIFACT_NAME = "selfcheck_cache.json"


def _load_precomputed_selfcheck(drive: D.DriveFolder, fingerprint: str) -> list[dict] | None:
    """`None` si no hay artefacto, si no se pudo leer, o si no coincide con
    `fingerprint` (la MISMA huella que usa `_stable_cache_key` — el nombre
    de fichero ya es content-addressed, ver su docstring, así que si
    coincide es exactamente la entrada que `_cached_selfcheck()` habría
    calculado)."""
    try:
        raw = drive.download(_SELFCHECK_ARTIFACT_NAME)
        if raw is None:
            return None
        data = json.loads(raw)
        if data.get("fingerprint") != fingerprint:
            return None
        return data.get("issues", [])
    except Exception:
        log.warning("No se pudo leer el artefacto precalculado de selfcheck; se recalcula",
                   exc_info=True)
        return None


def _persist_precomputed_selfcheck(drive: D.DriveFolder, fingerprint: str,
                                   issues: list[dict]) -> None:
    """Falla en silencio (sólo log) a propósito: esto es una optimización
    de arranque, nunca debe poder romper una sincronización que por lo
    demás fue bien — ver el uso en `_selfcheck_with_drive_cache`, que la
    lanza en segundo plano."""
    try:
        body = json.dumps({"fingerprint": fingerprint, "issues": issues}).encode("utf-8")
        drive.upload(_SELFCHECK_ARTIFACT_NAME, body, "application/json")
    except Exception:
        log.warning("No se pudo persistir el artefacto precalculado de selfcheck",
                   exc_info=True)


def _selfcheck_with_drive_cache(paths: tuple[str, ...]) -> list[dict]:
    """Envoltorio de `_cached_selfcheck()` que primero intenta el artefacto
    de Drive (ver arriba) — sólo importa en un proceso FRÍO (la caché de
    proceso de `_cached_selfcheck` está vacía): si hay un artefacto que
    coincide, se salta `SC.run_checks()` (varios recorridos completos del
    histórico, ver su docstring) enteramente en el primer render.

    Sin Drive conectado (Q4_STORAGE_BACKEND=local), o sin artefacto/sin
    coincidencia, cae en `_cached_selfcheck()` de siempre — que sí sigue
    cacheada en PROCESO para el resto de la sesión — y sube el resultado a
    Drive en segundo plano (no bloquea el render) para la próxima vez."""
    fingerprint = _stable_cache_key(tuple(paths))
    drive = st.session_state.get("_drive_folder")

    if drive is not None:
        cached = _load_precomputed_selfcheck(drive, fingerprint)
        if cached is not None:
            return cached

    issues = _cached_selfcheck(_RawPaths(paths))
    if drive is not None:
        _run_in_background("persist-selfcheck",
                           lambda: _persist_precomputed_selfcheck(drive, fingerprint, issues))
    return issues


@st.cache_data(show_spinner=False, hash_funcs={_RawPaths: _stable_cache_key},
               max_entries=_CACHE_MAX_TRADES_PORTFOLIO, ttl=_CACHE_TTL)
def _cached_trades(paths: _RawPaths, start: str, end: str,
                   accounts: tuple[str, ...] | None):
    """Mismo motivo y mismo patrón que _cached_attribute(): T.build()
    (pestaña Operaciones) también se recalculaba entero en cada rerun de
    cualquier pestaña, aunque nadie estuviera mirando Operaciones. Mismo
    `_RawPaths` que _cached_attribute(), y por el mismo motivo."""
    ds = load_paths(paths)
    return T.build(ds, start, end, accounts=list(accounts) if accounts else None)


@st.cache_data(show_spinner=False, hash_funcs={_RawPaths: _stable_cache_key},
               max_entries=_CACHE_MAX_TRADES_PORTFOLIO, ttl=_CACHE_TTL)
def _cached_portfolio(paths: _RawPaths, accounts: tuple[str, ...] | None,
                      currency: str | None, weight_basis: str):
    """Mismo motivo y mismo patrón que _cached_attribute(): T.portfolio()
    (pestaña Cartera) también se recalculaba entero en cada rerun de
    cualquier pestaña, aunque nadie estuviera mirando Cartera. Mismo
    `_RawPaths` que _cached_attribute(), y por el mismo motivo.

    weight_basis en la clave de caché a propósito: cambiar entre
    "Patrimonio"/"Exposición" en el selector de la pestaña debe recalcular
    (el pastel y el peso salen distintos), no reusar el resultado del otro
    modo."""
    ds = load_paths(paths)
    return T.portfolio(ds, accounts=list(accounts) if accounts else None,
                       analysis_currency=currency, weight_basis=weight_basis)


# --------------------------------------------------------------------------
# Google Drive — conexión y resolución de la carpeta "VeriFine" (sustituye
# a "Camino A", el selector de carpeta local vía File System Access API,
# que solo funcionaba en Chrome/Edge/Opera). Con Drive: cualquier
# navegador, nada que instalar, y los datos viven en el Drive del propio
# usuario — nunca en este servidor, que puede dormir/reiniciarse en
# cualquier momento (Streamlit Community Cloud) sin perder nada. Ver
# q4_drive.py (transporte OAuth + API REST) y q4_storage.py (el espejo
# entre RAW_DIR de esta sesión y la carpeta Drive).
# --------------------------------------------------------------------------

def _oauth_secrets() -> dict:
    try:
        return dict(st.secrets.get("google_oauth", {}))
    except Exception:
        return {}


def _render_connect_landing(ls, client_id: str, redirect_uri: str) -> None:
    """Pantalla de "Conectar con Google Drive" — primera visita, o tras
    revocar el acceso. El nonce de CSRF se guarda en localStorage, no en
    session_state: la ida y vuelta a accounts.google.com es una navegación
    de página completa (no un rerun de Streamlit), así que session_state no
    sobrevive el viaje pero localStorage sí."""
    state = uuid.uuid4().hex
    ls.setItem(itemKey="vf_oauth_state", itemValue=state)
    auth_url = D.build_auth_url(client_id, redirect_uri, state)
    st.markdown(f'<div class="vf-sec">{svg("cloud", 20)}<h3>Conecta tu Google Drive</h3></div>',
               unsafe_allow_html=True)
    st.markdown(
        "VeriFine necesita un sitio donde guardar tus extractos de IBKR, el estado de "
        "sincronización, tu licencia y el token de tu Flex Query, para que no tengas "
        "que volver a introducirlos cada vez que entras. Te pedimos conectar tu cuenta "
        "de Google para que ese sitio sea **tu propio Google Drive** — no un servidor "
        "de VeriFine.")
    _guide_callout("info", "check",
                  "<strong>No se guarda nada en ningún servidor de VeriFine.</strong> Al "
                  "conectar, la app crea (o reutiliza, si ya existe de una visita "
                  "anterior) una carpeta llamada <strong>«VeriFine»</strong> dentro de tu "
                  "Drive, y ahí — solo ahí — vive toda tu información. El servidor donde "
                  "corre esta app puede reiniciarse o dormirse en cualquier momento sin "
                  "que pierdas nada, precisamente porque nunca fue él quien la guardaba.")
    st.caption("El acceso que pedimos está limitado a esa carpeta que la propia app "
              "crea — no a tu Drive entero. Puedes desconectar cuando quieras desde "
              "«Conexión y licencia» sin que se borre nada de lo ya guardado. Solo hace "
              "falta conectar una vez; las próximas visitas no lo vuelven a pedir.")
    st.link_button("Conectar con Google Drive", auth_url)


def _drive_gate() -> D.DriveFolder | None:
    """Punto de entrada obligatorio al principio de main(): sin conexión a
    Drive, nada más se ejecuta (st.stop()). Devuelve el DriveFolder ya
    resuelto, y reasigna RAW_DIR/LICENSE_PATH/IBKR_CREDS_PATH (globales del
    módulo) al directorio local de ESTA sesión, ya hidratado desde Drive.

    Escape de desarrollo: Q4_STORAGE_BACKEND=local salta todo esto y deja
    RAW_DIR en su valor por defecto (~/VeriFine/raw) — solo para
    `streamlit run app.py` en local sin credenciales de Google. Nunca debe
    ser el comportamiento en producción."""
    global RAW_DIR, LICENSE_PATH, IBKR_CREDS_PATH

    if os.environ.get("Q4_STORAGE_BACKEND") == "local":
        return None

    # Ya resuelto en un rerun anterior de esta sesión: Streamlit re-ejecuta
    # el fichero entero en cada rerun, así que RAW_DIR ya ha vuelto a su
    # valor por defecto más arriba en ESTE mismo rerun — hay que
    # reasignarlo desde lo que se guardó la primera vez, sin volver a
    # tocar Drive (list/download en cada clic sería lentísimo y
    # machacaría la cuota de la API para nada).
    if "_drive_folder" in st.session_state:
        RAW_DIR = st.session_state["_raw_dir"]
        LICENSE_PATH = os.path.join(RAW_DIR, "license.json")
        IBKR_CREDS_PATH = os.path.join(RAW_DIR, "ibkr_credentials.json")
        return st.session_state["_drive_folder"]

    from streamlit_local_storage import LocalStorage
    ls = LocalStorage()

    secrets = _oauth_secrets()
    client_id, client_secret = secrets.get("client_id", ""), secrets.get("client_secret", "")
    redirect_uri = secrets.get("redirect_uri", "")
    if not (client_id and client_secret and redirect_uri):
        st.error("Falta configurar la conexión con Google Drive (secretos "
                 "`google_oauth.client_id` / `client_secret` / `redirect_uri`).")
        st.stop()

    tokens = st.session_state.get("_drive_tokens")
    if tokens is None:
        stored_refresh = ls.getItem("vf_google_refresh_token")
        params = st.query_params
        if "code" in params:
            expected_state = ls.getItem("vf_oauth_state")
            if expected_state is not None:
                ls.deleteItem("vf_oauth_state")
            if params.get("state") != expected_state:
                st.query_params.clear()
                st.error("El intento de conexión con Google no es válido "
                         "(el estado no coincide). Vuelve a intentarlo.")
                st.stop()
            try:
                tokens = D.exchange_code(client_id, client_secret, redirect_uri, params["code"])
            except D.DriveError as e:
                st.query_params.clear()
                st.error(f"No se pudo conectar con Google Drive: {e}")
                st.stop()
            if not tokens.refresh_token and stored_refresh:
                tokens.refresh_token = stored_refresh
            if tokens.refresh_token:
                ls.setItem(itemKey="vf_google_refresh_token", itemValue=tokens.refresh_token)
            st.session_state["_drive_tokens"] = tokens
            st.query_params.clear()
            st.rerun()
        elif stored_refresh:
            # Visita de vuelta: solo hay refresh_token en el navegador, sin
            # access_token vivo — el ensure_fresh() de abajo lo renueva
            # (expiry ya en el pasado a propósito para forzarlo).
            tokens = D.DriveTokens(access_token="", refresh_token=stored_refresh,
                                   expiry="2000-01-01T00:00:00+00:00")
        else:
            _render_connect_landing(ls, client_id, redirect_uri)
            st.stop()

    try:
        tokens = D.ensure_fresh(client_id, client_secret, tokens)
    except D.AuthError:
        if ls.getItem("vf_google_refresh_token") is not None:
            ls.deleteItem("vf_google_refresh_token")
        st.session_state.pop("_drive_tokens", None)
        st.warning("Tu conexión con Google Drive ha caducado o fue revocada.")
        _render_connect_landing(ls, client_id, redirect_uri)
        st.stop()
    st.session_state["_drive_tokens"] = tokens

    try:
        drive = D.DriveFolder.resolve_or_create(tokens)
    except D.DriveError as e:
        st.error(f"No se pudo acceder a tu carpeta de Drive: {e}")
        st.stop()

    RAW_DIR = ST.init_session_storage(drive, session_id=_probe_session_id())
    LICENSE_PATH = os.path.join(RAW_DIR, "license.json")
    IBKR_CREDS_PATH = os.path.join(RAW_DIR, "ibkr_credentials.json")
    st.session_state["_raw_dir"] = RAW_DIR
    st.session_state["_drive_folder"] = drive
    return drive


def _probe_session_id() -> str:
    """Identificador corto, sólo para correlacionar líneas de métrica (ver
    q4_probe) de la MISMA sesión en los logs — para poder contar sesiones
    concurrentes reales más adelante (Fase 0 del plan de escalabilidad), no
    usuarios registrados. No es un id de seguridad ni de auditoría; vive en
    session_state, así que sobrevive a los reruns pero no a una recarga de
    pestaña (eso SÍ cuenta como una sesión nueva a efectos de medir)."""
    sid = st.session_state.get("_probe_session_id")
    if sid is None:
        sid = uuid.uuid4().hex[:12]
        st.session_state["_probe_session_id"] = sid
    return sid


def _run_in_background(name: str, fn) -> None:
    """Lanza `fn` en un hilo daemon aparte, con el ScriptRunContext de
    Streamlit propagado (`add_script_run_ctx`) para que pueda tocar
    `session_state` con seguridad. Usado por el pre-warm de benchmarks
    (Fase 3) y el precálculo de la vista por defecto (Fase 4) del plan de
    escalabilidad: ninguno de los dos debe bloquear el render principal ni
    alargar el tiempo hasta la primera pantalla útil.

    `fn` debe capturar sus propios errores — una excepción sin capturar en
    un hilo así no llega a Streamlit, sólo se pierde en el log del proceso
    (sin romper la sesión, pero sin avisar tampoco: mejor que cada `fn`
    decida qué hacer con sus propios fallos, ver los usos)."""
    thread = threading.Thread(target=fn, daemon=True, name=f"verifine-{name}")
    add_script_run_ctx(thread)
    thread.start()


def _prewarm_benchmarks_in_background(ds: P.Dataset) -> None:
    """Descarga en segundo plano los tickers por defecto (`B.DEFAULT_SET`)
    sobre el rango de fechas del histórico ya cargado — sin bloquear el
    render principal (Fase 3 del plan de escalabilidad a 300 usuarios). Si
    el usuario llega a la pestaña Benchmark antes de que termine, cae en el
    camino síncrono de siempre (`fetch_benchmark` ya cachea en disco, ver
    q4_benchmark.py) — esto sólo ADELANTA el momento en que se descarga,
    nunca sustituye la vía de red.

    Una vez por sesión (marcado en `session_state`): Streamlit reejecuta
    `main()` en cada rerun, así que sin esta guarda se relanzaría el hilo en
    cada clic."""
    if st.session_state.get("_bench_prewarmed") or not ds.dates:
        return
    st.session_state["_bench_prewarmed"] = True
    start, end = ds.dates[0], ds.dates[-1]

    def _warm():
        for ticker in B.DEFAULT_SET:
            try:
                B.fetch_benchmark(ticker, start, end)
            except Exception:
                log.warning("Pre-warm de benchmark %s falló; se reintentará bajo "
                           "demanda cuando el usuario abra Benchmark", ticker, exc_info=True)

    _run_in_background("bench-prewarm", _warm)


def _connection_fields() -> tuple[str, str]:
    """Token, Query ID. Se llama DENTRO del expander colapsable de main()
    (igual que license_gate(), ver su docstring) — de ahí st.X en vez de
    st.sidebar.X.

    Antes había una casilla "Recordar en este equipo" (opt-in) para guardar
    el token/Query ID en disco local. Con Google Drive conectado
    (obligatorio para llegar aquí, ver _drive_gate()) ya no hace falta un
    opt-in aparte: haber conectado Drive ES el consentimiento para guardar
    datos del usuario en SU carpeta, así que se guarda solo, sin casilla."""
    st.caption("El token da acceso de lectura a tus extractos. Se guarda en "
              "tu carpeta «VeriFine» de Google Drive — no hace falta "
              "repetirlo en tu próxima visita.")
    saved_token, saved_qid = _load_ibkr_creds()
    token = st.text_input("Token", type="password", value=saved_token)
    qid = st.text_input("Query ID", value=saved_qid)
    if token and qid and (token, qid) != (saved_token, saved_qid):
        _save_ibkr_creds(token, qid)
    elif not (token or qid) and (saved_token or saved_qid):
        _clear_ibkr_creds()
    return token, qid


def _disconnect_google():
    """Desconecta la cuenta de Google de VeriFine — a diferencia de
    _danger_zone(), NO toca nada de lo que haya en la carpeta "VeriFine"
    del Drive del usuario (sigue siendo suyo, se queda tal cual). Revoca el
    token de verdad en el lado de Google (ver q4_drive.revoke — no es solo
    "olvidarlo" aquí) y lo borra de localStorage; la próxima visita vuelve
    a pedir "Conectar con Google Drive". Mismo sitio que _connection_fields()
    / _danger_zone(): dentro del expander colapsable de main()."""
    st.caption("Desconecta tu cuenta de Google de VeriFine (revoca el acceso de verdad, "
              "no solo lo olvida aquí). Lo que ya tengas guardado en tu carpeta "
              "«VeriFine» de Drive NO se toca — sigue siendo tuyo. La próxima vez que "
              "entres, tendrás que volver a conectar.")
    if st.button("Desconectar de Google"):
        tokens = st.session_state.get("_drive_tokens")
        if tokens is not None:
            try:
                D.revoke(tokens.refresh_token or tokens.access_token)
            except D.DriveError as e:
                st.warning(f"No se pudo avisar a Google (puede que ya estuviera "
                          f"desconectado igualmente): {e}")
        try:
            from streamlit_local_storage import LocalStorage
            ls = LocalStorage()
            if ls.getItem("vf_google_refresh_token") is not None:
                ls.deleteItem("vf_google_refresh_token")
        except Exception:
            pass
        for key in ("_drive_tokens", "_drive_folder", "_raw_dir"):
            st.session_state.pop(key, None)
        st.rerun()


def _danger_zone():
    """Borra TODO (extractos, estado de sincronización, token/Query ID de
    IBKR, licencia) — tanto el espejo local de esta sesión como la carpeta
    "VeriFine" en Drive. Para empezar de cero. Mismo sitio que
    _connection_fields(): dentro del expander colapsable de main()."""
    st.divider()
    st.caption("Borra todos los extractos descargados, el estado de "
              "sincronización, el token/Query ID de IBKR y el código de "
              "licencia — tanto de esta sesión como de tu carpeta "
              "«VeriFine» en Google Drive. No se puede deshacer — la "
              "próxima vez hay que sincronizar desde cero.")
    confirm_wipe = st.checkbox("Confirmo que quiero borrarlo todo", key="confirm_wipe_all")
    if st.button("Borrar todo y empezar de nuevo", disabled=not confirm_wipe):
        drive = st.session_state.get("_drive_folder")
        tokens = st.session_state.get("_drive_tokens")
        raw_dir = st.session_state.get("_raw_dir")
        ST.wipe_drive_folder(drive)
        shutil.rmtree(RAW_DIR, ignore_errors=True)
        st.session_state.clear()
        # La conexión con Google sobrevive al borrado a propósito: "Borrar
        # todo" vacía los DATOS, no debe forzar además una reconexión.
        if drive is not None:
            st.session_state["_drive_folder"] = drive
            st.session_state["_drive_tokens"] = tokens
            st.session_state["_raw_dir"] = raw_dir
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
    éste la app interactiva), no comparten proceso.

    `ST.known_raw_names()`, no `glob.glob(*.xml)` — el XML de un histórico
    ya parseado en una sesión anterior puede no estar físicamente aquí (ver
    `q4_storage.init_session_storage`), pero `P.load()` funciona igual
    porque su `.parsed.json` sí está."""
    all_raws = [os.path.join(RAW_DIR, n) for n in ST.known_raw_names(RAW_DIR)]
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


def _acquire_sync_lock(state_path: str) -> bool:
    """Cerrojo de sincronización — evita que dos ejecuciones pidan a la vez
    contra la misma query (el limitador de ritmo de FlexClient vive EN
    MEMORIA, por instancia; dos sesiones a la vez suman su ritmo sin que
    ninguna lo sepa, y eso basta para un 1025 sin que nadie haya
    reintentado a mano — ver el comentario largo en sidebar_source()).

    Con Drive conectado el cerrojo vive EN Drive (un fichero visible para
    TODAS las sesiones de esa carpeta) — el .lock local que había antes
    solo protegía sesiones que compartieran el mismo RAW_DIR, y desde que
    cada sesión tiene su propio scratch dir aislado (ver _drive_gate) ya
    nunca coinciden, así que habría dejado de proteger nada. Sin Drive
    (Q4_STORAGE_BACKEND=local, desarrollo), se mantiene el cerrojo de
    fichero de siempre — ahí sí hay un único RAW_DIR compartido."""
    drive = st.session_state.get("_drive_folder")
    if drive is not None:
        return drive.acquire_lock(os.path.basename(state_path) + ".lock",
                                  stale_seconds=LOCK_STALE_S)
    lock_path = f"{state_path}.lock"
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < LOCK_STALE_S:
            return False
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "w") as f:
        f.write(str(time.time()))
    return True


def _release_sync_lock(state_path: str) -> None:
    drive = st.session_state.get("_drive_folder")
    if drive is not None:
        drive.release_lock(os.path.basename(state_path) + ".lock")
        return
    lock_path = f"{state_path}.lock"
    if os.path.exists(lock_path):
        os.remove(lock_path)


def _resolve_raw_path(window: list) -> str:
    """La ruta absoluta que trae guardada `windows_done` (`window[2]`) es la
    que tenía cuando se descargó — con Drive de por medio, RAW_DIR es un
    directorio temporal DISTINTO en cada sesión (ver q4_storage.
    init_session_storage: tempfile.mkdtemp() nuevo cada vez), así que esa
    ruta de una sesión anterior ya no existe en esta ("[Errno 2] No such
    file or directory", visto en producción con Juan). El fichero SÍ está
    — con el mismo nombre — porque init_session_storage() ya lo bajó de
    Drive al RAW_DIR de ahora; solo hace falta recomponer la ruta con el
    nombre de fichero (estable) sobre el RAW_DIR actual, nunca fiarse del
    directorio que traiga guardado."""
    return os.path.join(RAW_DIR, os.path.basename(window[2]))


def _run_with_heartbeat(work) -> None:
    """Ejecuta `work()` — una llamada bloqueante que ya actualiza su propia
    UI por dentro (status.write()/bar.progress()/etc, callbacks de
    q4_ingest/q4_sync, sin cambios) — en un hilo aparte con el
    ScriptRunContext de Streamlit propagado (`add_script_run_ctx`), para
    que esas llamadas a `st.*` sigan funcionando exactamente igual que sin
    hilo de por medio.

    Mientras `work()` corre, el hilo PRINCIPAL — el que sostiene la
    conexión HTTP/websocket de esta sesión — manda un pulso propio cada
    ~1s, INDEPENDIENTE de cada cuánto tiquee `work()` por dentro. Los
    callbacks de IBKR ya avisan periódicamente (ver q4_ingest.py), pero su
    backoff llega hasta 60s entre avisos — en producción eso a veces ha
    bastado para que un proxy intermedio corte la conexión igualmente (ver
    el docstring de `FlexClient.fetch`, "recargué la página y los datos ya
    estaban"). Este pulso, de cadencia FIJA en vez de la del propio
    trabajo, es la garantía adicional (Fase 3 del plan de escalabilidad).

    Si `work()` deja escapar una excepción (no la captura ella misma), se
    relanza aquí en el hilo PRINCIPAL — para que el try/except de quien
    llama seguisa funcionando igual que si no hubiera hilo de por medio."""
    result: dict = {}

    def _run():
        try:
            work()
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=_run, daemon=True, name="verifine-sync-worker")
    add_script_run_ctx(thread)
    thread.start()
    heartbeat = st.empty()
    elapsed = 0
    while thread.is_alive():
        elapsed += 1
        heartbeat.caption(f"⏳ Sigue en marcha… ({elapsed}s)")
        thread.join(timeout=1.0)
    heartbeat.empty()
    if "error" in result:
        raise result["error"]


def _run_incremental_sync(token: str, qid: str, state_path: str):
    """Sincronización incremental manual: el mismo q4_sync.daily_job() que
    usa el cron (q4_daily.py), disparado a mano — para quien no tiene el
    disparador de launchd/cron instalado y quiere ponerse al día sin pedir
    otra vez todo el histórico."""
    from q4_ingest import FlexClient
    from q4_sync import daily_job

    if not _acquire_sync_lock(state_path):
        st.error("Ya hay una sincronización en marcha para esta query "
                 "(en esta u otra sesión/pestaña). Espera a que termine.")
        return
    try:
        client = FlexClient(token=token, query_id=qid, raw_dir=RAW_DIR)
        with st.status("Sincronizando desde el último día…", expanded=True) as status:
            bar = st.progress(0.0)

            def _sync_body():
                # daily_job() se llama desde un hilo aparte (ver
                # _run_with_heartbeat) — status.write()/bar.progress() aquí
                # dentro siguen funcionando igual gracias a
                # add_script_run_ctx, y el pulso independiente de cadencia
                # fija lo pone _run_with_heartbeat, no este callback.
                def _tick(i, n, msg):
                    bar.progress(i / n)
                    status.write(f"({i}/{n}) {msg}")

                try:
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
                            ST.sync_up(st.session_state.get("_drive_folder"), RAW_DIR,
                                      os.path.basename(res["raw_path"]),
                                      os.path.basename(state_path))
                    elif res["status"] == "no_new_data":
                        status.update(label="Ya estabas al día", state="complete")
                        st.info("Sin sesiones nuevas — nada que traer.")
                        if res.get("raw_path"):
                            # El XML ya se descargó (aunque no trajera fechas
                            # nuevas) — se sube igual para no perderlo de vista
                            # en cuanto termine esta sesión.
                            ST.sync_up(st.session_state.get("_drive_folder"), RAW_DIR,
                                      os.path.basename(res["raw_path"]))
                    else:
                        status.update(label="No se pudo sincronizar", state="error")
                        st.error(res.get("reason", res["status"]))
                except Exception as e:
                    status.update(label="Error en la sincronización", state="error")
                    st.error(str(e))

            _run_with_heartbeat(_sync_body)
    finally:
        _release_sync_lock(state_path)


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

    if license_mode == "blocked":
        # A petición expresa: NO se limita lo que se puede DESCARGAR sin
        # licencia — solo lo que se puede ANALIZAR (bloqueado del todo sin
        # al menos la gratuita, ver main()). Así, en cuanto se consigue una
        # licencia, ya está todo sincronizado y no hay que volver a
        # descargar nada.
        st.caption("Sin una licencia válida no se puede **analizar** — pero puedes "
                  "descargar todo el histórico que quieras desde ya (consigue la tuya "
                  "en la pestaña Configuración, paso «Introduce tu licencia»).")
    elif license_mode == "free":
        st.caption(f"Con licencia gratuita, el **análisis** se limita a los últimos "
                  f"{FREE_MONTHS} meses — la descarga no tiene ese límite.")

    default_start = pd.Timestamp(existing_start) if existing_start else pd.Timestamp("2023-01-01")
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
                tuple(_resolve_raw_path(w) for w in state_now.windows_done)).dates[-1]
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
    # razón para seguir sabiendo el estado de la licencia es el aviso de
    # más abajo, en el botón "Sincronizar" — la descarga en sí NUNCA se
    # limita por licencia (a petición expresa: solo el análisis, capado de
    # verdad en main() sobre `funded`), para no obligar a re-sincronizar
    # cuando se consiga o se amplíe la licencia.
    show_blocked_notice = license_mode == "blocked"

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

        if show_blocked_notice:
            st.info("Sin licencia activa: puedes descargar todo el histórico que "
                    "quieras, pero el **análisis** queda bloqueado hasta que "
                    "consigas una licencia válida — gratuita o de pago, ver la "
                    "pestaña Configuración.")

        # Candado por query: el limitador de ritmo de FlexClient (1 req/s,
        # 10/min) vive EN MEMORIA, dentro de esa instancia — no es global. Si
        # dos ejecuciones piden a la vez contra la misma query (dos pestañas,
        # una página recargada mientras la sincronización anterior seguía
        # corriendo en el servidor, un script suelto tipo probe_ibkr.py a la
        # vez que la app), cada una respeta su propio ritmo pero IBKR ve la
        # suma — eso basta para un 1025 sin que nadie haya reintentado a
        # mano. Ver _acquire_sync_lock(): con Drive conectado el candado
        # vive en la carpeta compartida, no en un fichero local por sesión.
        if not _acquire_sync_lock(state_path):
            st.error(
                "Ya hay una sincronización en marcha para esta query (en "
                "esta u otra pestaña/sesión). Pedir a la vez desde dos "
                "sitios es lo que suele acabar en un 1025 de IBKR. Espera "
                "a que termine.")
            return None

        # `signal["abort"]` sustituye a los `return None` que había antes
        # DENTRO del cuerpo de la sincronización: ahora ese cuerpo corre en
        # un hilo aparte (ver _run_with_heartbeat/_backfill_body más abajo),
        # así que un `return` allí dentro sólo saldría de esa función, no
        # de sidebar_source() — la señal es lo que permite that el `return
        # None` de más abajo (tras liberar el candado) siga pasando en los
        # mismos casos que antes: query inválida, o cualquier excepción.
        signal: dict = {}
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
                # manda ahora tráfico al websocket, y encima corre en un
                # hilo aparte con su propio pulso de cadencia fija (ver
                # _run_with_heartbeat) — doble red contra ese corte.
                poll_line = st.empty()

                def _poll_tick(i, n, fase):
                    verbo = "Pidiendo la referencia" if fase == "send" else "Esperando a IBKR"
                    poll_line.markdown(f"↻ {verbo} (intento {i}/{n})…")

                def _backfill_body():
                    try:
                        status.write("Validando la query…")
                        probe = client.fetch(on_progress=_poll_tick)
                        poll_line.empty()
                        v = validate_query(open(probe).read())
                        if not v["ok"]:
                            status.update(label="Query inválida", state="error")
                            st.error(v["note"])
                            signal["abort"] = True
                            return
                        status.write(f"Cuenta {', '.join(v['accounts'])} · query correcta")

                        state = SyncState.load(state_path, qid)
                        before_n = len(state.windows_done)
                        drive = st.session_state.get("_drive_folder")

                        def _prog(i, n, fd, td):
                            bar.progress(i / n)
                            status.write(f"Descargando bloque {i}/{n}: {fd} → {td}")

                        def _on_saved(s):
                            # Sube el estado + el XML de la ventana recién
                            # completada a Drive, UNA POR UNA (ver docstring de
                            # q4_sync.backfill, on_saved) — así una interrupción a
                            # media descarga (contenedor dormido/reiniciado) no
                            # pierde lo ya conseguido: queda en Drive, no solo en
                            # el scratch dir efímero de esta sesión.
                            last_path = s.windows_done[-1][2]
                            ST.sync_up(drive, RAW_DIR, os.path.basename(last_path),
                                      os.path.basename(state_path))

                        backfill(client, state, state_path, start.strftime("%Y%m%d"),
                                 on_progress=_prog, on_poll_progress=_poll_tick,
                                 on_saved=_on_saved)
                        # El guardado FINAL de backfill() (state.last_run) queda
                        # fuera del bucle, así que on_saved no lo cubre — un
                        # último envío recoge ese último cambio.
                        ST.sync_up(drive, RAW_DIR, os.path.basename(state_path))
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
                        paths = tuple(_resolve_raw_path(w) for w in state.windows_done
                                     if w[1] >= cutoff)
                        status.write(f"Parseando {len(paths)} extractos…")
                        st.session_state["paths"] = paths

                        # Progreso POR FICHERO, no un mensaje estático seguido
                        # de un load_paths() bloqueante — con un histórico
                        # grande recién descargado (nada en caché todavía,
                        # ver q4_parser.parse_file_cached), parsear puede
                        # tardar y antes no había ninguna señal de avance
                        # hasta que TODO terminaba. Reutiliza bar/poll_line,
                        # ya libres tras las fases de arriba (validar query +
                        # descargar bloques).
                        def _parse_tick(i, n, name):
                            bar.progress(i / n if n else 1.0)
                            poll_line.markdown(f"↻ Parseando {i}/{n}: {name}")

                        load_paths(paths, _on_progress=_parse_tick)
                        poll_line.empty()
                        bar.progress(1.0)
                        _sync_up_parsed_cache(paths)
                        # Sin `expanded=False`: si se colapsa solo, el único rastro de
                        # que ha ido bien es la etiqueta de la caja — fácil de leer
                        # como "no ha pasado nada" (fue justo lo que reportaron). Se
                        # deja abierta y además una confirmación aparte que no se
                        # colapsa con la caja.
                        status.update(label="Sincronización completa", state="complete",
                                      expanded=True)
                        st.success(f"Listo: {fetched} bloque(s) nuevo(s) · "
                                  f"{len(paths)} extractos cargados en el panel.")
                    except Exception as e:
                        status.update(label="Error en la sincronización", state="error",
                                      expanded=True)
                        st.error(str(e))
                        signal["abort"] = True

                _run_with_heartbeat(_backfill_body)
        finally:
            _release_sync_lock(state_path)
        if signal.get("abort"):
            return None
    if st.session_state.get("paths"):
        ds = load_paths(st.session_state["paths"])
        _sync_up_parsed_cache(st.session_state["paths"])
        return ds

    # Nada sincronizado todavía EN ESTA SESIÓN (recarga de página, sesión
    # nueva): si ya hay extractos en el almacén de una sincronización
    # anterior, cargarlos solos — el panel no debe quedarse en blanco
    # esperando a que se reintroduzca el token sólo para volver a ver lo
    # que ya se tenía.
    #
    # ST.known_raw_names(), no glob.glob(*.xml): con el histórico ya
    # parseado en una sesión anterior, init_session_storage() salta a
    # propósito la descarga del XML crudo (su .parsed.json ya basta, ver su
    # docstring) — un glob de *.xml a secas ve el RAW_DIR "vacío" y manda a
    # Configuración a un usuario que SÍ tiene datos.
    local = [os.path.join(RAW_DIR, n) for n in ST.known_raw_names(RAW_DIR)] or \
        sorted(glob.glob("./data/*.xml"))
    if local:
        st.sidebar.caption(f"Usando {len(local)} extractos del almacén ({RAW_DIR})")
        # Mismo session_state["paths"] que deja el camino de sincronización
        # de arriba — las vistas lo usan como clave de caché para el motor
        # (ver _cached_attribute más abajo); sin esto, recargar la página
        # con datos ya existentes dejaba esa clave vacía y la caché nunca
        # llegaba a activarse en ese caso.
        paths = tuple(local)
        st.session_state["paths"] = paths
        ds = load_paths(paths)
        _sync_up_parsed_cache(paths)
        return ds
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


def yearly_bar_chart(yt: pd.DataFrame, bench_col: str | None = None, use_total: bool = False):
    """Comparativa por año: barra horizontal ordenada por AÑO (más reciente
    arriba). Verde/rojo por signo, con el valor rotulado (+/−) para no depender
    del color. Si hay columna de benchmark, se marca como un TICK vertical
    ámbar sobre la barra de su mismo año — el patrón "bullet chart target":
    un punto de referencia, no una segunda barra, para no duplicar la lectura.

    `use_total` sólo decide el TEXTO (nombre de traza/eje/leyenda) — el valor
    ya viene en la columna "Rentabilidad" de `yt`, calculado según la misma
    magnitud por years_table()."""
    label = "Total" if use_total else "Estrategia"
    d = yt.dropna(subset=["Rentabilidad"]).copy()
    if d.empty:
        return
    # orden por año ascendente -> Plotly lo dibuja de abajo arriba, así que el
    # año más reciente queda arriba.
    d = d.sort_values(by="Año", key=lambda s: s.str[:4].astype(int))
    colors = [POS if v >= 0 else NEG for v in d["Rentabilidad"]]

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
        y=d["Año"], x=d["Rentabilidad"], orientation="h", marker_color=colors,
        name=label, width=bar_width,
        text=[f"{v:+.1f} %" for v in d["Rentabilidad"]], textposition="outside",
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
    fig.update_layout(showlegend=has_bench,
                      xaxis_title=f"Rentabilidad {'total' if use_total else 'de la estrategia'} (%)")
    if has_bench:
        fig.update_layout(
            margin=dict(t=46),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, font=dict(size=12)))
    fig.update_yaxes(type="category")
    # margen a ambos lados: deja sitio a las etiquetas fuera de barra (sobre todo
    # la de una barra negativa) para que no pisen las etiquetas del eje. Si hay
    # marcador de benchmark, entra también en el rango para que no quede cortado.
    vals = [d["Rentabilidad"].min(), d["Rentabilidad"].max(), 0.0]
    if has_bench:
        vals += [db[bench_col].min(), db[bench_col].max()]
    xmin, xmax = min(vals), max(vals)
    span = max(xmax - xmin, 1.0)
    fig.update_xaxes(range=[xmin - span * 0.22, xmax + span * 0.22])
    st.plotly_chart(fig, use_container_width=True)
    if has_bench:
        st.caption(f"El tick ámbar es {bench_col} ese mismo año, como referencia sobre la barra "
                   f"de {label} — no es una segunda barra, para no duplicar la lectura.")


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
                accounts: list[str] | None = None, bo: dict | None = None,
                use_total: bool = False) -> pd.DataFrame:
    """`use_total` gobierna qué magnitud se calcula por año — Total (con
    efecto divisa) o Estrategia (FX-neutral, por defecto) — compartido con
    trailing_table()/benchmark_section() vía el mismo botón en _metricas_tab
    (a petición expresa: los tres bloques deben leerse en la misma magnitud
    a la vez, no cada uno por su lado)."""
    rows = []
    for y in sorted({d[:4] for d in dates}):
        prior = [d for d in dates if d < y + "0101"]
        year_dates = [d for d in dates if d.startswith(y)]
        a = prior[-1] if prior else dates[0]      # sin cierre previo, arranca en el 1er día fondeado
        if not year_dates or a >= year_dates[-1]:
            continue
        b = year_dates[-1]
        try:
            att = _attr(a, b, currency, accounts)
        except Exception:
            continue
        series = att.series_total if use_total else att.series_strategy
        m = M.from_series(series, rf=rf)
        partial = not m.annualizable
        rows.append({
            "Año": y + (" YTD" if partial else ""),
            "Rentabilidad": (att.total if use_total else att.strategy) * 100,
            "Vol": m.vol, "Sharpe": m.sharpe, "Máx. DD": m.max_dd,
            f"{bo['name']}" if bo else "Benchmark": _bench_window_return(bo, dates, a, b),
        })
    return pd.DataFrame(rows)


def trailing_table(ds: P.Dataset, currency: str, rf: float, dates: list[str],
                   accounts: list[str] | None = None, bo: dict | None = None,
                   use_total: bool = False) -> pd.DataFrame:
    """Mismo `use_total` que years_table() — ver su docstring. Antes esta
    tabla mostraba SIEMPRE "Acum. total" y "Acum. estrategia" a la vez, pero
    Vol/Máx. DD/Anualizado sólo se calculaban de la Estrategia — a petición
    expresa, ahora TODO (incluido el acumulado) sale de la magnitud elegida,
    una sola columna, consistente con los otros dos bloques."""
    bench_col = bo["name"] if bo else "Benchmark"
    rows = []
    for w in M.trailing_windows(dates):
        if not w["available"]:
            rows.append({"Ventana": w["label"], "Acumulado": None, "Anualizado": None,
                         "Vol": None, "Máx. DD": None, bench_col: None,
                         "Nota": f"Sin datos · requiere histórico desde {w['needs']}"})
            continue
        att = _attr(w["start"], w["end"], currency, accounts)
        series = att.series_total if use_total else att.series_strategy
        m = M.from_series(series, rf=rf)
        rows.append({"Ventana": w["label"],
                     "Acumulado": (att.total if use_total else att.strategy) * 100,
                     "Anualizado": m.cagr, "Vol": m.vol, "Máx. DD": m.max_dd,
                     bench_col: _bench_window_return(bo, dates, w["start"], w["end"]),
                     "Nota": ""})
    return pd.DataFrame(rows)


@st.cache_data(show_spinner="Descargando benchmark de Yahoo…",
               max_entries=_CACHE_MAX_BENCH, ttl=_CACHE_TTL_BENCH)
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
        att = _attr(d0, d1, currency, accounts)
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


def benchmark_section(strat_series: E.Series, rf: float, tickers: list[str],
                      use_total: bool = False):
    """§15 — compara la magnitud elegida (Total o Estrategia FX-neutral,
    mismo botón que years_table()/trailing_table() en _metricas_tab) contra
    cada benchmark elegido en la barra lateral, en su divisa local (USD). La
    beta va siempre junto al alfa (§15.5). Incluye volatilidad y máx.
    drawdown DEL benchmark en solitario (no relativos), para leer el riesgo
    sin tener que calcularlo.

    §15.2 original: comparar Total (con efecto divisa) contra un benchmark
    en divisa distinta mezcla el resultado con el tipo de cambio, no es
    "sistema contra sistema" — a petición expresa, se ofrece igualmente como
    opción (con la etiqueta y el pie de página dejando claro qué se está
    mirando en cada caso), no se fuerza siempre a Estrategia."""
    section_header("target", "Comparación con benchmark — detalle")
    if not tickers:
        st.caption("Elige un benchmark en la barra lateral para ver el detalle.")
        return

    label = "Total" if use_total else "Estrategia"
    acum_col = f"Acum. {label.lower()}"
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
            acum_col: m["cum_portfolio"], "Acum. bench": m["cum_benchmark"],
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
    st.dataframe(style(pd.DataFrame(rows), [acum_col, "Acum. bench", "Alfa"]),
                 width='stretch', hide_index=True,
                 column_config={
                     acum_col: st.column_config.Column(
                         label="Acum. total" if use_total else "Acum. estr.",
                         help=f"Acumulado de {'Total (con efecto divisa)' if use_total else 'la Estrategia (FX-neutral)'}"),
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
    if use_total:
        st.caption(
            f"Total (con efecto divisa incluido) frente al benchmark en su divisa local (USD) "
            f"— aquí SÍ entra el tipo de cambio en la comparación (a diferencia de la lectura "
            f"Estrategia, §15.2). Volatilidad y máx. drawdown son del benchmark en solitario; "
            f"alfa y ratios con rf = {rf*100:.2f} %; la beta va al lado del alfa (§15.5).")
    else:
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
    detail, aggs = _cached_trades(_RawPaths(st.session_state["paths"]), d0, d1,
                                  tuple(accounts) if accounts else None)
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
    section_header("pie-chart", "Cartera")
    # st.segmented_control, no st.radio — mismo formato de botones que ya usa
    # el selector "Gráfico" (Curva de capital/Drawdown) en Métricas, para que
    # los dos selectores de la app se vean iguales. Puede devolver None si se
    # deselecciona (clic otra vez sobre el ya activo) — de ahí el `or`.
    weight_label = st.segmented_control(
        "Visualizar por", ["Patrimonio", "Exposición"],
        default="Patrimonio", key="portfolio_weight_basis",
        help="Patrimonio: solo acciones y efectivo, igual que siempre. "
             "Exposición: añade futuros y opciones (en valor absoluto, largo o "
             "corto sin compensar) — las 3 cajitas de abajo no cambian con esto, "
             "solo el pastel y el «% Peso» de la tabla de Posiciones.") or "Patrimonio"
    weight_basis = "exposicion" if weight_label == "Exposición" else "patrimonio"

    snap = _cached_portfolio(_RawPaths(st.session_state["paths"]),
                             tuple(accounts) if accounts else None, currency, weight_basis)
    if not snap.positions and not snap.cash:
        st.info("No hay posiciones abiertas ni efectivo en las cuentas seleccionadas.")
        return

    d = snap.as_of
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
            # 9 porciones (top 8 + "Otros", ya recortado en q4_trades.portfolio).
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
        "**Patrimonio** = acciones + efectivo, en la divisa base de la cuenta. **Exposición "
        "total** le añade el nocional bruto de futuros y opciones —largo o corto, sin "
        "compensar entre sí— porque un corto expone al mercado igual que un largo; no es "
        "capital inmovilizado (mismo criterio que el NAV, §2/NON_NAV_CATEGORIES). Estas dos "
        "cajitas no cambian con el selector de arriba.")
    if weight_basis == "exposicion":
        st.caption(
            "**Modo Exposición** (pastel y tabla de abajo): futuros y opciones SÍ entran, en "
            "valor absoluto —un corto pesa igual que un largo, sin compensar— y el % se "
            "calcula sobre la Exposición total, no sobre el Patrimonio.")
    else:
        st.caption(
            "**Modo Patrimonio** (pastel y tabla de abajo): solo acciones y efectivo. Futuros "
            "y opciones no entran aquí (no son capital inmovilizado) — ni en el pastel, ni en "
            "el % Peso, ni en la tabla de Posiciones — cambia a «Exposición» arriba para "
            "verlos.")

    # A petición expresa, el efectivo entra como filas más de ESTA MISMA
    # tabla (antes iba en una tabla "Efectivo" aparte, §21.4 original) — una
    # fila por DIVISA, combinando todas las cuentas visibles, para que
    # compita en el mismo orden por "% Peso" que las acciones/futuros.
    if snap.positions or snap.cash:
        section_header("layers", "Posiciones")
        _positions_table(snap, weight_basis)


def _positions_table(snap, weight_basis):
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
    # se puede recuperar. "Entrada" (fecha) se quita también, a propósito,
    # para hacer sitio a Total/% Peso (a petición expresa) sin volver a
    # chocar con el límite de columnas de más abajo.
    positions = snap.positions
    if weight_basis == "patrimonio":
        # En modo Patrimonio, futuros/opciones no cuentan ni para el
        # patrimonio ni para su % (mismo criterio que el pastel —
        # in_equity_weight, ver q4_trades.PIE_EXCLUDED_KINDS) — que
        # tampoco aparezcan aquí, para no sugerir un peso sobre un total
        # del que en realidad están excluidos. En Exposición sí entran.
        positions = [p for p in positions if p.in_equity_weight]
    if not positions and not snap.cash:
        st.caption("Sin acciones, futuros/opciones ni efectivo en las cuentas "
                  "seleccionadas.")
        return

    # Efectivo, combinado por DIVISA (a petición expresa: una fila por
    # divisa, sumando todas las cuentas visibles — no una fila por
    # cuenta+divisa). pct_weight es lineal en value_analysis_ccy (mismo
    # weight_total para toda la cartera), así que sumar los % ya calculados
    # por fila da el mismo resultado que recalcular sobre la suma — no
    # hace falta que este bloque sepa nada de equity_total/exposure_total.
    cash_by_ccy: dict[str, dict] = {}
    for c in snap.cash:
        agg = cash_by_ccy.setdefault(c.currency, {"balance": 0.0, "total": 0.0, "pct": 0.0})
        agg["balance"] += c.balance_local
        agg["total"] += c.value_analysis_ccy
        agg["pct"] += c.pct_weight or 0.0

    # "None" en vez del valor vacío: comprobado en vivo que st.dataframe
    # pinta "None" para todo NaN numérico en una NumberColumn, ignorando el
    # formateo de pandas.Styler, `na_rep` y el propio `format=` de
    # NumberColumn (por eso Posiciones/Efectivo iban antes en dos tablas
    # separadas, §21.4 original) — la única forma real de que la celda
    # salga en blanco es que la columna no sea numérica ahí. Por eso
    # Cantidad/Precio entrada/Plusvalía/% se formatean aquí a TEXTO ya
    # resuelto (vacío para efectivo) en vez de dejarlos como float con NaN;
    # el color de Plusvalía/% se calcula aparte, a partir de dos columnas
    # ocultas con el valor numérico crudo (_raw_gain/_raw_pct), porque una
    # vez convertidas a texto ya no se puede colorear por signo numérico
    # directamente sobre esas mismas columnas.
    def _txt(v, signed=False):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return f"{v:+,.2f}" if signed else f"{v:,.2f}"

    total_col = f"Total ({snap.currency})"
    rows = [dict(
        Cuenta=p.account, Ticker=p.symbol, Tipo=p.kind.capitalize(),
        Divisa=p.currency, Cantidad=_txt(p.quantity),
        **{"Precio entrada": _txt(p.entry_price)},
        **{"Plusvalía": _txt(p.unrealized_gain_local, signed=True)},
        **{"%": _txt(p.pct_return * 100 if p.pct_return is not None else None, signed=True)},
        **{total_col: p.value_analysis_ccy, "% Peso": p.pct_weight},
        _raw_gain=p.unrealized_gain_local,
        _raw_pct=p.pct_return * 100 if p.pct_return is not None else None,
    ) for p in positions]
    rows += [dict(
        Cuenta="Todas", Ticker=ccy, Tipo="Efectivo",
        Divisa=ccy, Cantidad=_txt(agg["balance"]),
        **{"Precio entrada": "", "Plusvalía": "", "%": ""},
        **{total_col: agg["total"], "% Peso": agg["pct"]},
        _raw_gain=None, _raw_pct=None,
    ) for ccy, agg in sorted(cash_by_ccy.items())]

    pos_df = pd.DataFrame(rows).sort_values("% Peso", ascending=False)

    def _row_colors(row):
        out = {c: "" for c in row.index}
        for col, raw_col in (("Plusvalía", "_raw_gain"), ("%", "_raw_pct")):
            raw = row[raw_col]
            if raw is not None and pd.notna(raw):
                out[col] = f"color:{POS if raw >= 0 else NEG}"
        return pd.Series(out)

    # "Cantidad" y "Total"/"% Peso" NO van coloreados: un corto es negativo
    # por convención (o representa un pasivo en Total), no una pérdida —
    # colorearlos en rojo confundiría dirección/magnitud con resultado.
    sty = (pos_df.style
          .apply(_row_colors, axis=1)
          .format({total_col: lambda v: "—" if pd.isna(v) else f"{v:,.2f}",
                   "% Peso": lambda v: "—" if pd.isna(v) else f"{v:,.2f}"})
          .set_properties(**{"background-color": CARD, "color": FG}))
    st.dataframe(sty, width='stretch', hide_index=True,
                column_config={
                    "Cuenta": st.column_config.TextColumn(width=80),
                    "Ticker": st.column_config.TextColumn(width=60),
                    "Tipo": st.column_config.TextColumn(width=65),
                    "Divisa": st.column_config.TextColumn(width=52),
                    "Cantidad": st.column_config.TextColumn(width=65),
                    "Precio entrada": st.column_config.TextColumn(width=85),
                    "Plusvalía": st.column_config.TextColumn(width=85),
                    "%": st.column_config.TextColumn(width=55),
                    total_col: st.column_config.NumberColumn(width=95),
                    "% Peso": st.column_config.NumberColumn(width=65),
                    "_raw_gain": None, "_raw_pct": None,
                })
    st.caption(f"Cantidad negativa = posición corta. **Total** y **% Peso** en la divisa "
              f"base de análisis ({snap.currency}), sobre la misma base que el pastel de "
              "arriba (Patrimonio o Exposición, según el selector) — no en la divisa local "
              "de cada fila (columna Divisa). Efectivo combinado por divisa, todas las "
              "cuentas visibles. Orden por defecto: de mayor a menor peso. Precio actual, "
              "dirección explícita y fecha de entrada se han quitado de esta tabla para "
              "que quepa sin recortarse.")


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
            att = _attr(d0, d1, currency, accounts)
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
            a = _attr(d0, d1, ccy, accounts)
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
BRANDING_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "assets", "branding")


def _onboarding_screenshot(label: str, filename: str):
    """Capturas reales del Client Portal como apoyo — opcionales: si el
    fichero no está (aún) en assets/onboarding/, no rompe la guía, solo no
    muestra el expander."""
    path = os.path.join(ONBOARDING_ASSETS_DIR, filename)
    if not os.path.exists(path):
        return
    with st.expander(label):
        st.image(path, use_container_width=True)


def _inject_header_title() -> None:
    """El título grande que antes iba arriba del todo del cuerpo (icono +
    "VeriFine" + coletilla, ver git blame) vive ahora SÓLO en la cabecera
    fija de Streamlit ([data-testid="stHeader"], siempre visible, no se va
    con el scroll — a petición expresa) — al 90% de aquel tamaño (la
    cabecera mide {_HEADER_H}px, ver su definición, agrandada para que
    quepa) y con "by Quant4all" añadido en medio, a la mitad del tamaño de
    "VeriFine".

    Reutiliza las clases `vf-headline`/`vf-title`/`vf-tagline`/`vf-ico` de
    THEME_CSS tal cual (mismo verde del icono, mismo resplandor del
    título, la coletilla ya en 0.6em relativo — así que escala sola con
    `title_size`) — el HTML se inyecta en el documento PADRE, no en un
    iframe aislado, así que SÍ hereda ese CSS. stHeader es DOM propio de
    Streamlit — st.markdown() no puede insertar nada ahí, sólo añade a la
    columna de contenido normal. En su lugar, esto corre en un <iframe>
    (components.html) y alcanza `window.parent.document` — mismo origen
    (todo Streamlit), el navegador lo permite. Con guarda de idempotencia
    (getElementById): main() se re-ejecuta entero en cada rerun de
    Streamlit, así que sin la guarda se apilaría una copia nueva cada vez."""
    # 0.9 (90%) medía 932px de ancho en vivo — no cabía en los 644px
    # disponibles en la cabecera (entre la barra lateral y Deploy/⋮), se
    # cortaba por la izquierda. 0.56 es lo que de verdad cabe con margen,
    # comprobado en vivo.
    _SCALE = 0.56
    icon_size = round(60 * _SCALE)
    title_size = round(60 * _SCALE)
    by_size = round(title_size * 0.5)
    # [data-testid="stHeader"] * { color: var(--fg) !important; } (más
    # arriba en THEME_CSS, puesta para forzar el color de los iconos
    # NATIVOS de Streamlit dentro de la cabecera) alcanza también a
    # cualquier cosa que se inyecte aquí dentro — sin el !important propio
    # de cada span, esa regla se comía el verde/blanco/apagado y lo dejaba
    # todo en var(--fg) (comprobado en vivo: el icono y "VeriFine" salían
    # en el color de texto normal, no en verde). El propio icono necesita
    # el color en su MISMO tag (el selector universal `*` también lo
    # alcanza a él directamente, envolverlo en un span de color no basta).
    icon_html = svg("target", icon_size).replace(
        'class="vf-ico"', f'class="vf-ico" style="color:{POS} !important"', 1)
    inner_html = (
        f'{icon_html}'
        f'<h1 class="vf-title" style="font-size:{title_size}px">'
        f'<span style="color:{POS} !important;">VeriFine</span> '
        f'<span style="font-size:{by_size}px;font-weight:600;color:{FG} !important;">'
        f'by Quant4all</span> '
        f'<span class="vf-tagline" style="color:{MUTED} !important;">'
        f'| Audita tu cuenta de IBKR</span></h1>'
    )
    components.html(f"""
        <script>
        (function() {{
          var doc = window.parent.document;
          var header = doc.querySelector('[data-testid="stHeader"]');
          if (!header) return;
          // Busca-o-crea, NO "si ya existe no lo toques": en el primer
          // render de la sesión, el tema puede caer por defecto en "dark"
          // hasta que el componente de detección avisa de verdad (ver
          // _apply_theme_palette) — con "si ya existe, no lo toques" ese
          // primer color (a veces el equivocado) se quedaba fijo para
          // siempre, porque este elemento vive FUERA del ciclo normal de
          // rerender de Streamlit (nadie más lo vuelve a tocar). Actualizar
          // el innerHTML en CADA rerun deja que la corrección de tema
          // (POS/FG/MUTED ya resueltos bien) llegue aquí también.
          var el = doc.getElementById('vf-header-title');
          if (!el) {{
            el = doc.createElement('div');
            el.id = 'vf-header-title';
            el.className = 'vf-headline';
            el.style.cssText = 'position:absolute;left:50%;top:50%;'
              + 'transform:translate(-50%,-50%);white-space:nowrap;'
              + 'pointer-events:none;margin:0;overflow:hidden;'
              // Con centrado + ancho fijo, una ventana más estrecha que el
              // portátil normal donde esto se probó (1280px, sobraban
              // 138px antes de "Deploy") podría acabar montándose sobre
              // Deploy/⋮ — este tope, con elipsis, es la red de seguridad
              // para esos casos en vez de fiarlo todo a un tamaño que sólo
              // se probó a un ancho.
              + 'max-width:calc(100% - 220px);text-overflow:ellipsis;';
            header.appendChild(el);
          }}
          el.innerHTML = {json.dumps(inner_html)};
        }})();
        </script>
    """, height=0)


def _clickable_logo(filename: str, href: str, alt: str, title: str, subtitle: str) -> None:
    """CTA con logo clicable — SOLO para el paso "Introduce tu licencia" de
    configuracion_view() (el de Substack). Opcional, igual que
    _onboarding_screenshot: si el fichero no está (aún) en assets/branding/,
    no rompe la guía, sólo no se muestra.

    Va por `components.html()` (un <iframe> propio), NO por `st.markdown()`
    — comprobado en vivo con dos fallos distintos por el camino de
    markdown: (1) como `<img src="data:...;base64,...">`, el navegador NUNCA
    ejecuta las animaciones CSS (@keyframes) de un SVG cargado como recurso
    de imagen — el texto "Quant4all" se quedaba sin revelar, congelado en
    su estado inicial; (2) incrustando el SVG tal cual en el HTML de
    `st.markdown(unsafe_allow_html=True)`, el propio bloque `<style>` del
    SVG salía TRUNCADO a medio comentario (el resto del dibujo —fondo,
    rejilla, texto— desaparecía entero) — algo en el paso de Markdown a
    HTML de Streamlit no digiere bien un `<style>` con @keyframes dentro de
    contenido "unsafe". Un `<iframe>` no pasa por ninguno de los dos
    caminos: recibe el HTML tal cual, sin markdown ni saneado.

    Las variables CSS del tema (var(--card) etc.) NO cruzan al iframe —
    tiene su propio documento — así que aquí se usan los hex ya resueltos
    de las globals BG/CARD/FG/MUTED/BORDER/POS (ver _apply_theme_palette,
    ya corridas para este rerun antes de llegar aquí)."""
    path = os.path.join(BRANDING_ASSETS_DIR, filename)
    if not os.path.exists(path):
        return
    ext = os.path.splitext(filename)[1].lstrip(".").lower() or "png"
    if ext == "svg":
        with open(path, encoding="utf-8") as fh:
            svg_markup = fh.read()
        # Sin el width/height fijo del fichero (pensado para 600×600) — el
        # CSS de abajo manda sobre el tamaño real en la tarjeta; el propio
        # viewBox interno ya escala el dibujo.
        svg_markup = re.sub(r'(<svg\b[^>]*?)\s+width="[^"]*"', r"\1", svg_markup, count=1)
        svg_markup = re.sub(r'(<svg\b[^>]*?)\s+height="[^"]*"', r"\1", svg_markup, count=1)
        logo_html = svg_markup
    else:
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        logo_html = f'<img src="data:image/{ext};base64,{b64}" alt="{alt}"/>'
    components.html(f"""
        <style>
          html, body {{ margin:0; padding:0; background:transparent; }}
          a {{ display:flex; align-items:center; gap:16px; border-radius:8px;
            padding:14px 20px; border:1px solid {BORDER}; background:{CARD};
            text-decoration:none; box-sizing:border-box; height:100%;
            transition:border-color .15s ease;
            font-family:'Fira Sans',Helvetica,Arial,sans-serif; }}
          /* Sin transform:translateY en :hover a propósito — el iframe mide
             justo el alto del contenido (ver components.html más abajo), así
             que desplazar el elemento hacia arriba recortaba el borde de
             ARRIBA contra el borde del propio iframe (se veía el resto del
             borde iluminarse en verde al pasar el ratón, pero no el de
             arriba — comprobado en vivo). Sólo el color, sin movimiento. */
          a:hover {{ border-color:{POS}; }}
          a:focus-visible {{ outline:2px solid {POS}; outline-offset:2px; }}
          .logo {{ width:52px; height:52px; flex:0 0 auto; border-radius:8px;
            overflow:hidden; display:block; }}
          .logo svg, .logo img {{ width:100%; height:100%; display:block; }}
          .copy {{ display:flex; flex-direction:column; gap:2px; min-width:0; }}
          .copy strong {{ color:{FG}; font-size:14.5px; font-weight:600; }}
          .copy small {{ color:{MUTED}; font-size:12.5px; }}
        </style>
        <a href="{href}" target="_blank" rel="noopener">
          <span class="logo" role="img" aria-label="{alt}">{logo_html}</span>
          <span class="copy"><strong>{title}</strong><small>{subtitle}</small></span>
        </a>
    """, height=82)


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

    _guide_step("01", "cloud", "Conecta tu Google Drive",
                "Ya lo has hecho — es el paso obligatorio para entrar en VeriFine, por "
                "eso puedes ver esta pantalla. Tus extractos, tu licencia y el token de "
                "IBKR viven en la carpeta <strong>«VeriFine»</strong> que se creó en tu "
                "Google Drive al conectar — nunca en un servidor de VeriFine. Puedes "
                "desconectar la cuenta cuando quieras desde «Conexión y licencia», en "
                "el lateral, sin que se borre nada de lo ya guardado.")

    _guide_step("02", "target", "Introduce tu licencia",
                "Necesitas una licencia válida para probar la aplicación. Consigue "
                "una al suscribirte <strong>GRATIS</strong> a Quant4all en Substack.")
    _clickable_logo("substack_logo.svg", SUBSTACK_URL,
                    "Suscríbete a Quant4all en Substack",
                    "Suscríbete gratis a Quant4all | Systematic trading en Substack",
                    "El email de bienvenida trae tu licencia gratuita")

    _guide_step("03", "layers", "Crea la Flex Query en el Client Portal",
                "Esto es un proceso necesario para poder sincronizar la información de "
                "IBKR. Sólo tienes que hacerlo una vez. "
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

    _guide_step("04", "file-text", "Formato y configuración general",
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

    _guide_step("05", "arrows-exchange", "Genera el token del Flex Web Service",
                "En el mismo Client Portal: engranaje de Configuración → Configuración "
                "de informes → <strong>Servicio Flex Web (Flex Web Service)</strong> → "
                "Configurar → generar token. Da acceso de <strong>solo lectura</strong> "
                "a tus extractos — nunca a operar ni mover dinero.")
    _guide_callout("warn", "alert-circle",
                  "El token caduca (recomendado renovarlo cada 90 días). Si deja de "
                  "funcionar, «Sincronizar» lo dirá con un aviso claro de token caducado "
                  "— basta con generar uno nuevo aquí y pegarlo de nuevo abajo, sin "
                  "tocar la Flex Query.")

    _guide_step("06", "target", "Pega el token y el Query ID aquí",
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
    _inject_header_title()

    # El título grande que iba aquí (icono+"VeriFine"+coletilla, arriba del
    # todo del cuerpo) se ha movido a la cabecera fija de Streamlit — ver
    # _inject_header_title() más arriba — para que sea SIEMPRE visible sin
    # depender de estar en la primera pantalla ni de hacer scroll hasta
    # arriba (a petición expresa). No se duplica aquí también.
    st.sidebar.markdown(f'<div class="vf-brand">{svg("target", 22)}VeriFine</div>',
                        unsafe_allow_html=True)

    # Puerta de entrada obligatoria: sin conexión a Google Drive, nada más
    # se pinta (st.stop() dentro). RAW_DIR ya queda apuntando al scratch dir
    # de esta sesión, hidratado desde la carpeta "VeriFine" del usuario.
    _drive_gate()

    # Licencia/Token/Query ID/carpeta van en un bloque plegable — a
    # petición expresa, para no ocupar sitio en el sidebar en cada visita
    # una vez ya hay datos cargados. Plegado por defecto solo cuando YA hay
    # extractos en el almacén (comprobación barata por fichero, antes de
    # tocar nada de sesión); la primera vez, o tras "Borrar todo", se abre
    # solo porque hace falta rellenarlo.
    # ST.known_raw_names(), no glob.glob(*.xml) — mismo motivo que en
    # sidebar_source(): el XML puede faltar a propósito si ya está parseado.
    has_data = bool(ST.known_raw_names(RAW_DIR))
    with st.sidebar.expander("Conexión y licencia", expanded=not has_data):
        license_mode = license_gate()
        token, qid = _connection_fields()
        start = _history_start_field(qid, license_mode)
        st.divider()
        _disconnect_google()
        _danger_zone()

    ds = sidebar_source(license_mode, token, qid, start)
    if ds is None:
        configuracion_view()
        st.stop()

    # Sin al menos licencia gratuita, ningún panel de análisis se muestra —
    # la misma guía de Configuración (que ya lleva el paso de licencia con
    # el enlace a Substack) hace de pantalla, en vez de inventar una nueva.
    # La descarga/sincronización de arriba en el sidebar ya funcionó igual
    # sin licencia (ver sidebar_source/_history_start_field).
    if license_mode == "blocked":
        configuracion_view()
        st.stop()

    # Fase 3 del plan de escalabilidad: adelanta la descarga de Yahoo a
    # segundo plano en cuanto hay dataset, en vez de esperar a que el
    # usuario abra la pestaña Benchmark (ver docstring de la función).
    _prewarm_benchmarks_in_background(ds)

    # Quitados del sidebar a petición expresa: antes selectbox/number_input.
    # La moneda ya no es elegible — se fija sola a la moneda base de la
    # cuenta (ds.base_currency, de EquitySummaryInBase/StmtFunds, §1). 0 %
    # es el valor por defecto que ya traía el widget de tasa libre de
    # riesgo, así que ese cálculo no cambia para nadie.
    currency = ds.base_currency
    rf = 0.0
    # Licencia gratuita: el multiselect queda DESHABILITADO (no sólo
    # ignorado tras tocarlo) — a petición expresa, para que ni siquiera
    # dispare un rerun al intentar tocarlo (con `disabled=True` el
    # navegador no deja interactuar, así que no hay on_change ni recálculo
    # de por medio — antes, aunque el resultado se ignorara, cada intento
    # de cambio SÍ disparaba un rerun completo de Streamlit, y eso es lo
    # que se notaba como "se pone a calcular"). Con `default=ds.accounts`
    # y deshabilitado, el valor devuelto es siempre la lista completa.
    accounts_locked = license_mode == "free"
    accounts = st.sidebar.multiselect(
        "Cuentas", ds.accounts, default=ds.accounts, disabled=accounts_locked)
    if accounts_locked:
        # Texto SIEMPRE visible, no un tooltip que sólo aparece al pasar el
        # ratón — con el widget deshabilitado no hay "intento" que detectar
        # (el navegador bloquea la interacción antes de que llegue a
        # Streamlit), así que el aviso tiene que verse sin tener que tocar
        # nada (a petición expresa).
        st.sidebar.caption("Requiere licencia completa — con la gratuita se "
                          "analiza siempre con todas las cuentas.")

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
        # _cached_selfcheck() ya se calcula una vez por histórico, no en cada rerun
        # (ver su docstring — el guard basado en id(ds) que había aquí no funcionaba).
        # _selfcheck_with_drive_cache() añade un artefacto persistido en Drive
        # por encima (Fase 4 del plan de escalabilidad): sólo importa en un
        # proceso frío, ver su docstring.
        sc_issues = _selfcheck_with_drive_cache(st.session_state["paths"])
        for issue in sc_issues:
            (st.error if issue["level"] == "error" else st.warning)(issue["msg"])
        if not sc_issues:
            st.caption("Comprobaciones de integridad superadas: el NAV reconstruye por "
                       "cuenta, la atribución cierra y es invariante a la moneda.")

        funded_all = analyzable_dates(ds, accounts or None)

        # Con licencia gratuita, el periodo analizable se capa a los
        # últimos FREE_MONTHS SIEMPRE — independientemente de cuánto
        # histórico haya ya descargado/sincronizado (p. ej. de cuando la
        # licencia era completa, o de un backfill hecho antes de
        # verificarla). Se filtra `funded` aquí, antes de que nada más lo
        # use (slider, date_input, el "else" de más abajo) — así el tope se
        # respeta pase lo que pase por debajo, no solo cuando el slider
        # tiene rango de sobra. Con "blocked" no se llega hasta aquí (ver
        # el corte tras sidebar_source(), más arriba en main()).
        funded = funded_all
        if license_mode == "free":
            free_floor_s = (pd.Timestamp.today().normalize()
                            - pd.DateOffset(months=FREE_MONTHS)).strftime("%Y%m%d")
            funded = [d for d in funded_all if d >= free_floor_s]

        if len(funded) < 2:
            st.warning("No hay histórico con NAV positivo suficiente para analizar."
                       if license_mode == "full" else
                       f"Con licencia gratuita, solo se pueden analizar los últimos "
                       f"{FREE_MONTHS} meses — y en ese tramo no hay histórico con NAV "
                       "positivo suficiente. Consigue la licencia completa para "
                       "levantar el límite.")
            st.stop()

        if license_mode == "free" and funded[0] > funded_all[0]:
            st.info(f"Licencia gratuita: análisis limitado a los últimos {FREE_MONTHS} "
                    f"meses (desde {funded[0][6:]}/{funded[0][4:6]}/{funded[0][:4]}), aunque "
                    "tengas más sincronizado. Consigue la licencia completa para "
                    "levantar el límite.")
        elif funded[0] > ds.dates[0]:
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

        # Licencia gratuita: los tres widgets quedan DESHABILITADOS, no sólo
        # ignorados tras tocarlos — mismo motivo que el multiselect de
        # cuentas, más arriba (con `disabled=True` no hay on_change ni
        # rerun al intentar moverlos, así que no hay ni sensación de que
        # "se pone a calcular"). session_state.sld ya está fijado al rango
        # completo por el reset de arriba, así que deshabilitado siempre
        # muestra ese rango, nunca uno parcial.
        period_locked = license_mode == "free"
        st.sidebar.slider("Periodo de análisis", min_value=lo_d, max_value=hi_d,
                          key="sld", on_change=_from_slider, format="DD/MM/YYYY",
                          disabled=period_locked)
        cA, cB = st.sidebar.columns(2)
        cA.date_input("Desde", min_value=lo_d, max_value=hi_d, key="din_from",
                      on_change=_from_inputs, format="DD/MM/YYYY", disabled=period_locked)
        cB.date_input("Hasta", min_value=lo_d, max_value=hi_d, key="din_to",
                      on_change=_from_inputs, format="DD/MM/YYYY", disabled=period_locked)
        if period_locked:
            # Texto SIEMPRE visible, mismo motivo que el de cuentas arriba —
            # con los widgets deshabilitados no hay "intento" que detectar.
            st.sidebar.caption("Requiere licencia completa — con la gratuita "
                              "se analiza siempre el rango completo.")
        lo_sel, hi_sel = st.session_state.sld
        lo_s, hi_s = lo_sel.strftime("%Y%m%d"), hi_sel.strftime("%Y%m%d")
        dates = [d for d in funded if lo_s <= d <= hi_s]
    else:
        dates = funded
    if len(dates) < 2:
        st.warning("Periodo demasiado corto: elige un rango con al menos dos sesiones.")
        st.stop()
    d0, d1 = dates[0], dates[-1]

    # st.segmented_control, no st.tabs() — a propósito, y es la única razón
    # de que esto ya no sean "pestañas" nativas: st.tabs() NO es perezoso,
    # el cuerpo de las 6 se ejecuta ENTERO en cada rerun aunque solo se vea
    # una (Streamlit ya renderiza las 6 de antemano y las oculta con CSS en
    # el navegador) — cambiar cuentas o el periodo de análisis recalculaba
    # T.portfolio()/T.build()/las tablas de Métricas/Informe TODAS a la vez,
    # aunque el usuario solo estuviera mirando una (pedido por Juan: "sigue
    # siendo poco fluido" tras cachear cada pieza por separado — el
    # problema ya no era el coste de cada una, era pagarlas todas siempre).
    #
    # Con `key=`, la sección elegida vive en session_state y sobrevive a
    # reruns disparados por OTROS widgets (cuentas, periodo) — no vuelve a
    # "MÉTRICAS" solo porque cambiaste algo en el lateral.
    #
    # Contrapartida asumida a propósito: cambiar de sección ahora sí
    # dispara un rerun del servidor (antes era 100% en el navegador, porque
    # las 6 ya estaban calculadas). Se paga una vez por clic de sección, a
    # cambio de dejar de pagarlo 6 veces por cada cambio de cuentas/periodo.
    seccion = st.segmented_control(
        "Sección", ["MÉTRICAS", "CARTERA", "EFECTO DIVISA", "OPERACIONES",
                   "INFORME", "CONFIGURACIÓN"],
        default="MÉTRICAS", key="main_section", label_visibility="collapsed") or "MÉTRICAS"

    if seccion == "CARTERA":
        portfolio_view(ds, accounts or None)
    elif seccion == "EFECTO DIVISA":
        if _tab_gate(license_mode):
            efecto_divisa_view(ds, accounts or None, currency, d0, d1)
    elif seccion == "OPERACIONES":
        if _tab_gate(license_mode):
            operations_view(ds, accounts or None, d0, d1)
    elif seccion == "INFORME":
        if _tab_gate(license_mode):
            informe_view(ds, accounts or None, currency, rf, d0, d1, bench_primary)
    elif seccion == "MÉTRICAS":
        _metricas_tab(ds, currency, rf, accounts, dates, d0, d1, bench_primary, bench_tickers)
    elif seccion == "CONFIGURACIÓN":
        configuracion_view()


def _metricas_tab(ds, currency, rf, accounts, dates, d0, d1, bench_primary, bench_tickers):
    try:
        with st.spinner("Calculando TWR y atribución…"):
            att = _attr(d0, d1, currency, accounts)
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

    # Un solo botón para los tres bloques de abajo (año, ventanas móviles y
    # benchmark) — a petición expresa: antes cada uno mostraba una magnitud
    # fija (años/benchmark siempre Estrategia; ventanas mezclaba las dos sin
    # opción), y no había forma de leerlos los tres en la misma magnitud a
    # la vez. Mismo componente/formato que "Curva de capital"/"Drawdown" de
    # arriba. `key=` para que sobreviva a reruns de otros widgets (cuentas,
    # periodo) sin volver a "Estrategia" solo por eso.
    st.caption("Aplica a los tres bloques de abajo — año, ventanas móviles y "
              "benchmark: **Total** incluye el efecto divisa; **Estrategia** "
              "lo neutraliza (FX-neutral, §7.1).")
    magnitud = st.segmented_control("Magnitud", ["Total", "Estrategia"],
                                    default="Estrategia", key="metricas_magnitud",
                                    label_visibility="collapsed") or "Estrategia"
    use_total = magnitud == "Total"

    section_header("bar-chart", "Rentabilidad por año")
    yt = years_table(ds, currency, rf, dates, accounts or None, bo=bo, use_total=use_total)
    yearly_bar_chart(yt, bench_col=bench_col, use_total=use_total)

    section_header("layers", "Ventanas móviles")
    tt = trailing_table(ds, currency, rf, dates, accounts or None, bo=bo, use_total=use_total)
    st.dataframe(style(tt, ["Acumulado", "Anualizado", bench_col]),
                 width='stretch', hide_index=True,
                 # el nombre del benchmark ("S&P 500 Total Return") como cabecera
                 # ensanchaba la columna por el título y empujaba "Nota" fuera.
                 column_config={bench_col: st.column_config.Column(
                     label="Benchmark", help=bench_col)})

    benchmark_section(att.series_total if use_total else att.series_strategy,
                      rf, bench_tickers, use_total=use_total)


# Streamlit ejecuta el script con __name__ == "__main__", así que este guard
# no estorba y a la vez permite importar el módulo desde los tests.
if __name__ == "__main__":
    main()
