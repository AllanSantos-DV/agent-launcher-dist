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
from agent_launcher.domain.errors import LauncherError
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
        raise LauncherError(f"O {harness.LABEL} está sendo atualizado agora; abra a sessão quando terminar.")
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
            # Aba na janela única do launcher; --suppressApplicationTitle impede o CLI de trocar o título da aba.
            # wt.exe apenas encaminha a abertura e termina antes da sessão, por isso não há processo para acompanhar.
            try:
                subprocess.Popen([terminal_exe, "-w", TERMINAL_WINDOW, "new-tab", "--title", title.replace(";", " "),
                                  "--suppressApplicationTitle", "--tabColor", tab_color(harness, store.home), *command],
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
        gateway.verify()
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
            input("\nPressione ENTER para fechar esta janela...")
        except (EOFError, KeyboardInterrupt):
            pass
    return result
