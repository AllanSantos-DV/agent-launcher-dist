"""Executa, elevado e sem UAC, o que o Agent Launcher pede para projetos marcados como elevados. Disparado pela tarefa
agendada AgentLauncher-Elevated (RunLevel Highest, só sob demanda).

Esta é a cópia que `launcher elevated enable` instala em %ProgramData%\\agent-launcher\\elevated, pasta em que só
administrador grava. A lista de projetos elevados fica em elevated.json, ao lado deste arquivo (mesma pasta protegida):
o pedido que chega da pasta do usuário traz só o comando; o diretório de trabalho vem da lista, não do pedido. Pedido de
projeto fora da lista, velho ou malformado é recusado e registrado em runner.log.

Tipos de pedido: `run` (um comando PowerShell ou cmd elevado, com saída e código de volta) e `probe` (prova de que a
tarefa roda com token elevado). O resultado vai para um arquivo da pasta de pedidos, nunca para outro lugar.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "elevated.json"
LOG = HERE / "runner.log"
MAX_AGE = 120  # segundos: pedido mais velho que isso é descartado (não vale repetir um disparo antigo)
MAX_COMMAND = 20000
MAX_TIMEOUT = 3600
MAX_OUTPUT = 200_000
REQUEST_KEYS = {"kind", "project", "command", "shell", "timeout", "created", "result"}
SHELLS = {"powershell": ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"],
          "cmd": ["cmd.exe", "/d", "/s", "/c"]}


def log(message: str) -> None:
    """Uma linha em runner.log (pasta protegida)."""
    try:
        with open(LOG, "a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def registry() -> dict:
    """O elevated.json protegido, já conferido no formato."""
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if not isinstance(data.get("projects"), list) or not isinstance(data.get("requests"), str):
        raise ValueError("elevated.json malformado")
    return data


def validate(request: object, listed: dict, now: float | None = None) -> dict:
    """O pedido já validado, ou ValueError dizendo o que está errado."""
    if not isinstance(request, dict) or set(request) - REQUEST_KEYS:
        raise ValueError("campos do pedido")
    now = time.time() if now is None else now
    created = request.get("created")
    if not isinstance(created, (int, float)) or not 0 <= now - created <= MAX_AGE:
        raise ValueError("pedido velho ou sem data")
    kind = request.get("kind")
    if kind not in ("run", "probe"):
        raise ValueError("tipo de pedido")
    result = request.get("result")
    # só dentro da pasta de pedidos e com nome do tipo: o runner é elevado e não grava em lugar escolhido de fora
    if not isinstance(result, str) or Path(result).parent != Path(listed["requests"]) \
            or not re.fullmatch(kind + r"-[0-9a-f]{32}\.json", Path(result).name):
        raise ValueError("arquivo de resultado")
    if kind == "probe":
        return request
    if request.get("project") not in {item.get("id") for item in listed["projects"] if isinstance(item, dict)}:
        raise ValueError("projeto não está na lista de elevados")
    command = request.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
        raise ValueError("comando")
    if request.get("shell", "powershell") not in SHELLS:
        raise ValueError("shell")
    timeout = request.get("timeout", 600)
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        raise ValueError("timeout")
    return request


def token_facts() -> dict:
    """Prova de elevação deste processo: token elevado e escrita em HKLM (que exige administrador)."""
    import winreg
    facts = {"admin": bool(ctypes.windll.shell32.IsUserAnAdmin()), "pid": os.getpid(), "user": os.environ.get("USERNAME")}
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\AgentLauncherSonda", 0, winreg.KEY_WRITE):
            pass
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\AgentLauncherSonda")
        facts["hklm_write"] = True
    except OSError as error:
        facts["hklm_write"] = False
        facts["hklm_error"] = str(error)
    return facts


def run_command(request: dict, listed: dict) -> dict:
    """Executa o comando elevado na pasta do projeto (a da lista protegida) e devolve saída e código de saída."""
    folder = next(item["path"] for item in listed["projects"] if item.get("id") == request["project"])
    command = SHELLS[request.get("shell", "powershell")] + [request["command"]]
    answer = {"ok": True, "admin": bool(ctypes.windll.shell32.IsUserAnAdmin())}
    try:
        done = subprocess.run(command, cwd=folder, capture_output=True, timeout=request.get("timeout", 600),
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        answer.update(exit=done.returncode, stdout=done.stdout, stderr=done.stderr)
    except subprocess.TimeoutExpired as error:
        answer.update(ok=False, error=f"tempo esgotado ({request.get('timeout', 600)} s)", exit=None,
                      stdout=error.stdout or b"", stderr=error.stderr or b"")
    except OSError as error:
        answer.update(ok=False, error=f"não foi possível executar: {error}", exit=None, stdout=b"", stderr=b"")
    for stream in ("stdout", "stderr"):
        answer[stream] = answer[stream][-MAX_OUTPUT:].decode("utf-8", errors="replace")
    return answer


def claim(requests: Path) -> list[Path]:
    """Os pedidos pendentes, renomeados para este processo (dois disparos seguidos não pegam o mesmo arquivo)."""
    mine = []
    for path in sorted(requests.glob("request-*.json")):
        target = path.with_name(f"claimed-{os.getpid()}-{path.name}")
        try:
            os.replace(path, target)
        except OSError:
            continue  # outro runner levou
        mine.append(target)
    return mine


def run_request(path: Path, listed: dict) -> None:
    """Valida e atende um pedido, gravando a resposta no arquivo de resultado dele."""
    try:
        request = validate(json.loads(path.read_text(encoding="utf-8")), listed)
    except (OSError, ValueError) as error:
        log(f"pedido recusado ({path.name}): {error}")
        return
    answer = token_facts() if request["kind"] == "probe" else run_command(request, listed)
    with open(request["result"], "x", encoding="utf-8") as stream:  # "x": não sobrescreve o que já existe
        stream.write(json.dumps(answer))
    log(f"{request['kind']} atendido ({request.get('project', '-')}): "
        + (f"saída {answer.get('exit')}" if request["kind"] == "run" else "ok"))


def main() -> int:
    """Atende os pedidos pendentes; um pedido ruim não derruba os outros."""
    try:
        listed = registry()
        pending = claim(Path(listed["requests"]))
        if not pending:
            log("disparo sem pedido pendente")
        for path in pending:
            try:
                run_request(path, listed)
            except Exception as error:
                log(f"falha em {path.name}: {type(error).__name__}: {error}")
            finally:
                path.unlink(missing_ok=True)
    except Exception as error:
        log(f"runner falhou: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
