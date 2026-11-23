"""Registro dos adaptadores: os embutidos e os do usuário (~/.agent-launcher/harnesses/*.py, cada um com HARNESS =
uma instância de Harness). Um arquivo com erro não derruba os outros (mesmo padrão dos conectores)."""
from __future__ import annotations

import importlib
from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.user_modules import load_user_modules

BUILTIN_MODULES = {"claude": "claude_code", "codex": "codex", "copilot": "copilot_cli", "vscode": "vscode",
                   "copilot_app": "copilot_app"}  # ID -> módulo: achar um adaptador sem importar os outros
BUILTIN_NAMES = tuple(BUILTIN_MODULES.values())
_loaded: dict | None = None
_errors: list[str] = []


def _builtin(names: tuple[str, ...] = BUILTIN_NAMES) -> dict:
    found = {}
    for name in names:
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
    found = {}

    def accept(module, _name):
        harness = getattr(module, "HARNESS", None)
        if harness is None or not getattr(harness, "ID", "") or not getattr(harness, "LABEL", ""):
            raise ValueError("falta HARNESS com ID e LABEL")
        for method in ("command", "plan", "eligible"):
            if not callable(getattr(harness, method, None)):
                raise ValueError(f"falta o método {method}()")
        found[harness.ID] = harness

    return found, load_user_modules(folder, "agent_launcher_harness", accept)


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
    """O adaptador com esse ID. Um embutido é achado sem carregar os outros (o processo da janela da sessão só precisa do
    seu); os do usuário e as listas completas carregam todos."""
    if _loaded is None and identifier in BUILTIN_MODULES:
        return _builtin((BUILTIN_MODULES[identifier],)).get(identifier)
    return _all().get(identifier)


def get(identifier: str):
    harness = find(identifier)
    if harness is None:
        raise LauncherError(f"CLI não suportado. Escolha um destino: {', '.join(ids())}.")
    return harness


def terminals() -> list:
    return [harness for harness in all() if harness.KIND == "terminal"]
