"""Gravação atômica e validação de nomes, caminhos e IDs de modelo."""
from __future__ import annotations

import json
import os
from pathlib import Path

from agent_launcher.domain.catalog import MODEL_PATTERN
from agent_launcher.domain.errors import LauncherError


def clean_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LauncherError(f"{label} não pode ficar vazio.")
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise LauncherError(f"{label} contém caracteres de controle.")
    return value.strip()


def project_path(value: str) -> Path:
    path = Path(clean_text(value, "Caminho")).expanduser().resolve()
    if not path.is_dir():
        raise LauncherError(f"Pasta de projeto não encontrada: {path}")
    return path


def path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
        raise LauncherError("ID de modelo inválido. Use o ID completo do catálogo, incluindo o provider.")
    return model


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
