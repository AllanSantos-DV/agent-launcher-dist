"""Comandos de administrador sem pedido de UAC, só para os projetos que o dono marcar.

O Windows só deixa criar uma tarefa elevada com um clique de UAC, uma única vez (`launcher elevated enable`). Depois
disso, `launcher elevated run -- COMANDO` grava um pedido e dispara sob demanda a tarefa agendada AgentLauncher-Elevated
(RunLevel Highest, sem gatilho), que roda elevated_runner.py: ele executa o comando com token elevado (winget em Program
Files, firewall, perfil de rede...) e devolve saída e código de saída. A sessão em si continua comum: um Claude elevado
fica inalcançável (o pipe de mensagens dele não abre para processo comum, erro 5, e `launcher keys` também não
anexa ao console), o que quebraria o orquestrador.

A lista dos projetos marcados e o runner ficam em %ProgramData%\\agent-launcher\\elevated, só administrador grava (o
registro de consentimento: marcar ou desmarcar exige um clique). O pedido que vem da pasta do usuário traz só o comando;
o runner confere o projeto contra a lista protegida e usa a pasta dela.

Risco que sobra, dito sem rodeio: o UAC deixa de proteger esses projetos. Qualquer programa rodando como o usuário pode
disparar a tarefa e mandar um comando de um projeto marcado, que roda como administrador sem aviso. Por isso vale só
para projetos que precisam, não para todos.
"""
from __future__ import annotations

import argparse
import ctypes
import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from agent_launcher import ROOT
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, path_key
from agent_launcher.domain.store import Store
from version import data_home

TASK = "AgentLauncher-Elevated"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAIM_WAIT = 25  # segundos até o runner pegar o pedido


def protected_dir() -> Path:
    return Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "agent-launcher" / "elevated"


def requests_dir(home: Path | None = None) -> Path:
    return (home or data_home()) / "runtime" / "elevated"


def is_admin() -> bool:
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def listed(folder: Path | None = None) -> dict:
    """Conteúdo de elevated.json ({} se nunca foi configurado ou se está ilegível)."""
    try:
        data = json.loads(((folder or protected_dir()) / "elevated.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_marked(project: dict, folder: Path | None = None) -> bool:
    """O projeto está na lista protegida e continua no mesmo caminho em que foi marcado."""
    return any(item.get("id") == project["id"] and path_key(item.get("path", "")) == path_key(project["path"])
               for item in listed(folder).get("projects", []) if isinstance(item, dict))


def trigger(request: dict, home: Path | None = None, run=subprocess.run, wait: float = CLAIM_WAIT) -> None:
    """Grava o pedido e dispara a tarefa elevada; falha alto se a tarefa não existe ou não pegou o pedido."""
    folder = requests_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"request-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({**request, "created": time.time()}, ensure_ascii=False), encoding="utf-8")
    result = run(["schtasks.exe", "/run", "/tn", TASK], capture_output=True, text=True, creationflags=NO_WINDOW,
                 timeout=30)
    if result.returncode:
        path.unlink(missing_ok=True)
        raise LauncherError(f"Não foi possível disparar a tarefa elevada {TASK}: "
                            f"{(result.stderr or result.stdout).strip()}. Rode 'launcher elevated enable' (um clique de UAC).")
    deadline = time.monotonic() + wait
    while path.exists():
        if time.monotonic() > deadline:
            path.unlink(missing_ok=True)
            raise LauncherError(f"A tarefa {TASK} não pegou o pedido em {wait:.0f}s; veja {protected_dir() / 'runner.log'}.")
        time.sleep(0.1)


# ---- instalação (processo elevado) ---------------------------------------------------------------------------

def task_xml(user: str, command: str, arguments: str) -> str:
    def esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Executa, elevados e sem UAC, os comandos dos projetos que o Agent Launcher marcou como elevados.</Description></RegistrationInfo>
  <Triggers/>
  <Principals><Principal id="Author"><UserId>{esc(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{esc(command)}</Command><Arguments>{esc(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def current_user() -> str:
    output = subprocess.run(["whoami.exe"], capture_output=True, text=True, creationflags=NO_WINDOW, check=True).stdout
    return output.strip()


def task_action(system_root: str | None = None) -> tuple[str, str]:
    """(comando, argumentos) que a tarefa grava, resolvidos agora e sem caminho de Python fixo: o `pyw.exe` do launcher
    `py` (em %SystemRoot%, só administrador grava) escolhe o Python instalado a cada disparo, então mover ou trocar o
    Python não quebra a tarefa. Sem o launcher, cai no pythonw do Python base (nunca o da venv, que o usuário grava)."""
    launcher = Path(system_root or os.environ.get("SystemRoot") or r"C:\Windows") / "pyw.exe"
    if launcher.is_file():
        return str(launcher), "-3 -I"
    base = Path(getattr(sys, "_base_executable", sys.executable))
    windowless = base.with_name("pythonw.exe")
    return str(windowless if windowless.is_file() else base), "-I"


def secure(folder: Path) -> None:
    """Só SYSTEM e Administradores gravam; usuários comuns leem e executam (sem herdar o 'criar/gravar' do ProgramData)."""
    result = subprocess.run(["icacls.exe", str(folder), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
                             "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-32-545:(OI)(CI)RX"], capture_output=True, text=True,
                            creationflags=NO_WINDOW)
    if result.returncode:
        raise LauncherError(f"icacls falhou em {folder}: {(result.stderr or result.stdout).strip()}")


def apply(home: Path, mark: set[str], unmark: set[str], folder: Path | None = None, register=True) -> dict:
    """Deve rodar elevado. Atualiza a lista protegida, o runner e a tarefa; sem projeto marcado, remove a tarefa."""
    if not is_admin():
        raise LauncherError("Esta etapa precisa de administrador.")
    folder = folder or protected_dir()
    store = Store(home)
    projects = {item["id"]: item for item in listed(folder).get("projects", []) if isinstance(item, dict)}
    for reference in unmark:
        projects.pop(store.find(reference)["id"], None)
    for reference in mark:
        project = store.find(reference)
        projects[project["id"]] = {"id": project["id"], "name": project["name"], "path": project["path"]}
    folder.mkdir(parents=True, exist_ok=True)
    secure(folder)
    runner = folder / "elevated_runner.py"
    import shutil  # só na etapa elevada
    shutil.copyfile(ROOT / "elevated_runner.py", runner)
    atomic_json(folder / "elevated.json", {"requests": str(requests_dir(home)), "projects": list(projects.values())})
    requests_dir(home).mkdir(parents=True, exist_ok=True)
    if register:
        if projects:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="agent-launcher-task-", dir=str(folder)) as work:
                xml = Path(work) / "task.xml"
                command, flags = task_action()
                xml.write_text(task_xml(current_user(), command, f'{flags} "{runner}"'), encoding="utf-16")
                result = subprocess.run(["schtasks.exe", "/create", "/tn", TASK, "/xml", str(xml), "/f"],
                                        capture_output=True, text=True, creationflags=NO_WINDOW)
            if result.returncode:
                raise LauncherError(f"schtasks /create falhou: {(result.stderr or result.stdout).strip()}")
        else:
            subprocess.run(["schtasks.exe", "/delete", "/tn", TASK, "/f"], capture_output=True, creationflags=NO_WINDOW)
    return {"projects": [item["name"] for item in projects.values()], "task": bool(projects) and register}


# ---- um clique de UAC -------------------------------------------------------------------------------------------

class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def run_elevated(arguments: list[str], timeout: float = 300) -> int:
    """Roda este arquivo elevado (aparece UM prompt de UAC), sem janela, e devolve o código de saída."""
    python = Path(sys.executable)
    python = python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python
    line = subprocess.list2cmdline(["-I", str(ROOT / "elevated.py"), *arguments])
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.lpParameters, info.lpDirectory = "runas", str(python), line, str(ROOT)
    info.nShow = 0  # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise LauncherError(f"O UAC foi recusado ou não abriu (erro {ctypes.get_last_error() or ctypes.GetLastError()}).")
    kernel32 = ctypes.windll.kernel32
    try:
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
            raise LauncherError("A etapa elevada não terminou no tempo esperado.")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def change(home: Path, mark: list[str], unmark: list[str]) -> dict:
    """Marca/desmarca projetos: um clique de UAC (se já elevado, aplica direto)."""
    if is_admin():
        return apply(home, set(mark), set(unmark))
    # O resultado fica na pasta de pedidos, que herda a permissão do usuário: arquivo criado pelo processo elevado numa
    # pasta temporária só dá acesso a Administradores, e o processo comum (token sem o grupo ativo) não o lê.
    folder = requests_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    result = folder / f"apply-{uuid.uuid4().hex}.json"
    try:
        code = run_elevated(["apply", "--home", str(home), "--result", str(result),
                             *[arg for ref in mark for arg in ("--mark", ref)],
                             *[arg for ref in unmark for arg in ("--unmark", ref)]])
        try:
            outcome = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            outcome = {"ok": False, "error": f"a etapa elevada saiu com código {code} e o resultado não pôde ser lido ({error})"}
    finally:
        result.unlink(missing_ok=True)
    if not outcome.get("ok"):
        raise LauncherError(f"Não foi possível configurar a elevação: {outcome.get('error')}")
    return outcome


def await_result(result: Path, what: str, timeout: float = 30) -> dict:
    """Lê o arquivo de resultado que o runner elevado grava na pasta de pedidos e o apaga."""
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                return json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):  # ainda não existe ou está sendo gravado
                if time.monotonic() > deadline:
                    raise LauncherError(f"{what}: a tarefa elevada não respondeu em {timeout:.0f}s; "
                                        f"veja {protected_dir() / 'runner.log'}.")
                time.sleep(0.2)
    finally:
        result.unlink(missing_ok=True)


def probe(home: Path | None = None, timeout: float = 30) -> dict:
    """Prova de elevação sem janela: a tarefa grava o estado do token dela (e escreve em HKLM) num arquivo."""
    result = requests_dir(home) / f"probe-{uuid.uuid4().hex}.json"
    trigger({"kind": "probe", "result": str(result)}, home)
    return await_result(result, "sonda", timeout)


def run_admin(project: dict, command: str, shell: str = "powershell", timeout: int = 600,
              home: Path | None = None) -> dict:
    """Executa o comando elevado (sem UAC) pela tarefa, na pasta do projeto marcado, e devolve saída e código."""
    if not is_marked(project):
        raise LauncherError(f"O projeto {project['name']} não está marcado como elevado. Rode 'launcher elevated enable "
                            f"{project['name']}' (um clique de UAC).")
    result = requests_dir(home) / f"run-{uuid.uuid4().hex}.json"
    trigger({"kind": "run", "project": project["id"], "command": command, "shell": shell, "timeout": timeout,
             "result": str(result)}, home)
    return await_result(result, "comando elevado", timeout + 30)


SESSION_NOTE = (
    "ADMINISTRADOR SEM UAC (este projeto foi marcado pelo dono): comando que exige administrador (winget em Program Files "
    "ou machine scope, instalador MSI, regra de firewall, Set-NetConnectionProfile, CheckNetIsolation, serviço, pasta "
    "protegida) NÃO deve ser rodado direto: abriria um pedido de UAC que ninguém vai clicar e a sessão travaria. Rode-o "
    'assim, que executa elevado, sem UAC, e devolve saída e código de saída: {python} {launcher} elevated run -- "<comando '
    'PowerShell>" (--cmd usa o cmd; --timeout SEGUNDOS, padrão 600). Use só para o que de fato exige administrador; o resto '
    "rode normalmente."
)


def session_note(project: dict) -> str | None:
    """A instrução que o projeto marcado recebe no prompt da sessão; None para os demais."""
    if not is_marked(project):
        return None
    python = Path(sys.executable)
    python = python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python
    return SESSION_NOTE.format(python=f'"{python}"', launcher=f'"{ROOT / "launcher.py"}"')


def task_command(run=subprocess.run) -> str | None:
    """O executável que a tarefa instalada dispara (None se o XML não traz o comando). Só se chama com a tarefa já
    confirmada; se o schtasks falha ao ler o XML levanta LauncherError em vez de dizer "sem tarefa"."""
    result = run(["schtasks.exe", "/query", "/tn", TASK, "/xml"], capture_output=True, creationflags=NO_WINDOW)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        raise LauncherError(f"schtasks /query /xml da tarefa {TASK} saiu com código {result.returncode}: "
                            f"{detail.strip()[-200:] or 'sem mensagem'}")
    raw = result.stdout
    text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", "replace") if isinstance(raw, bytes) else raw
    found = re.search(r"<Command>([^<]*)</Command>", text)
    return html.unescape(found.group(1)).strip() if found else None


def stale_task(run=subprocess.run, system_root: str | None = None) -> str | None:
    """Motivo se a tarefa instalada precisa ser regravada: aponta para um executável que não existe mais (Python movido
    ou removido) ou grava o caminho de um Python quando o `pyw.exe` do Windows já resolveria o Python a cada disparo.
    Regravar exige o clique de UAC de `launcher elevated enable`; a tarefa antiga segue funcionando enquanto o Python
    dela existir."""
    try:
        command = task_command(run)
    except LauncherError as error:
        return f"não foi possível ler a tarefa {TASK} para saber se está atual: {error}"
    if not command:
        return None
    path = Path(os.path.expandvars(command.strip('"')))
    if not path.is_file():
        return (f"a tarefa {TASK} aponta para {command}, que não existe mais; rode 'launcher elevated enable' "
                "(um clique de UAC) para regravá-la")
    if path.name.lower() in ("python.exe", "pythonw.exe") and Path(task_action(system_root)[0]).name.lower() == "pyw.exe":
        return (f"a tarefa {TASK} grava o caminho de um Python ({command}); rode 'launcher elevated enable' (um clique "
                "de UAC) para regravá-la pelo pyw.exe, que acha o Python a cada disparo")
    return None


def status(store: Store) -> dict:
    task = subprocess.run(["schtasks.exe", "/query", "/tn", TASK], capture_output=True, text=True,
                          creationflags=NO_WINDOW)
    data = listed()
    return {"task": task.returncode == 0, "task_stale": stale_task() if task.returncode == 0 else None, "admin_now": is_admin(), "folder": str(protected_dir()),
            "projects": [item["name"] for item in data.get("projects", []) if isinstance(item, dict)],
            "stale": [item["name"] for item in data.get("projects", []) if isinstance(item, dict)
                      and not any(project["id"] == item.get("id") and is_marked(project) for project in store.projects)]}


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply",))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--mark", action="append", default=[])
    parser.add_argument("--unmark", action="append", default=[])
    options = parser.parse_args(arguments)
    try:
        outcome = {"ok": True, **apply(options.home, set(options.mark), set(options.unmark))}
        code = 0
    except Exception as error:  # volta como resultado legível, não como janela fechando
        outcome, code = {"ok": False, "error": f"{type(error).__name__}: {error}"}, 1
    options.result.write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    return code

