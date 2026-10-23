"""Registro dos adaptadores: os embutidos e os do usuário (~/.agent-launcher/harnesses/*.py, cada um com HARNESS =
uma instância de Harness). Um arquivo com erro não derruba os outros (mesmo padrão dos conectores)."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from agent_launcher.domain.errors import LauncherError

BUILTIN_NAMES = ("claude_code", "codex", "copilot_cli", "vscode", "copilot_app")
_loaded: dict | None = None
_errors: list[str] = []


def _builtin() -> dict:
    found = {}
    for name in BUILTIN_NAMES:
        try:
            module = importlib.import_module(f"agent_launcher.harnesses.{name}")
        except ModuleNotFoundError as error:
            if error.name == f"agent_launcher.harnesses.{name}":
                continue  # adaptador ainda não existe nesta versão
            raise
        harness = module.HARNESS
        found[harness.ID] = harness
    return found


def load_user(folder: Path | None) -> tuple[dict, list[str]]:
    found, errors = {}, []
    if folder is None or not folder.is_dir():
        return found, errors
    for path in sorted(folder.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"agent_launcher_harness_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            harness = getattr(module, "HARNESS", None)
            if harness is None or not getattr(harness, "ID", "") or not getattr(harness, "LABEL", ""):
                raise ValueError("falta HARNESS com ID e LABEL")
            for method in ("command", "plan", "eligible"):
                if not callable(getattr(harness, method, None)):
                    raise ValueError(f"falta o método {method}()")
            found[harness.ID] = harness
        except Exception as error:
            errors.append(f"{path.name}: {type(error).__name__}: {error}")
    return found, errors


def _all() -> dict:
    global _loaded, _errors
    if _loaded is None:
        loaded = _builtin()
        from version import data_home
        user, _errors = load_user(data_home() / "harnesses")
        for identifier, harness in user.items():
            if identifier in loaded:
                _errors.append(f"adaptador do usuário {identifier!r} ignorado: já existe um embutido com esse ID")
            else:
                loaded[identifier] = harness
        _loaded = loaded
    return _loaded


def all() -> list:
    return list(_all().values())


def ids() -> tuple[str, ...]:
    return tuple(_all())


def errors() -> list[str]:
    _all()
    return list(_errors)


def find(identifier: str):
    return _all().get(identifier)


def get(identifier: str):
    harness = find(identifier)
    if harness is None:
        raise LauncherError(f"CLI não suportado. Escolha um destino: {', '.join(ids())}.")
    return harness


def terminals() -> list:
    return [harness for harness in all() if harness.KIND == "terminal"]
