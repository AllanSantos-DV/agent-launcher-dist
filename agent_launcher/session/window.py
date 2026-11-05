"""Janela da sessão: abre (aba do Windows Terminal ou console) e, dentro dela, roda o destino de terminal escolhido.

A linha de comando da janela contém desktop.py e o id do projeto: o orquestrador acha a janela por ela."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys

from agent_launcher import ROOT
from agent_launcher.domain.errors import LauncherBusy, LauncherError
from agent_launcher.domain import profiles
from agent_launcher.domain.files import project_path
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import registry
from agent_launcher.platform import elevated, focus, keys, terminal
from agent_launcher.session.close import consume_close_request
from agent_launcher.session.controlled import without_parent_session
from agent_launcher.session.conversations import record_session
from agent_launcher.session.plan import build_plan
from agent_launcher.update import updater
from version import APP_ID, APP_NAME


class _Labels(dict):
    """{id: nome} dos destinos de terminal, lido do registro na hora (adaptadores do usuário inclusive)."""

    def _load(self):
        if not dict.__len__(self):
            self.update({harness.ID: harness.LABEL for harness in registry.terminals()})

    def __getitem__(self, key):
        self._load()
        return dict.__getitem__(self, key)

    def __iter__(self):
        self._load()
        return dict.__iter__(self)

    def __len__(self):
        self._load()
        return dict.__len__(self)

    def items(self):
        self._load()
        return dict.items(self)


CLI_NAMES = _Labels()
# Todas as sessões viram abas de uma única janela do Windows Terminal com este nome (criada se não existir).
TERMINAL_WINDOW = APP_ID
BOOT_RETRY_DELAYS = (1, 2, 4, 8, 15)  # gateway lento no boot: 30 s de espera no total


def session_title(project: dict, cli: str, home: Path | None = None) -> str:
    """Título da aba. Fora do perfil padrão ganha o nome do perfil (no padrão fica igual ao de sempre)."""
    title = f"{project['name']} — {registry.get(cli).LABEL}"
    profile = profiles.label(home) if home is not None else None
    return f"{title} · {profile}" if profile else title


def tab_color(harness, home: Path | None = None) -> str:
    """Cor da aba: fora do perfil padrão, a do perfil (o título já leva o nome); no padrão, a do CLI."""
    if home is not None and profiles.label(home) is not None:
        color = profiles.profile_of_home(home, profiles.base_of(home)).get("color")
        if color:
            return color
    return harness.TAB_COLOR


def console_python() -> Path:
    """Python de console para a janela da sessão. Dentro da venv, o python.exe dela é só um redirecionador que abre o
    Python base como filho e fica vivo a sessão inteira (~4 MB a mais). A janela só usa a biblioteca padrão (testado:
    sem PySide6), então roda direto no Python base."""
    executable = Path(sys.executable)
    base = Path(getattr(sys, "_base_executable", "") or "")
    if sys.prefix != sys.base_prefix and base.is_file():
        executable = base
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if not executable.is_file():
        raise LauncherError("Python de console não encontrado nesta instalação.")
    return executable


def terminal_tab(terminal_exe: str, title: str, color: str, window_command: list[str],
                 window: str = TERMINAL_WINDOW) -> list[str]:
    """wt.exe abrindo a sessão numa aba da janela única. --suppressApplicationTitle impede o CLI de trocar o título.

    O primeiro processo da aba é um cmd.exe que roda a janela e sai com 0: o Terminal só fecha a aba sozinho com o
    primeiro processo saindo com 0, depois que o último processo do console sai. Assim a aba fecha no fim da sessão
    mesmo com o desktop.py encerrado por fora (taskkill /IM python.exe de outra sessão) e o CLI ainda vivo. O "call"
    impede o cmd de tirar as aspas do primeiro argumento quando o caminho do Python tem espaço."""
    return [terminal_exe, "-w", window, "new-tab", "--title", title.replace(";", " "), "--suppressApplicationTitle",
            "--tabColor", color, "cmd.exe", "/d", "/c", "call", *window_command, "&", "exit", "0"]


def start_session(store: Store, project: dict, cli: str, model: str, resume: bool = False,
                  bypass_permissions: bool = False, controlled: bool = False,
                  connection: str | None = None, background: bool | None = None,
                  minimized: bool | None = None, conversation: str | None = None) -> subprocess.Popen | None:
    """Abre a janela da sessão. background (padrão: só nas sessões controladas) abre sem tirar o foco do teclado de
    quem está na frente e, com minimized (padrão: o mesmo que background), minimizada, sem cobrir nada na tela (o
    usuário a abre pela barra de tarefas); com a preferência sessions_secondary_monitor, a janela vai para outro
    monitor, se houver."""
    if os.name != "nt":
        raise LauncherError("A abertura de janelas de sessão requer Windows.")
    harness = registry.get(cli)
    if harness.KIND != "terminal":
        raise LauncherError(f"O {harness.LABEL} não abre em janela de terminal; use a interface.")
    from agent_launcher.harnesses import cli_update  # fora do topo: a janela da sessão tem orçamento de imports
    if cli_update.updating(cli):
        raise LauncherBusy(f"O {harness.LABEL} está sendo atualizado agora; abra a sessão quando terminar.")
    try:  # sessões abertas por outro programa (sem a interface) também mantêm a instalação atualizada
        updater.check_in_background(ROOT, store.preference("auto_update"))
    except OSError as error:
        print(f"Aviso: verificação de atualização não iniciou ({error}).", file=sys.stderr)
    directory = project_path(project["path"])
    gateway = Gateway.for_project(store, project, connection)
    build_plan(gateway, cli, str(directory), model, resume, gateway.catalog(offline=True),
               bypass_permissions=bypass_permissions, controlled=controlled, conversation=conversation)
    command = [str(console_python()), str(ROOT / "desktop.py"), "--home", str(store.home.resolve()),
               "--project", project["id"], "--connection", gateway.connection["id"], "--cli", cli, "--model", model]
    if resume:
        command.append("--resume")
    if resume and conversation:
        command += ["--conversation", conversation]
    if bypass_permissions:
        command.append("--bypass")
    if controlled:
        command.append("--controlled")
    background = controlled if background is None else background
    minimized = background if minimized is None else minimized
    environment = without_parent_session(dict(os.environ))
    title = session_title(project, cli, store.home)
    terminal_exe = terminal.find(ROOT)

    def open_window() -> subprocess.Popen | None:
        if terminal_exe:
            # wt.exe apenas encaminha a abertura e termina antes da sessão, por isso não há processo para acompanhar.
            try:
                subprocess.Popen(terminal_tab(terminal_exe, title, tab_color(harness, store.home), command),
                                 cwd=directory, env=environment, shell=False)
                return None
            except OSError as error:  # ex.: AppLocker barrando o Terminal portátil
                print(f"Aviso: Windows Terminal não abriu ({error}); usando um console separado.", file=sys.stderr)
        return subprocess.Popen(command, cwd=directory, env=environment, shell=False,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)

    if not background:
        return open_window()
    # O Windows Terminal ativa a janela ao abrir a aba (não há opção para não ativar): o foco volta logo em seguida.
    report = focus.keep_behind(open_window, title.replace(";", " "), store.preference("sessions_secondary_monitor"),
                               minimized=minimized)
    if not report["window"]:
        print(f"Aviso: a janela \"{title}\" não apareceu a tempo; ela pode ter tomado o foco.", file=sys.stderr)
    return report["result"]


def announce_wait(error: Exception, delay: float, attempt: int, attempts: int) -> None:
    print(f"  O host ainda não respondeu (tentativa {attempt} de {attempts}); nova tentativa em {delay:g} s.")


def run_session(home: Path, reference: str, cli: str, model: str, resume: bool,
                bypass_permissions: bool = False, controlled: bool = False, connection: str | None = None,
                conversation: str | None = None) -> int:
    store = Store(home)
    project = store.find(reference)
    title = session_title(project, cli, store.home)
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    keys.register(os.getpid(), project["name"], project["id"], cli, title, controlled)  # para `launcher keys`
    try:
        print(f"\n  {title}\n  {project['path']}\n")
        gateway = Gateway.for_project(store, project, connection)
        harness = registry.get(cli)
        print(f"  Conexão: {gateway.name} • {harness.connection_url(gateway) or gateway.url}\n")
        quota = gateway.verify(tolerate_quota=True, retry_delays=BOOT_RETRY_DELAYS, on_wait=announce_wait)
        if quota:
            print(f"  Aviso: {quota}\n")
        plan = build_plan(gateway, cli, project["path"], model, resume, gateway.catalog(), preserve_terminal_title=True,
                          bypass_permissions=bypass_permissions, controlled=controlled,
                          admin_note=elevated.session_note(project), conversation=conversation)
        for warning in plan.warnings:
            print(f"  Aviso: {warning}")
        record_session(store.home, project, gateway, plan)  # registro do perfil
        harness.prepare(plan)
        return harness.run(plan, project)
    finally:
        keys.unregister(os.getpid())
        if os.name == "nt":
            ctypes.windll.kernel32.SetConsoleTitleW(title)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Janela de sessão do {APP_NAME}")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--connection", help="Nome ou ID; omitido usa a última do projeto ou a padrão")
    parser.add_argument("--cli", choices=tuple(CLI_NAMES), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--conversation", help="ID da conversa a retomar (com --resume); omitido retoma a última")
    parser.add_argument("--bypass", action="store_true", help="Ignora permissões; no Codex, também desativa o sandbox.")
    parser.add_argument("--controlled", action="store_true",
                        help="Sessão controlada por orquestrador (implica --bypass; Codex com app-server + TUI --remote).")
    options = parser.parse_args()
    try:
        result = run_session(options.home, options.project, options.cli, options.model, options.resume, options.bypass,
                             options.controlled, options.connection, options.conversation)
    except (LauncherError, OSError, ValueError) as error:
        print(f"\n  Não foi possível iniciar a sessão: {error}")
        result = 1
    except KeyboardInterrupt:
        result = 130
    if consume_close_request(os.getpid()):
        return 0  # fechamento pedido pelo orquestrador: código 0 faz o Windows Terminal fechar a aba
    if result not in (0, 130):
        try:
            return wait_to_close(result, os.getpid())
        except KeyboardInterrupt:
            pass
    return result


ENABLE_PROCESSED_INPUT = 0x0001
KEY_POLL_S = 0.1


def _console_key() -> str | None:
    """Tecla digitada no console, ou None depois de KEY_POLL_S sem tecla."""
    import msvcrt
    import time
    if msvcrt.kbhit():
        return msvcrt.getwch()
    time.sleep(KEY_POLL_S)
    return None


def wait_to_close(result: int, pid: int, read_key=None) -> int:
    """Segura a janela que não iniciou a sessão até ENTER ou Ctrl+C (devolve result) ou até o pedido de fechamento do
    orquestrador chegar (devolve 0), o que vier primeiro: o pedido pode chegar durante a espera.

    Ctrl+C chega como tecla, com a entrada processada do console desligada: como sinal, ele iria também ao cmd.exe pai
    da aba, que largaria o "& exit 0" e deixaria a aba aberta. read_key (testes) devolve a tecla ou None; sem ele, a
    tecla vem do console, e sem console não há quem tecle ENTER."""
    console = mode = None
    if read_key is None:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        console = kernel32.CreateFileW("CONIN$", 0xC0000000, 0x3, None, 3, 0, None)  # leitura e escrita, OPEN_EXISTING
        if console in (None, wintypes.HANDLE(-1).value):
            return result
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(console, ctypes.byref(mode)):
            kernel32.CloseHandle(console)
            return result
        if not kernel32.SetConsoleMode(console, mode.value & ~ENABLE_PROCESSED_INPUT):
            print(f"  Aviso: Ctrl+C continua como sinal ({ctypes.get_last_error()}); feche com ENTER.")
        read_key = _console_key
    print("\nPressione ENTER para fechar esta janela...", flush=True)
    try:
        while not consume_close_request(pid):
            if read_key() in ("\r", "\n", "\x03"):
                return result
        return 0  # fechamento pedido pelo orquestrador: código 0 faz o Windows Terminal fechar a aba
    finally:
        if console is not None:
            kernel32.SetConsoleMode(console, mode.value)
            kernel32.CloseHandle(console)
