"""Fixtures de pytest para los tests dorados.

Los XML de referencia son extractos reales de la cuenta y NO se versionan
(rule 9: dato sensible fuera del repo). Los tests los leen de una ruta
configurable y se SALTAN con un mensaje claro si no están presentes, de modo
que un clon limpio no falla, sólo omite la regresión dorada.

    Q4_GOLDEN_DIR   directorio con los cuatro
                    Quant4all_Posiciones_Historicas_20NN.xml
                    (por defecto ~/Downloads)

En producción la app descarga los XML vía Flex Web Service (token + Query ID);
estos ficheros son únicamente el fixture que ancla los golden values de §13.
"""

from __future__ import annotations

import os
import glob
import pathlib

import pytest

DEFAULT_DIR = str(pathlib.Path.home() / "Downloads")
PATTERN = "Quant4all_Posiciones_Historicas_20??.xml"


def golden_files() -> list[str]:
    base = os.environ.get("Q4_GOLDEN_DIR", DEFAULT_DIR)
    return sorted(glob.glob(os.path.join(base, PATTERN)))


@pytest.fixture(scope="session")
def dataset():
    """El Dataset consolidado de los cuatro XML golden, cargado una sola vez."""
    files = golden_files()
    if len(files) < 4:
        pytest.skip(
            f"Fixture dorado ausente: se esperan 4 ficheros '{PATTERN}' en "
            f"{os.environ.get('Q4_GOLDEN_DIR', DEFAULT_DIR)} (encontrados {len(files)}). "
            "Define Q4_GOLDEN_DIR para apuntar a tus extractos."
        )
    import q4_parser as P
    return P.load(files)
