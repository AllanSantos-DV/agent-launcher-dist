"""Executáveis nativos (C# Native AOT) dos processos pequenos, gerados por `python nativo/compilar.py` em nativo-bin/.

Presente o executável, a interface lança a forma nativa do processo; ausente, a forma Python. As duas cumprem o mesmo
contrato. AGENT_LAUNCHER_NATIVE=0 força a forma Python.
"""
from __future__ import annotations

import os
from pathlib import Path

from agent_launcher import ROOT

FOLDER = ROOT / "nativo-bin"


def executable(name: str, folder: Path | None = None) -> Path | None:
    """nativo-bin/<name>.exe, se existe e a forma nativa não foi desligada."""
    if os.environ.get("AGENT_LAUNCHER_NATIVE") == "0" or os.name != "nt":
        return None
    path = (folder or FOLDER) / f"{name}.exe"
    return path if path.is_file() else None
