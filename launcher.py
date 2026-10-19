from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from catalog import CLAUDE_FAMILIES, CLIS, base_model, token_label
from core import (ROOT, LauncherError, Gateway, Store, build_plan, controlled_codex_commands, prepare_unattended,
                  resolve_cli,
                  run_controlled_codex, run_plan)
import clis
import elevated
import focus
import keys
import terminal
import updater
from version import APP_NAME, VERSION

CLI_LABELS = {"claude": "Claude Code", "codex": "Codex"}


def heading(title: str) -> None:
    print(f"\n{'─' * 64}\n  {title}\n{'─' * 64}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def number_choice(value: str, length: int) -> int | None:
    if value.isdecimal() and 1 <= int(value) <= length:
        return int(value) - 1
    return None


def show_projects(store: Store) -> None:
    heading("PROJETOS")
    for index, project in enumerate(store.projects, start=1):
        marker = " • recente" if project["id"] == store.data["last_project"] else ""
        missing = " • pasta indisponível" if not Path(project["path"]).is_dir() else ""
        print(f"  {index:>2}. {project['name']}{marker}{missing}\n      {project['path']}")
    if not store.projects:
        print("  Nenhum projeto cadastrado. Você pode adicionar uma pasta existente.")


def show_connections(store: Store) -> None:
    heading("CONEXÕES")
    modules, errors = store.connectors()
    for index, connection in enumerate(store.connections, start=1):
        module = modules.get(connection["connector"])
        marker = " • padrão" if connection["id"] == store.data["default_connection"] else ""
        label = module.LABEL if module else f"{connection['connector']} (conector indisponível)"
        print(f"  {index:>2}. {connection['name']}{marker}\n      {label} • {connection['options'].get('base_url', '')}")
    if not store.connections:
        print("  Nenhuma conexão. Adicione um provedor (LiteLLM, 9router, OmniRoute, OpenRouter, Anthropic, OpenAI...).")
    for error in errors:
        print(f"  [AVISO] conector do usuário não carregado: {error}")


def add_project(store: Store) -> dict | None:
    path = ask("Pasta do projeto (ENTER cancela)").strip('"')
    if not path:
        return None
    name = ask("Nome (ENTER usa o nome da pasta)")
    project = store.add(path, name or None)
    print(f"Projeto disponível: {project['name']}")
    return project


def choose_project(store: Store) -> dict | None:
    while True:
        show_projects(store)
        print("\n  a. Adicionar pasta    0. Voltar")
        value = ask("Projeto", "1" if store.projects else "a")
        if value == "0":
            return None
        if value.lower() == "a":
            return add_project(store)
        selected = number_choice(value, len(store.projects))
        if selected is not None:
            return store.projects[selected]
        print("Escolha um número da lista.")


def choose_connection(store: Store, project: dict | None = None) -> dict | None:
    try:
        default = store.connection_for(project)
    except LauncherError:
        default = None
    if len(store.connections) == 1 and default:
        return default
    while True:
        show_connections(store)
        print("\n  a. Adicionar conexão    0. Voltar")
        suggestion = str(store.connections.index(default) + 1) if default else ("1" if store.connections else "a")
        value = ask("Conexão", suggestion)
        if value == "0":
            return None
        if value.lower() == "a":
            return add_connection_interactive(store)
        selected = number_choice(value, len(store.connections))
        if selected is not None:
            return store.connections[selected]
        print("Escolha um número da lista.")


def choose_cli(gateway: Gateway, default: str = "claude") -> str | None:
    options = [cli for cli in CLIS if cli in gateway.clis]
    if len(options) == 1:
        print(f"  {gateway.name} atende somente o {CLI_LABELS[options[0]]}.")
        return options[0]
    while True:
        print("\n  1. Claude Code    2. Codex    0. Voltar")
        value = ask("Ferramenta", "2" if default == "codex" else "1")
        if value in ("0", "1", "2"):
            return {"0": None, "1": "claude", "2": "codex"}[value]
        print("Escolha 1 ou 2; 0 volta ao menu.")


def field_value(item: dict, current=None):
    label = item["label"] + ("" if item.get("required") else " (opcional)")
    if item.get("hint"):
        print(f"    {item['hint']}")
    if item.get("secret"):
        value = getpass.getpass(f"{label} (oculto; ENTER mantém o atual): " if current else f"{label} (oculto): ")
        return value if value or not current else None
    if item.get("type") == "bool":
        default = "s" if (current if current is not None else item.get("default")) else "n"
        return ask(f"{label} (s/n)", default).lower().startswith("s")
    return ask(label, str(current if current is not None else item.get("default", "")))


def add_connection_interactive(store: Store) -> dict | None:
    modules, _errors = store.connectors()
    names = list(modules)
    heading("NOVA CONEXÃO")
    for index, name in enumerate(names, start=1):
        print(f"  {index:>2}. {modules[name].LABEL} — {getattr(modules[name], 'HINT', '')}")
    selected = number_choice(ask("Tipo (0 cancela)", "0"), len(names))
    if selected is None:
        return None
    module = modules[names[selected]]
    title = ask("Nome da conexão", module.LABEL)
    values = {item["name"]: field_value(item) for item in module.FIELDS}
    print("Testando a conexão…")
    connection = store.save_connection(title, module.NAME, values)
    print(f"Conexão salva: {connection['name']}")
    return connection


def choose_model(gateway: Gateway, cli: str, previous: str = "") -> str | None:
    try:
        catalog = gateway.catalog()
    except LauncherError as error:
        catalog = gateway.empty_catalog(status=str(error))
    query = ""
    page = 0
    while True:
        models = [record for record in catalog.for_cli(cli) if query.casefold() in record.id.casefold()]
        total_pages = max(1, (len(models) + 11) // 12)
        page = min(page, total_pages - 1)
        visible = models[page * 12:(page + 1) * 12]
        heading(f"MODELOS / {gateway.name} / {cli.upper()} • {len(models)} resultados • página {page + 1}/{total_pages}")
        print(f"  {catalog.status}")
        if previous:
            print(f"\n  ENTER: usar último modelo → {previous}")
        if query:
            print(f"  Busca: {query}")
        for index, record in enumerate(visible, start=1):
            print(f"  {index:>2}. {catalog.describe(record)}")
        if not visible:
            print("  Nenhum modelo nesta busca. Use /texto, r ou m.")
        print("\n  /texto buscar  / limpar  n/p páginas  r atualizar  m ID manual  0 voltar")
        value = ask("Modelo")
        if not value and previous:
            return previous
        if value == "0":
            return None
        if value.startswith("/"):
            query, page = value[1:], 0
        elif value.lower() == "n":
            page = min(page + 1, total_pages - 1)
        elif value.lower() == "p":
            page = max(page - 1, 0)
        elif value.lower() == "r":
            try:
                catalog = gateway.catalog(refresh=True)
            except LauncherError as error:
                catalog.verified = False
                catalog.status = str(error) + " • lista anterior sem confirmação; 1M desativado"
        elif value.lower() == "m":
            manual = ask("ID completo do modelo (ENTER cancela)")
            if manual:
                print("A disponibilidade do ID manual será validada pelo host ao usar a sessão.")
                return manual
        else:
            selected = number_choice(value, len(visible))
            if selected is not None:
                return visible[selected].id
            print("Seleção inválida.")


def launch(store: Store, project: dict, gateway: Gateway, cli: str, model: str, resume: bool = False,
           dry_run: bool = False, controlled: bool = False) -> int:
    if not dry_run:
        gateway.verify()
    try:
        catalog = gateway.catalog(offline=dry_run)
    except LauncherError as error:
        catalog = gateway.empty_catalog(status=str(error))
    plan = build_plan(gateway, cli, project["path"], model, resume, catalog, controlled=controlled)
    if dry_run:
        controlled_commands = dict(zip(("server", "tui"), controlled_codex_commands(plan, "ws://127.0.0.1:PORTA"))) \
            if plan.controlled and cli == "codex" else None
        hidden = {gateway.token} - {""}
        command = ["<chave>" if part in hidden else part for part in plan.command]
        print(json.dumps({"cli": cli, "project": str(plan.directory), "model": plan.model,
                          "connection": gateway.name, "connector": gateway.module.NAME, "targets": gateway.targets,
                          "resume": resume, "command": command,
                          "controlled": plan.controlled, "controlled_codex": controlled_commands,
                          "environment_overrides": plan.public_overrides, "catalog_status": catalog.status,
                          "input_limit": plan.input_limit, "context_window": plan.context_window,
                          "auto_compact_token_limit": plan.auto_compact_limit,
                          "claude_families": plan.families, "warnings": plan.warnings}, ensure_ascii=False, indent=2))
        return 0
    heading(f"{'RETOMAR' if resume else 'INICIAR'} / {cli.upper()}")
    print(f"  Projeto: {project['name']}\n  Pasta:   {plan.directory}\n  Modelo:  {plan.model}")
    print(f"  Conexão: {gateway.name} • {gateway.targets[cli]}")
    print(f"  Catálogo: {catalog.status}\n  Entrada informada: {token_label(plan.input_limit)}")
    if plan.context_window is not None:
        print(f"  Contexto configurado: {token_label(plan.context_window)}")
    if plan.auto_compact_limit is not None:
        print(f"  Compactação nativa: até {token_label(plan.auto_compact_limit)} tokens de contexto")
    for family, family_model in plan.families.items():
        print(f"  {family.title():7}: {family_model}")
    for warning in plan.warnings:
        print(f"  [AVISO] {warning}")
    print("  Configurações globais dos CLIs preservadas.\n")
    store.remember(project, gateway.connection, cli, base_model(model))
    prepare_unattended(plan)
    exit_code = run_controlled_codex(plan, project["name"]) if plan.controlled and cli == "codex" else run_plan(plan)
    if exit_code:
        print(f"\n{cli} encerrou com código {exit_code}. Use 'doctor' para verificar a conexão e o catálogo.")
    return exit_code


def doctor(store: Store, reference: str | None = None) -> int:
    heading("DIAGNÓSTICO / SOMENTE LEITURA")
    print(f"  {APP_NAME} {VERSION}\n  Dados: {store.home}")
    failed = False
    installed = 0
    for cli in CLIS:
        try:
            command = resolve_cli(cli)
            installed += 1
            print(f"  [OK] {cli}: {command[-1]}")
        except LauncherError as error:
            print(f"  [AVISO] {error}")
    if not installed:
        failed = True
    wt = terminal.find(ROOT)
    print(f"  [OK] Windows Terminal: {wt}" if wt else
          "  [AVISO] Windows Terminal ausente: sessões em consoles separados. Use 'terminal --install'.")
    connections = [store.find_connection(reference)] if reference else store.connections
    if not connections:
        failed = True
        print("  [ERRO] Nenhuma conexão cadastrada. Use 'connections add'.")
    for connection in connections:
        try:
            gateway = Gateway(store, connection)
            catalog = gateway.catalog(refresh=True, save_cache=False)
            print(f"  [OK] {gateway.name} ({gateway.module.LABEL}) • {gateway.url}")
            for cli in sorted(gateway.clis):
                records = catalog.for_cli(cli)
                large = sum(record.one_million for record in records) if catalog.verified else 0
                print(f"       {CLI_LABELS[cli]}: {len(records)} modelos; {large} com entrada >= 1M informada")
            if not catalog.verified:
                print(f"       [AVISO] {catalog.status}")
        except LauncherError as error:
            failed = True
            print(f"  [ERRO] {connection['name']}: {error}")
    print("\n  O diagnóstico só lista modelos: não gera tokens. Catálogo acessível não garante inferência.")
    return 1 if failed else 0


def import_projects(store: Store, source: Path) -> None:
    added, skipped = store.import_legacy(source)
    print(f"{added} projeto(s) importado(s); {skipped} duplicado(s), inválido(s) ou pasta(s) indisponível(is).")
    print("Somente nomes e caminhos foram aproveitados. Nenhuma chave, token ou configuração antiga foi copiada.")


def manage_projects(store: Store) -> None:
    show_projects(store)
    print("\n  a. Adicionar    e. Editar    r. Remover cadastro    0. Voltar")
    value = ask("Ação", "0").lower()
    if value == "a":
        add_project(store)
    elif value == "e":
        selected = number_choice(ask("Número do projeto"), len(store.projects))
        if selected is None:
            raise LauncherError("Projeto inválido.")
        project = store.projects[selected]
        name = ask("Nome", project["name"])
        path = ask("Pasta", project["path"]).strip('"')
        project = store.update(project["id"], name, path)
        print(f"Projeto atualizado: {project['name']}\n      {project['path']}")
    elif value == "r":
        selected = number_choice(ask("Número do projeto"), len(store.projects))
        if selected is None:
            raise LauncherError("Projeto inválido.")
        project = store.projects[selected]
        if ask(f"Remover '{project['name']}' da lista? A pasta será preservada (s/N)", "n").lower() == "s":
            store.remove(project["id"])


def manage_connections(store: Store) -> None:
    show_connections(store)
    print("\n  a. Adicionar    t. Testar    p. Tornar padrão    r. Remover    0. Voltar")
    value = ask("Ação", "0").lower()
    if value == "a":
        add_connection_interactive(store)
    elif value in ("t", "p", "r"):
        selected = number_choice(ask("Número da conexão"), len(store.connections))
        if selected is None:
            raise LauncherError("Conexão inválida.")
        connection = store.connections[selected]
        if value == "t":
            Gateway(store, connection).verify()
            print(f"{connection['name']}: conexão respondeu.")
        elif value == "p":
            store.set_default_connection(connection["id"])
        elif ask(f"Remover a conexão '{connection['name']}' e sua chave salva? (s/N)", "n").lower() == "s":
            store.remove_connection(connection["id"])


def menu(store: Store) -> int:
    while True:
        heading(f"{APP_NAME.upper()} {VERSION}")
        print("  Claude Code + Codex em qualquer provedor compatível.")
        recent = next((project for project in store.projects if project["id"] == store.data["last_project"]), None)
        if recent:
            print(f"\n  ENTER: {recent['name']} / {recent['last_cli']} / último modelo")
        print("\n  1. Nova sessão        2. Retomar última sessão do projeto\n  3. Projetos           4. Catálogo de modelos\n"
              "  5. Diagnóstico        6. Importar projetos\n  7. Conexões           0. Sair")
        try:
            value = ask("Ação", "enter" if recent else ("1" if store.connections else "7"))
            if value == "0":
                return 0
            if value == "enter" and recent:
                gateway = Gateway.for_project(store, recent)
                model = store.last_model(recent, gateway.connection, recent["last_cli"])
                if not model:
                    raise LauncherError("Escolha um modelo em 'Nova sessão' antes de usar o atalho.")
                launch(store, recent, gateway, recent["last_cli"], model)
            elif value in ("1", "2"):
                project = choose_project(store)
                if not project:
                    continue
                connection = choose_connection(store, project)
                if not connection:
                    continue
                gateway = Gateway(store, connection)
                cli = choose_cli(gateway, project["last_cli"])
                if not cli:
                    continue
                resolve_cli(cli)
                model = choose_model(gateway, cli, store.last_model(project, connection, cli))
                if model:
                    launch(store, project, gateway, cli, model, resume=value == "2")
            elif value == "3":
                manage_projects(store)
            elif value == "4":
                connection = choose_connection(store)
                if connection:
                    gateway = Gateway(store, connection)
                    cli = choose_cli(gateway)
                    if cli:
                        chosen = choose_model(gateway, cli)
                        if chosen:
                            print(f"Modelo selecionado: {chosen} (nenhuma sessão iniciada)")
            elif value == "5":
                doctor(store)
            elif value == "6":
                source = ask("Caminho do config.json com os projetos (0 cancela)", "0").strip('"')
                if source != "0":
                    import_projects(store, Path(source))
            elif value == "7":
                manage_connections(store)
            else:
                print("Escolha uma opção do menu.")
        except (LauncherError, OSError) as error:
            print(f"\nNão foi possível concluir: {error}")


def parse_values(module, pairs: list[str], secret_env: list[str]) -> dict:
    """--set campo=valor para campos comuns; --secret-env campo=VARIAVEL lê o segredo do ambiente (fora do histórico)."""
    fields = {item["name"]: item for item in module.FIELDS}
    values = {}
    for pair, secret in [(pair, False) for pair in pairs] + [(pair, True) for pair in secret_env]:
        key, separator, value = pair.partition("=")
        if not separator or key not in fields:
            raise LauncherError(f"Campo inválido para {module.NAME}: {pair}. Campos: {', '.join(fields)}")
        if bool(fields[key].get("secret")) != secret:
            raise LauncherError(f"{key}: use {'--secret-env' if fields[key].get('secret') else '--set'} para este campo.")
        if secret:
            if value not in os.environ:
                raise LauncherError(f"Variável de ambiente {value} não definida.")
            values[key] = os.environ[value]
        elif fields[key].get("type") == "bool":
            values[key] = value.strip().lower() in ("1", "true", "s", "sim", "yes", "on")
        else:
            values[key] = value
    for key, item in fields.items():
        if item.get("secret") and item.get("required") and key not in values and sys.stdin.isatty():
            values[key] = getpass.getpass(f"{item['label']} (oculto): ")
    return values


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=f"{APP_NAME}: Claude Code e Codex em provedores compatíveis.")
    result.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    commands = result.add_subparsers(dest="action")
    check = commands.add_parser("doctor", help="Verificar CLIs e conexões (somente leitura)")
    check.add_argument("--connection")
    commands.add_parser("connectors", help="Listar os tipos de conector e seus campos")
    connections = commands.add_parser("connections", help="Gerenciar conexões com provedores")
    connection_actions = connections.add_subparsers(dest="connection_action")
    add_connection = connection_actions.add_parser("add", help="Adicionar ou atualizar uma conexão (testa antes de salvar)")
    add_connection.add_argument("--name", required=True)
    add_connection.add_argument("--connector", required=True)
    add_connection.add_argument("--set", action="append", default=[], metavar="CAMPO=VALOR")
    add_connection.add_argument("--secret-env", action="append", default=[], metavar="CAMPO=VARIAVEL")
    for name, help_text in (("test", "Testar a conexão"), ("remove", "Remover a conexão e sua chave"),
                            ("default", "Tornar a conexão padrão")):
        connection_actions.add_parser(name, help=help_text).add_argument("reference")
    models = commands.add_parser("models", help="Listar IDs completos de modelos de uma conexão")
    models.add_argument("--connection")
    models.add_argument("--cli", choices=CLIS)
    models.add_argument("--search", default="", help="Filtrar por trecho do ID")
    models.add_argument("--family", choices=CLAUDE_FAMILIES, help="Filtrar uma família Claude")
    models.add_argument("--refresh", action="store_true", help="Ignorar cache e exigir catálogo ao vivo")
    projects = commands.add_parser("projects", help="Gerenciar os projetos cadastrados")
    actions = projects.add_subparsers(dest="project_action")
    add = actions.add_parser("add", help="Cadastrar uma pasta existente")
    add.add_argument("path")
    add.add_argument("--name")
    edit = actions.add_parser("edit", help="Renomear o projeto ou apontar para outra pasta")
    edit.add_argument("reference", help="Nome, ID ou caminho")
    edit.add_argument("--name")
    edit.add_argument("--path")
    remove = actions.add_parser("remove", help="Remover cadastro, sem excluir a pasta")
    remove.add_argument("reference", help="Nome, ID ou caminho")
    legacy = actions.add_parser("import", help="Importar nomes e caminhos de outro config.json")
    legacy.add_argument("source", type=Path)
    cli_command = commands.add_parser("cli", help="Claude Code e Codex: situação e instalação por usuário, sem admin")
    cli_command.add_argument("--install", choices=(*CLIS, "all"), help="instala o CLI que falta (instalador oficial ou npm)")
    term = commands.add_parser("terminal", help="Windows Terminal: situação, bloqueios corporativos e instalação sem admin")
    term.add_argument("--install", action="store_true", help="instala o Terminal portátil (ou via winget) se faltar")
    term.add_argument("--zip", type=Path, help="ZIP do Windows Terminal entregue pela TI; força a cópia portátil")
    admin = commands.add_parser("elevated", help="Projetos que abrem sessões elevadas (administrador) sem pedido de UAC")
    admin_actions = admin.add_subparsers(dest="elevated_action")
    admin_actions.add_parser("status", help="Tarefa, projetos marcados e pendências")
    for name, text in (("enable", "Marca projetos como elevados (um clique de UAC, uma vez)"),
                       ("disable", "Desmarca projetos (um clique de UAC)")):
        marker = admin_actions.add_parser(name, help=text)
        marker.add_argument("projects", nargs="+", metavar="PROJETO", help="Nome ou ID de projeto cadastrado")
    admin_actions.add_parser("probe", help="Prova, sem janela, que a tarefa roda com token elevado e sem UAC")
    runner = admin_actions.add_parser("run", help="Executa um comando elevado (sem UAC) na pasta de um projeto marcado")
    runner.add_argument("--project", help="Nome, ID ou caminho do projeto; omitido usa a pasta atual")
    runner.add_argument("--cmd", action="store_true", help="interpretar como cmd em vez de PowerShell")
    runner.add_argument("--timeout", type=int, default=600, help="segundos (padrão 600, máximo 3600)")
    runner.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMANDO")
    preferences = commands.add_parser("preferences", help="Preferências")
    preferences.add_argument("--bypass-default", choices=("on", "off"),
                             help="Bypass de permissões marcado por padrão na interface")
    preferences.add_argument("--auto-update", choices=("on", "off"),
                             help="Interface se atualiza sozinha pelo npm a cada abertura")
    preferences.add_argument("--sessions-monitor", choices=("primary", "other"),
                             help="Sessões em segundo plano no outro monitor (o que não tem a janela sempre por cima), "
                                  "se existir")
    top = commands.add_parser("topmost", help="Janela sempre por cima (ex.: a tela do orquestrador)")
    top.add_argument("state", choices=("on", "off", "resume", "status"),
                     help="on marca e mantém; off desmarca; resume encerra um 'front' em andamento")
    top.add_argument("--title", help="Título exato da janela (obrigatório com on)")
    front = commands.add_parser("front", help="Abrir por cima, a pedido explícito do usuário (arquivo, pasta ou URL): "
                                              "a janela sempre por cima dá lugar até o usuário voltar a ela, trocar de "
                                              "janela, o tempo acabar ou apertar Ctrl+Alt+J")
    front.add_argument("target")
    front.add_argument("--seconds", type=float, default=focus.FRONT_SECONDS,
                       help=f"tempo máximo por cima (padrão {focus.FRONT_SECONDS:.0f} s)")
    send = commands.add_parser("keys", help="Enviar teclas a uma sessão aberta (responder ou dispensar um diálogo)")
    send.add_argument("keys", nargs="*", metavar="TECLA",
                      help="enter esc up down left right tab shift+tab space backspace ctrl+c home end pageup pagedown "
                           "delete, um caractere (1, y) ou text:ALGO")
    send.add_argument("--project", help="Nome, ID ou caminho do projeto da sessão")
    send.add_argument("--cli", choices=CLIS, help="Desempata quando o projeto tem sessão Claude e Codex abertas")
    send.add_argument("--pid", type=int, help="pid da janela da sessão (ver --list); dispensa --project")
    send.add_argument("--list", action="store_true", help="Listar as sessões abertas (JSON) e sair")
    send.add_argument("--screen", action="store_true", help="Mostrar o que a sessão exibe agora (antes das teclas)")
    update = commands.add_parser("update", help="Atualizar agora pela última versão publicada no npm")
    update.add_argument("--check", action="store_true", help="só mostra se há versão nova, sem instalar")
    session = commands.add_parser("launch", help="Iniciar um CLI no terminal atual")
    session.add_argument("--project", required=True, help="Nome, ID ou caminho de projeto cadastrado")
    session.add_argument("--connection", help="Nome ou ID; omitido usa a última do projeto ou a padrão")
    session.add_argument("--cli", choices=CLIS)
    session.add_argument("--model", help="ID completo; omitido usa o último modelo desse CLI/projeto/conexão")
    session.add_argument("--resume", action="store_true", help="Retomar a última sessão do CLI no projeto")
    session.add_argument("--dry-run", action="store_true", help="Exibir argumentos sem rede, sem salvar e sem iniciar CLI")
    session.add_argument("--controlled", action="store_true", help="Sessão controlada por orquestrador (implica bypass)")
    return result


def main(arguments: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    options = parser().parse_args(arguments)
    try:
        store = Store()
        if store.recovered:
            print(f"Aviso: {store.recovered}", file=sys.stderr)
        if options.action != "update":
            try:
                updater.check_in_background(ROOT, store.preference("auto_update"))
            except OSError as error:
                print(f"Aviso: verificação de atualização não iniciou ({error}).", file=sys.stderr)
        if options.action is None:
            return menu(store)
        if options.action == "doctor":
            return doctor(store, options.connection)
        if options.action == "connectors":
            modules, errors = store.connectors()
            for module in modules.values():
                fields = ", ".join(item["name"] + (" (secreto)" if item.get("secret") else "") for item in module.FIELDS)
                print(f"{module.NAME:22} {module.LABEL} — {getattr(module, 'HINT', '')}\n{'':22} campos: {fields}")
            for error in errors:
                print(f"[AVISO] {error}", file=sys.stderr)
        elif options.action == "connections":
            action = options.connection_action
            if action == "add":
                modules, _errors = store.connectors()
                if options.connector not in modules:
                    raise LauncherError(f"Conector desconhecido: {options.connector}. Veja 'connectors'.")
                values = parse_values(modules[options.connector], options.set, options.secret_env)
                existing = next((c["id"] for c in store.connections if c["name"].casefold() == options.name.casefold()), None)
                connection = store.save_connection(options.name, options.connector, values, existing)
                print(f"Conexão {'atualizada' if existing else 'salva'}: {connection['name']}")
            elif action == "test":
                Gateway(store, store.find_connection(options.reference)).verify()
                print("Conexão respondeu.")
            elif action == "remove":
                store.remove_connection(options.reference)
                print("Conexão removida, com a chave salva.")
            elif action == "default":
                store.set_default_connection(options.reference)
                print("Conexão padrão definida.")
            else:
                show_connections(store)
        elif options.action == "models":
            gateway = Gateway.for_project(store, reference=options.connection)
            if options.family and options.cli == "codex":
                raise LauncherError("--family é um filtro do Claude; não se aplica ao Codex.")
            catalog = gateway.catalog(options.refresh)
            print(catalog.status)
            wanted = [options.cli] if options.cli else sorted(gateway.clis)
            for record in catalog.records:
                if (any(catalog.allows(record, cli) for cli in wanted)
                        and (not options.family or record.family == options.family)
                        and options.search.casefold() in record.id.casefold()):
                    print(catalog.describe(record))
        elif options.action == "projects":
            if options.project_action == "add":
                project = store.add(options.path, options.name)
                print(f"Projeto cadastrado: {project['name']}\n{project['path']}")
            elif options.project_action == "edit":
                if options.name is None and options.path is None:
                    raise LauncherError("Informe --name e/ou --path.")
                project = store.update(options.reference, options.name, options.path)
                print(f"Projeto atualizado: {project['name']}\n{project['path']}")
            elif options.project_action == "remove":
                store.remove(options.reference)
                print("Cadastro removido. A pasta do projeto foi preservada.")
            elif options.project_action == "import":
                import_projects(store, options.source)
            else:
                show_projects(store)
        elif options.action == "cli":
            if options.install:
                targets = CLIS if options.install == "all" else (options.install,)
                return 0 if all(clis.ensure(cli) for cli in targets) else 1
            for cli in CLIS:
                print(f"{clis.NAMES[cli]}: {clis.find(cli) or 'não instalado'}")
        elif options.action == "terminal":
            if options.install or options.zip:
                return 0 if terminal.ensure(ROOT, options.zip.resolve() if options.zip else None) else 1
            found = terminal.find(ROOT)
            print(f"Windows Terminal: {found or 'ausente (sessões em consoles separados)'}")
            for line in terminal.describe(terminal.environment()):
                print(f"  {line}")
        elif options.action == "elevated":
            if options.elevated_action in ("enable", "disable"):
                names = [store.find(reference)["name"] for reference in options.projects]
                enabling = options.elevated_action == "enable"
                outcome = elevated.change(store.home.resolve(), names if enabling else [], [] if enabling else names)
                print(json.dumps({"ok": True, "projects": outcome["projects"], "task": outcome["task"]}, ensure_ascii=False))
            elif options.elevated_action == "run":
                words = options.command[1:] if options.command[:1] == ["--"] else options.command
                if not words:
                    raise LauncherError("Informe o comando depois de --.")
                project = store.find(options.project or os.getcwd())
                outcome = elevated.run_admin(project, " ".join(words), "cmd" if options.cmd else "powershell",
                                             options.timeout, store.home.resolve())
                sys.stdout.write(outcome.get("stdout", ""))
                sys.stderr.write(outcome.get("stderr", ""))
                if not outcome.get("ok"):
                    raise LauncherError(f"Comando elevado: {outcome.get('error')}")
                return outcome["exit"]
            elif options.elevated_action == "probe":
                print(json.dumps(elevated.probe(store.home.resolve()), ensure_ascii=False))
            else:
                print(json.dumps(elevated.status(store), ensure_ascii=False))
        elif options.action == "preferences":
            if options.bypass_default:
                store.set_preference("bypass_default", options.bypass_default == "on")
            if options.auto_update:
                store.set_preference("auto_update", options.auto_update == "on")
            if options.sessions_monitor:
                store.set_preference("sessions_secondary_monitor", options.sessions_monitor == "other")
            print(f"Bypass marcado por padrão: {'sim' if store.preference('bypass_default') else 'não'}")
            print(f"Atualização automática: {'sim' if store.preference('auto_update') else 'não'}")
            print(f"Sessões em segundo plano: "
                  f"{'no outro monitor, se existir' if store.preference('sessions_secondary_monitor') else 'onde abrirem'}")
        elif options.action == "topmost":
            if options.state == "on":
                if not options.title:
                    raise LauncherError("Informe --title com o título exato da janela.")
                focus.topmost_on(options.title)
            elif options.state == "off":
                focus.topmost_off()
            elif options.state == "resume":
                focus.topmost_resume()
            state = focus.read_state()
            marked = focus.find_window(state["title"]) if state.get("title") else None
            print(json.dumps({"enabled": bool(state.get("enabled")), "title": state.get("title"),
                              "found": bool(marked), "topmost": bool(marked and focus.is_topmost(marked)),
                              "front_until": state.get("front_until")}, ensure_ascii=False))
        elif options.action == "front":
            focus.open_front(options.target, options.seconds)
            print(f"Aberto por cima: {options.target}")
        elif options.action == "keys":
            if options.list:
                print(json.dumps(keys.sessions(), ensure_ascii=False))
                return 0
            if options.pid is None and not options.project:
                raise LauncherError("Informe --project (e --cli se preciso) ou --pid; veja --list.")
            if not options.keys and not options.screen:
                raise LauncherError("Informe as teclas a enviar ou --screen.")
            try:
                ids = {store.find(options.project)["id"]} if options.project else set()
                session = keys.resolve(ids, options.cli, options.pid)
                if options.screen:
                    print(keys.read_screen(session["pid"]))
                if options.keys:
                    keys.send(session["pid"], options.keys)
                    print(json.dumps({"ok": True, "pid": session["pid"], "title": session["title"],
                                      "sent": options.keys}, ensure_ascii=False))
            except keys.KeysError as error:
                raise LauncherError(str(error)) from error
        elif options.action == "update":
            try:
                if options.check:
                    meta = updater.latest()
                    print(f"Instalada: {VERSION} · publicada no npm: {meta['version']}"
                          + (" (há versão nova)" if updater.newer(meta) else " (em dia)"))
                    return 0
                updater.update(ROOT, log=print)
            except updater.UpdateError as error:
                print(f"Atualização falhou: {error}")
                return 1
        elif options.action == "launch":
            project = store.find(options.project)
            gateway = Gateway.for_project(store, project, options.connection)
            cli = options.cli or (project["last_cli"] if project["last_cli"] in gateway.clis else sorted(gateway.clis)[0])
            model = options.model or store.last_model(project, gateway.connection, cli)
            if not model:
                raise LauncherError("Informe --model com um ID completo, ou escolha um modelo pela interface primeiro.")
            return launch(store, project, gateway, cli, model, options.resume, options.dry_run, options.controlled)
        return 0
    except (LauncherError, OSError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nLauncher encerrado.")
        return 130


def console_of_its_own() -> bool:
    """O console foi aberto só para esta execução (duplo clique, atalho): nele vale esperar ENTER antes de sumir com
    a mensagem de erro. Automação (entrada redirecionada, NUL ou sem console) e terminal já aberto não esperam."""
    if os.name != "nt":
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle, mode = kernel32.GetStdHandle(-10), ctypes.c_uint()  # STD_INPUT_HANDLE
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    processes = (ctypes.c_uint * 8)()
    return kernel32.GetConsoleProcessList(processes, 8) <= 2  # só o cmd do launcher.cmd e este python


if __name__ == "__main__":
    exit_code = main()
    if exit_code not in (0, 130) and console_of_its_own():
        try:
            input("\nPressione ENTER para fechar...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
