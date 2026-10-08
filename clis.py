"""Claude Code e Codex: onde ficam as instalações nativas e como instalá-los por usuário, sem admin.

Ordem (nenhuma exige admin):
  1. Instalador oficial do fabricante, por usuário e conferindo o SHA-256 do binário:
     Claude Code: https://claude.ai/install.ps1 -> ~/.local/bin/claude.exe (code.claude.com/docs/en/setup)
     Codex: https://chatgpt.com/codex/install.ps1 -> %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe
  2. npm install -g (quem instalou o launcher pelo npm tem Node; o pacote do Claude exige Node 22+).
Os instaladores oficiais acrescentam a pasta ao PATH do usuário, mas o processo atual não vê a mudança: por isso
native_paths() é consultado além do PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

NAMES = {"claude": "Claude Code", "codex": "Codex"}
OFFICIAL = {"claude": "https://claude.ai/install.ps1", "codex": "https://chatgpt.com/codex/install.ps1"}
NPM_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
NODE_MINIMUM = {"claude": 22, "codex": 18}


class CliInstallError(Exception):
    pass


def native_paths(cli: str) -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if cli == "claude":
        return [home / ".local" / "bin" / "claude.exe"]
    if cli == "codex":
        custom = os.environ.get("CODEX_INSTALL_DIR")  # o instalador oficial aceita essa pasta alternativa
        return ([Path(custom) / "codex.exe"] if custom else []) + [local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"]
    raise ValueError(cli)


def find(cli: str) -> str | None:
    """Executável do CLI: o do PATH, senão o da instalação nativa por usuário."""
    found = shutil.which(cli)
    if found:
        return found
    return next((str(path) for path in native_paths(cli) if path.is_file()), None)


def _run(command: list[str], timeout: int, environment: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          env={**os.environ, **(environment or {})},
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _tail(result: subprocess.CompletedProcess) -> str:
    lines = [line for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()]
    return " | ".join(lines[-3:]) or f"código {result.returncode}"


def install_official(cli: str) -> None:
    # O script do fabricante roda como texto (irm | iex); -ExecutionPolicy Bypass só vale onde a política da empresa
    # não está fixada por GPO, e o erro volta legível nesse caso.
    script = f"$ProgressPreference = 'SilentlyContinue'; irm '{OFFICIAL[cli]}' | iex"
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                  timeout=900, environment={"CODEX_NON_INTERACTIVE": "1"})
    if not any(path.is_file() for path in native_paths(cli)):
        raise CliInstallError(f"instalador oficial ({OFFICIAL[cli]}) não deixou o executável: {_tail(result)}")


def _node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    match = re.match(r"v(\d+)", _run([node, "--version"], timeout=30).stdout.strip())
    return int(match.group(1)) if match else None


def install_npm(cli: str) -> None:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise CliInstallError("npm não encontrado (Node.js não instalado).")
    major = _node_major()
    if major is None or major < NODE_MINIMUM[cli]:
        raise CliInstallError(f"{NPM_PACKAGES[cli]} exige Node {NODE_MINIMUM[cli]}+; encontrado: "
                              f"{'nenhum' if major is None else major}.")
    result = _run([npm, "install", "-g", NPM_PACKAGES[cli]], timeout=900)
    if result.returncode or not find(cli):
        raise CliInstallError(f"npm install -g {NPM_PACKAGES[cli]} falhou: {_tail(result)}")


def ensure(cli: str, log=print) -> str | None:
    """Garante o CLI; devolve o executável ou None (com o motivo de cada tentativa no log). Nunca pede admin."""
    found = find(cli)
    if found:
        log(f"{NAMES[cli]}: instalado ({found})")
        return found
    errors = []
    for label, step in (("instalador oficial", install_official), ("npm", install_npm)):
        try:
            log(f"Instalando {NAMES[cli]} pelo {label}…")
            step(cli)
            found = find(cli)
            if found:
                log(f"{NAMES[cli]} instalado ({found}).")
                return found
            errors.append(f"{label}: terminou sem deixar o executável")
        except (CliInstallError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{label}: {error}")
    for error in errors:
        log(f"  [falhou] {error}")
    log(f"{NAMES[cli]} não foi instalado. Instale manualmente ({OFFICIAL[cli]}) ou peça à TI; "
        f"depois rode: launcher.py cli --install {cli}")
    return None
