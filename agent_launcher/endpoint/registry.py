"""Porta fixa por conexão (o destino guarda a URL), estado do processo e subir/parar o endpoint local.

As portas ficam num arquivo só, na raiz (vale para todos os perfis), com chave "<perfil>/<conexão>": dois perfis nunca
gravam a mesma porta. A entrada antiga (só o id da conexão, do tempo de um arquivo por pasta) é adotada com a mesma
porta, porque o VS Code e o Copilot app guardam a URL."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from agent_launcher import ROOT
from agent_launcher.domain import profiles
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json

# As portas do produto começam em 47910 (as anteriores ficam livres para testes locais).
PORT_RANGE = range(47910, 48000)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def state_file(home: Path | None = None) -> Path:
    """Arquivo único na raiz; `home` fica só por compatibilidade de assinatura."""
    return profiles.root() / "endpoints.json"


def _read_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ports": {}}
    return data if isinstance(data, dict) and isinstance(data.get("ports"), dict) else {"ports": {}}


def _read(home: Path | None = None) -> dict:
    return _read_file(state_file())


def _profile_id(home: Path) -> str:
    return profiles.profile_of_home(Path(home))["id"]


def _key(home: Path, connection_id: str) -> str:
    return f"{_profile_id(home)}/{connection_id}"


def _stored(data: dict, home: Path, connection_id: str) -> tuple[int | None, bool]:
    """Porta já gravada para perfil+conexão: (porta, veio de entrada antiga a migrar)."""
    port = data["ports"].get(_key(home, connection_id))
    if isinstance(port, int) and port in PORT_RANGE:
        return port, False
    default = _profile_id(home) == profiles.DEFAULT
    taken = {value for name, value in data["ports"].items()
             if isinstance(value, int) and not (default and name == connection_id)}
    legacy = []
    if default:
        legacy.append(data["ports"].get(connection_id))  # arquivo antigo do padrão era o próprio arquivo da raiz
    old_file = Path(home) / "endpoints.json"
    if old_file.resolve() != state_file().resolve():
        legacy.append(_read_file(old_file)["ports"].get(connection_id))
    for port in legacy:
        if isinstance(port, int) and port in PORT_RANGE and port not in taken:
            return port, True
    return None, False


def _free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def port_for(home: Path, connection_id: str, reserve: bool = True) -> int:
    """Porta da conexão: a já gravada (estável: o VS Code e o Copilot app guardam a URL); senão a preferida pelo hash
    do id, ou a próxima livre da faixa."""
    data = _read()
    key = _key(home, connection_id)
    port, legacy = _stored(data, home, connection_id)
    if port is not None:
        if legacy and reserve:
            if _profile_id(home) == profiles.DEFAULT:
                data["ports"].pop(connection_id, None)
            data["ports"][key] = port
            atomic_json(state_file(), data)
        return port
    taken = {value for value in data["ports"].values() if isinstance(value, int)}
    start = int(hashlib.sha256(connection_id.encode()).hexdigest()[:8], 16) % len(PORT_RANGE)
    for offset in range(len(PORT_RANGE)):
        candidate = PORT_RANGE[(start + offset) % len(PORT_RANGE)]
        if candidate not in taken and _free(candidate):
            if reserve:
                data["ports"][key] = candidate
                atomic_json(state_file(), data)
            return candidate
    raise LauncherError(f"Nenhuma porta livre entre {PORT_RANGE.start} e {PORT_RANGE.stop - 1} para o endpoint local.")


def forget(home: Path, connection_id: str) -> int | None:
    """Libera a porta da conexão no endpoints.json (e a entrada antiga, só com o id, no padrão). Devolve a porta."""
    data = _read()
    port = data["ports"].pop(_key(home, connection_id), None)
    if _profile_id(home) == profiles.DEFAULT:
        legacy = data["ports"].pop(connection_id, None)
        port = port if port is not None else legacy
    if port is not None:
        atomic_json(state_file(), data)
    return port if isinstance(port, int) else None


def base_url(home: Path, connection_id: str) -> str:
    return f"http://127.0.0.1:{port_for(home, connection_id)}"


def health(port: int, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            data = json.loads(response.read(65536))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("agent_launcher_endpoint") else None


def server_command(home: Path, connection_id: str) -> list[str]:
    # Só biblioteca padrão: roda no Python base, sem o redirecionador da venv (um processo a menos).
    python = Path(sys.executable)
    base = Path(getattr(sys, "_base_executable", "") or "")
    if sys.prefix != sys.base_prefix and base.is_file():
        python = base
    pythonw = python.with_name("pythonw.exe")
    return [str(pythonw if pythonw.is_file() else python), str(ROOT / "endpoint.py"), "serve",
            "--home", str(Path(home).resolve()), "--connection", connection_id]


def _command_executable(command: str) -> str:
    """Primeiro token de uma linha de comando do registro ("C:\\Python\\pythonw.exe" arg ... ou sem aspas)."""
    command = command.strip()
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    return command.split(" ", 1)[0]


def _autostart_value(connection_id: str, home: Path | None) -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, _run_value(connection_id, home))[0]
    except (OSError, ImportError):
        return None


def autostart_stale(connection_id: str, home: Path | None = None) -> str | None:
    """Motivo se o início no logon está ligado com um executável que não existe mais (Python movido ou removido)."""
    value = _autostart_value(connection_id, home)
    if value and not Path(os.path.expandvars(_command_executable(value))).is_file():
        return f"o início no logon aponta para {_command_executable(value)}, que não existe mais"
    return None


def refresh_autostart(home: Path, connection_id: str) -> bool:
    """Com o início no logon ligado e o comando gravado diferente do de agora (outro Python base, pasta movida),
    regrava o valor em HKCU\\...\\Run (sem admin). Desligado fica desligado. True se regravou."""
    current = _autostart_value(connection_id, home)
    wanted = subprocess.list2cmdline(server_command(home, connection_id))
    if current is None or current == wanted:
        return False
    autostart(home, connection_id, True)
    return True


def ensure_running(home: Path, connection_id: str, timeout: float = 15) -> int:
    """Sobe o endpoint da conexão se ele não responde; devolve a porta. Processo oculto, desligado deste."""
    try:
        refresh_autostart(home, connection_id)
    except OSError as error:  # o endpoint sobe mesmo assim; o registro fica como estava
        print(f"Aviso: o início no logon do endpoint não foi regravado ({error}).", file=sys.stderr)
    port = port_for(home, connection_id)
    current = health(port)
    if _same(current, home, connection_id):
        return port
    if current:
        raise LauncherError(f"A porta {port} responde por outra conexão ({current.get('connection')}, perfil "
                            f"{current.get('profile') or profiles.DEFAULT}).")
    if not _free(port):
        raise LauncherError(f"A porta {port} do endpoint local está ocupada por outro programa. Libere a porta ou "
                            f"apague a entrada {_key(home, connection_id)} de {state_file()} para escolher outra (e abra o destino de novo pelo launcher).")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(server_command(home, connection_id), cwd=ROOT, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health(port):
            return port
        time.sleep(0.25)
    raise LauncherError(f"O endpoint local não respondeu em {timeout:.0f} s. Veja {Path(home) / 'endpoint.log'}.")


def _same(current: dict | None, home: Path, connection_id: str) -> bool:
    """O endpoint que responde é desta conexão neste perfil (endpoint antigo, sem 'profile', é do padrão)."""
    return bool(current) and current.get("connection_id") == connection_id \
        and (current.get("profile") or profiles.DEFAULT) == _profile_id(home)


def stop(home: Path, connection_id: str, timeout: float = 5) -> bool:
    port = port_for(home, connection_id, reserve=False)
    current = health(port)
    if not _same(current, home, connection_id):
        return False
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", data=b"", method="POST"), timeout=timeout).close()
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and health(port):
        time.sleep(0.2)
    return not health(port)


def _run_value(connection_id: str, home: Path | None = None) -> str:
    """Nome do valor em HKCU\\...\\Run, único por perfil+conexão (o padrão mantém o nome antigo)."""
    profile_id = _profile_id(home) if home is not None else profiles.DEFAULT
    if profile_id == profiles.DEFAULT:
        return f"AgentLauncherEndpoint-{connection_id[:8]}"
    return f"AgentLauncherEndpoint-{profile_id}-{connection_id[:8]}"


def autostart(home: Path, connection_id: str, enabled: bool) -> None:
    """Sobe o endpoint no logon do usuário (HKCU\\...\\Run, sem admin), para o VS Code e o Copilot app acharem a
    conexão mesmo sem o launcher aberto."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _run_value(connection_id, home), 0, winreg.REG_SZ,
                              subprocess.list2cmdline(server_command(home, connection_id)))
        else:
            try:
                winreg.DeleteValue(key, _run_value(connection_id, home))
            except FileNotFoundError:
                pass


def autostart_enabled(connection_id: str, home: Path | None = None) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, _run_value(connection_id, home))
            return True
    except (OSError, ImportError):
        return False


def status(home: Path, connections: list[dict]) -> list[dict]:
    data = _read()
    result = []
    for connection in connections:
        port, _legacy = _stored(data, home, connection["id"])
        current = health(port) if isinstance(port, int) else None
        result.append({"connection": connection["name"], "port": port, "running": bool(current),
                       "url": f"http://127.0.0.1:{port}/v1" if port else None,
                       "requests": (current or {}).get("requests"), "autostart": autostart_enabled(connection["id"], home),
                       "autostart_stale": autostart_stale(connection["id"], home)})
    return result
