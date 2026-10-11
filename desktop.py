from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys

from core import (ROOT, Gateway, LauncherError, Store, build_plan, consume_close_request, prepare_unattended,
                  project_path,
                  run_controlled_codex, run_plan,
                  without_parent_session)
import focus
import terminal
from version import APP_ID, APP_NAME


CLI_NAMES = {"claude": "Claude Code", "codex": "Codex"}
# Todas as sessões viram abas de uma única janela do Windows Terminal com este nome (criada se não existir).
TERMINAL_WINDOW = APP_ID
TAB_COLORS = {"claude": "#D97757", "codex": "#10A37F"}


def session_title(project: dict, cli: str) -> str:
    return f"{project['name']} — {CLI_NAMES[cli]}"


def console_python() -> Path:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if not executable.is_file():
        raise LauncherError("Python de console não encontrado nesta instalação.")
    return executable


def start_session(store: Store, project: dict, cli: str, model: str, resume: bool = False,
                  bypass_permissions: bool = False, controlled: bool = False,
                  connection: str | None = None, background: bool | None = None) -> subprocess.Popen | None:
    """Abre a janela da sessão. background (padrão: só nas sessões controladas) abre sem tirar o foco do teclado de
    quem está na frente; com a preferência sessions_secondary_monitor, a janela vai para outro monitor, se houver."""
    if os.name != "nt":
        raise LauncherError("A abertura de janelas de sessão requer Windows.")
    directory = project_path(project["path"])
    gateway = Gateway.for_project(store, project, connection)
    build_plan(gateway, cli, str(directory), model, resume, gateway.catalog(offline=True),
               bypass_permissions=bypass_permissions, controlled=controlled)
    command = [str(console_python()), str(ROOT / "desktop.py"), "--home", str(store.home.resolve()),
               "--project", project["id"], "--connection", gateway.connection["id"], "--cli", cli, "--model", model]
    if resume:
        command.append("--resume")
    if bypass_permissions:
        command.append("--bypass")
    if controlled:
        command.append("--controlled")
    background = controlled if background is None else background
    environment = without_parent_session(dict(os.environ))
    title = session_title(project, cli)
    terminal_exe = terminal.find(ROOT)

    def open_window() -> subprocess.Popen | None:
        if terminal_exe:
            # Aba na janela única do launcher; --suppressApplicationTitle impede o CLI de trocar o título da aba.
            # wt.exe apenas encaminha a abertura e termina antes da sessão, por isso não há processo para acompanhar.
            try:
                subprocess.Popen([terminal_exe, "-w", TERMINAL_WINDOW, "new-tab", "--title", title.replace(";", " "),
                                  "--suppressApplicationTitle", "--tabColor", TAB_COLORS[cli], *command],
                                 cwd=directory, env=environment, shell=False)
                return None
            except OSError as error:  # ex.: AppLocker barrando o Terminal portátil
                print(f"Aviso: Windows Terminal não abriu ({error}); usando um console separado.", file=sys.stderr)
        return subprocess.Popen(command, cwd=directory, env=environment, shell=False,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)

    if not background:
        return open_window()
    # O Windows Terminal ativa a janela ao abrir a aba (não há opção para não ativar): o foco volta logo em seguida.
    report = focus.keep_behind(open_window, title.replace(";", " "), store.preference("sessions_secondary_monitor"))
    if not report["window"]:
        print(f"Aviso: a janela \"{title}\" não apareceu a tempo; ela pode ter tomado o foco.", file=sys.stderr)
    return report["result"]


def run_session(home: Path, reference: str, cli: str, model: str, resume: bool,
                bypass_permissions: bool = False, controlled: bool = False, connection: str | None = None) -> int:
    store = Store(home)
    project = store.find(reference)
    title = session_title(project, cli)
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    try:
        print(f"\n  {title}\n  {project['path']}\n")
        gateway = Gateway.for_project(store, project, connection)
        print(f"  Conexão: {gateway.name} • {gateway.targets.get(cli) or gateway.url}\n")
        gateway.verify()
        plan = build_plan(gateway, cli, project["path"], model, resume, gateway.catalog(), preserve_terminal_title=True,
                          bypass_permissions=bypass_permissions, controlled=controlled)
        for warning in plan.warnings:
            print(f"  Aviso: {warning}")
        prepare_unattended(plan)
        if plan.controlled and cli == "codex":
            return run_controlled_codex(plan, project["name"])
        return run_plan(plan)
    finally:
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
    parser.add_argument("--bypass", action="store_true", help="Ignora permissões; no Codex, também desativa o sandbox.")
    parser.add_argument("--controlled", action="store_true",
                        help="Sessão controlada por orquestrador (implica --bypass; Codex com app-server + TUI --remote).")
    options = parser.parse_args()
    try:
        result = run_session(options.home, options.project, options.cli, options.model, options.resume, options.bypass,
                             options.controlled, options.connection)
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


if __name__ == "__main__":
    raise SystemExit(main())
