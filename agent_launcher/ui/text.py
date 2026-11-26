from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_launcher import ROOT
from agent_launcher.domain import profiles, usage
from agent_launcher.domain.cache import prune_old_versions
from agent_launcher.domain.conversation_usage import conversation_usage, conversations_tsv
from agent_launcher.domain.catalog import CLAUDE_FAMILIES, ModelInfo, base_model, order_models, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import project_path
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import cli_update
from agent_launcher.harnesses import installers as clis
from agent_launcher.harnesses.installers import CliInstallError
from agent_launcher.harnesses import registry
from agent_launcher.platform import elevated, focus, keys, terminal
from agent_launcher.session import activity, open_session, reopen
from agent_launcher.session.conversation_grep import grep_conversations
from agent_launcher.session.conversations import conversations_for, record_session
from agent_launcher.session.plan import quota_notice
from agent_launcher.session import worktree as worktrees
from agent_launcher.session.preview import RESUME_SHELLS, plan_command, plan_report, planned, resume_command
from agent_launcher.ui import completion
from agent_launcher.update import updater
from version import APP_NAME, VERSION


MAX_USAGE_MONTHS = 24
ADD_HERE_HELP = ("Se o projeto não está cadastrado, cadastra a pasta atual (com o nome da pasta) e segue; --project, se vier, "
                 "tem de ser o nome ou o caminho da pasta atual. Recusa raiz de disco, pasta de usuário e pasta do sistema; "
                 "projeto já cadastrado não é regravado")


def destinations() -> tuple[str, ...]:
    """IDs de todos os destinos (adaptadores), na ordem do registro."""
    return registry.ids()


def terminal_destinations() -> tuple[str, ...]:
    return tuple(harness.ID for harness in registry.terminals())


def label_of(cli: str) -> str:
    harness = registry.find(cli)
    return harness.LABEL if harness else cli


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
        is_git, branch = worktrees.current_branch(project["path"])
        shown = f" • ramo {branch}" if branch else " • cabeça solta" if is_git else ""
        print(f"  {index:>2}. {project['name']}{marker}{missing}{shown}\n      {project['path']}")
    if not store.projects:
        print("  Nenhum projeto cadastrado. Você pode adicionar uma pasta existente.")


def project_branch(path: str) -> dict:
    """`{"branch": ramo}` quando a pasta é um repositório git (`None` na cabeça solta); `{}` quando não é (sem a chave)."""
    is_git, branch = worktrees.current_branch(path)
    return {"branch": branch} if is_git else {}


def projects_json(store: Store) -> str:
    """Projetos na ordem da interface (fixados primeiro, depois o uso mais recente), em JSON estável para script. `branch`,
    no fim, só nas pastas que são repositório git (ver project_branch)."""
    pinned = store.pinned_ids()
    return json.dumps([{"id": project["id"], "name": project["name"], "path": project["path"],
                        "pinned": project["id"] in pinned, "last_used": project["last_used"],
                        "folder_available": Path(project["path"]).is_dir(), **project_branch(project["path"]),
                        **open_session.remembered(store, project)}
                       for project in store.projects],
                      ensure_ascii=False, indent=2)


def open_project_command(store: Store, options) -> None:
    """`launcher projects open REF --folder|--vscode`: a pasta do projeto no Explorer ou no VS Code do usuário (o perfil
    dele), como o menu do projeto na interface; nenhum console aparece."""
    if options.folder and options.vscode:
        raise LauncherError("Use --folder ou --vscode, não os dois.")
    if not (options.folder or options.vscode):
        raise LauncherError("Informe --folder (abre no Explorer) ou --vscode (abre no VS Code).")
    folder = project_path(store.find(options.reference)["path"])
    if options.folder:
        try:
            os.startfile(folder)
        except OSError as error:
            raise LauncherError(f"Não foi possível abrir a pasta no Explorer: {error}") from error
        print(f"Pasta aberta no Explorer: {folder}")
        return
    from agent_launcher.harnesses import vscode
    code = vscode.find_code()
    if not code:
        raise LauncherError("O VS Code não está instalado. Instale com 'launcher cli --install vscode' (sem admin) e "
                            "repita o comando.")
    try:
        vscode.start_code(code, folder)
    except OSError as error:
        raise LauncherError(f"Não foi possível abrir no VS Code: {error}") from error
    print(f"Pasta aberta no VS Code: {folder}")


def discover_projects_command(store: Store, options) -> None:
    """`launcher projects discover [--add] [--cli CLI]`: as pastas do histórico de conversas em JSON, as já cadastradas
    marcadas; com `--add`, cadastra as cadastráveis (sem ele, só leitura)."""
    from agent_launcher.session import discovery
    rows = discovery.discover(store, options.cli)
    if options.add:
        discovery.register(store, rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def resolve_project(store: Store, options) -> tuple[dict, bool]:
    """O projeto de `--project` (sem ele, o da pasta atual) e se ele acabou de ser criado por `--add-here`. Com a flag,
    a pasta atual vira projeto só na memória quando o projeto ainda não existe; quem chama grava com `register_added`
    depois que o pedido inteiro passou nas validações. Sem a flag, projeto desconhecido é o erro de sempre."""
    if not options.add_here:
        return store.find(options.project or os.getcwd()), False
    return store.find_or_add_here(options.project, os.getcwd(), save=False)


def register_added(store: Store, project: dict, added: bool, save: bool = True) -> None:
    """Grava o projeto que `resolve_project` criou e avisa no stderr; com `save=False` (`--dry-run`) ele vale só nesta
    execução."""
    if not added:
        return
    if save:
        store.save()
    print(f"Aviso: projeto cadastrado a partir da pasta atual: {project['name']} ({project['path']})"
          + ("" if save else "; só nesta execução, o --dry-run não grava") + ".", file=sys.stderr)


def worktrees_command(store: Store, options) -> int:
    """`launcher worktrees [--project REF] [--json]`: os worktrees do launcher no projeto (só leitura; remover é
    `git worktree remove`)."""
    project = store.find(options.project or os.getcwd())
    found = worktrees.listing(project["path"])
    if options.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    if not found:
        print(f"Nenhum worktree do launcher em {project['name']}.")
    for item in found:
        state = ("pasta ausente (git worktree prune limpa o registro)" if not item["exists"]
                 else "pronto" if item["ready"] else f"sem o marcador {worktrees.MARKER} (incompleto ou criado à mão)")
        print(f"  {item['name']} • {item['branch'] or 'cabeça solta'} • {state}\n      {item['path']}")
    return 0


def show_connections(store: Store) -> None:
    heading("CONEXÕES")
    modules, errors = store.connectors()
    for index, connection in enumerate(store.connections, start=1):
        module = modules.get(connection["connector"])
        marker = " • padrão" if connection["id"] == store.data["default_connection"] else ""
        label = module.LABEL if module else f"{connection['connector']} (conector indisponível)"
        quota = " • cota esgotada" if (connection.get("status") or {}).get("state") == "quota_exhausted" else ""
        reserve = store.fallback_of(connection)
        reserve_text = f" • reserva: {reserve['name']}" if reserve else ""
        sessions_text = " • sessões pelo endpoint" if store.endpoint_sessions_of(connection) else ""
        print(f"  {index:>2}. {connection['name']}{marker}\n      {label} • {connection['options'].get('base_url', '')}"
              f"{quota}{reserve_text}{sessions_text}")
    if not store.connections:
        print("  Nenhuma conexão. Adicione um provedor (LiteLLM, 9router, OmniRoute, OpenRouter, Anthropic, OpenAI...).")
    for error in errors:
        print(f"  [AVISO] conector do usuário não carregado: {error}")


def set_fallback_command(store: Store, options) -> None:
    """`launcher connections --set-fallback NOME --to RESERVA` ou `--clear-fallback NOME`. LauncherError para
    combinação inválida de opções ou reserva recusada pelo cadastro (inexistente, a própria conexão, ciclo)."""
    if options.clear_fallback and (options.set_fallback or options.to):
        raise LauncherError("--clear-fallback não se combina com --set-fallback nem com --to.")
    if options.clear_fallback:
        store.clear_fallback(options.clear_fallback)
        print(f"Conexão reserva de {store.find_connection(options.clear_fallback)['name']} removida.")
        return
    if not (options.set_fallback and options.to):
        raise LauncherError("--set-fallback NOME e --to RESERVA andam juntos: informe os dois.")
    store.set_fallback(options.set_fallback, options.to)
    print(f"Conexão reserva de {store.find_connection(options.set_fallback)['name']}: "
          f"{store.find_connection(options.to)['name']}.")


def endpoint_sessions_command(store: Store, options) -> None:
    """`launcher connections --endpoint-sessions on|off --connection NOME`: liga ou desliga as sessões de terminal pelo
    endpoint local da conexão. LauncherError sem --connection ou com conexão inexistente."""
    if not options.connection:
        raise LauncherError("--endpoint-sessions precisa de --connection NOME.")
    store.set_endpoint_sessions(options.connection, options.endpoint_sessions == "on")
    name = store.find_connection(options.connection)["name"]
    print(f"Sessões de terminal da conexão {name} pelo endpoint local: "
          f"{'ligadas' if options.endpoint_sessions == 'on' else 'desligadas (direto ao host)'}.")


def add_project(store: Store) -> dict | None:
    path = ask("Pasta do projeto (ENTER cancela)").strip('"')
    if not path:
        return None
    name = ask("Nome (ENTER usa o nome da pasta)")
    project = store.add(path, name or None)
    print(f"Projeto disponível: {project['name']}")
    warn_shared(store, project)
    return project


def warn_shared(store: Store, project: dict, resume: bool = False) -> None:
    """Mesma pasta em outro perfil: avisa em stderr (não pergunta nada, para não travar scripts)."""
    try:
        warning = profiles.shared_warning(project["path"], store.home)
    except (LauncherError, OSError):
        return
    if warning:
        extra = " Retomar usa só as conversas abertas neste perfil." if resume else ""
        print(f"Aviso: {warning}{extra}", file=sys.stderr)


def test_all_connections(store: Store) -> int:
    """Testa uma a uma, sem parar na primeira que falha, e diz o resultado de cada. Devolve 1 se alguma falhou."""
    failed = 0
    for connection in store.connections:
        try:
            Gateway(store, connection).verify()
        except LauncherError as error:
            print(f"{connection['name']}: falhou: {' '.join(str(error).split())}")
            failed = 1
        else:
            print(f"{connection['name']}: respondeu")
    return failed


def report_cleanup(messages) -> int:
    """O que foi feito vai para a saída; o que falhou, para o erro padrão. Devolve 1 se algum passo falhou (a remoção segue)."""
    from agent_launcher.harnesses.cleanup import Failure
    failed = False
    for message in messages:
        if isinstance(message, Failure):
            print(f"Falha na limpeza: {message}", file=sys.stderr)
            failed = True
        else:
            print(message)
    return 1 if failed else 0


def profiles_command(options) -> int:
    action = options.profile_action or "list"
    if action == "add":
        profile = profiles.create(options.name)
        print(f"Perfil criado: {profile['name']} ({profile['id']})")
    elif action == "rename":
        profile = profiles.rename(profiles.find(options.reference)["id"], options.name)
        print(f"Perfil renomeado: {profile['name']} ({profile['id']})")
    elif action == "import":
        from agent_launcher.domain import profile_package
        result = profile_package.import_profile(profiles.root(), Path(options.file), ask=ask_package)
        print(profile_package.describe(result))
    elif action == "export":
        from agent_launcher.domain import profile_package
        profile = profiles.find(options.reference)
        result = profile_package.export_profile(profiles.root(), profile["id"], Path(options.file))
        print(f"Perfil {profile['name']} exportado (sem chaves) em {result['path']}: {len(result['connections'])} "
              f"conexão(ões)" + (f", conectores {', '.join(result['connectors'])}" if result["connectors"] else "") + ".")
    elif action == "remove":
        profile = profiles.find(options.reference)
        status = 0
        if options.clean_destinations:
            from agent_launcher.harnesses.cleanup import cleanup_profile
            status |= report_cleanup(cleanup_profile(profiles.home_of(profile["id"]), confirm=confirm_flagged))
        from agent_launcher.harnesses.cleanup import release_profile_endpoints
        status |= report_cleanup(release_profile_endpoints(profiles.home_of(profile["id"])))
        profiles.remove(profile["id"], options.delete_data)
        print(f"Perfil removido: {profile['name']}" + (" (dados apagados)" if options.delete_data else
                                                        f" (dados preservados em {profiles.root() / profiles.FOLDER / profile['id']})"))
        return status
    elif action == "use":
        profile = profiles.find(options.reference)
        profiles.set_active(profile["id"])
        print(f"Perfil ativo: {profile['name']}")
    else:
        current = profiles.active()["id"]
        print(json.dumps([{"id": item["id"], "name": item["name"], "color": item["color"],
                           "active": item["id"] == current, "home": str(profiles.home_of(item["id"]))}
                          for item in profiles.list_profiles()], ensure_ascii=False, indent=2))
    return 0


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


def choose_cli(gateway: Gateway, default: str = "claude", kinds: tuple = ("terminal",)) -> str | None:
    options = [harness.ID for harness in registry.all() if harness.KIND in kinds and harness.ID in gateway.clis]
    if not options:
        print(f"  {gateway.name} não atende nenhum destino de terminal.")
        return None
    if len(options) == 1:
        print(f"  {gateway.name} atende somente o {label_of(options[0])}.")
        return options[0]
    while True:
        print("\n  " + "    ".join(f"{number}. {label_of(cli)}" for number, cli in enumerate(options, start=1)) + "    0. Voltar")
        value = ask("Ferramenta", str(options.index(default) + 1) if default in options else "1")
        if value == "0":
            return None
        chosen = number_choice(value, len(options))
        if chosen is not None:
            return options[chosen]
        print(f"Escolha de 1 a {len(options)}; 0 volta ao menu.")


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


def require_terminal_for_worktree(cli: str, worktree: str | None) -> None:
    """`--worktree` só vale para os destinos de terminal; erro claro para os demais."""
    if worktree is not None and registry.get(cli).KIND != "terminal":
        accepted = ", ".join(item.LABEL for item in registry.terminals())
        raise LauncherError(f"--worktree vale só para as sessões de terminal ({accepted}); "
                            f"{registry.get(cli).LABEL} abre numa pasta própria.")


def launch(store: Store, project: dict, gateway: Gateway, cli: str, model: str, resume: bool = False,
           dry_run: bool = False, controlled: bool = False, conversation: str | None = None,
           command_shell: str | None = None, worktree: str | None = None, fork: bool = False) -> int:
    """Abre a sessão no terminal atual. Com `dry_run`, só imprime o plano (JSON); com `command_shell` (cmd ou powershell),
    imprime em vez disso o comando que o reproduz fora do launcher. `worktree` (nome; vazio gera um) abre a sessão num
    worktree git do projeto (session/worktree.py), criado se não existir; só vale para os destinos de terminal."""
    require_terminal_for_worktree(cli, worktree)
    if not dry_run:
        quota = gateway.verify(tolerate_quota=True)
        if quota:
            print(f"  [AVISO] {quota_notice(gateway, quota, cli)}")
    try:
        catalog = gateway.catalog(offline=dry_run)
    except LauncherError as error:
        catalog = gateway.empty_catalog(status=str(error))
    harness = registry.get(cli)
    if not dry_run and clis.locate(cli) is None and harness.install_steps():
        offer_install(cli)
    if harness.KIND != "terminal":
        if command_shell:
            raise LauncherError(f"{harness.LABEL} não abre por comando de terminal: --command só vale para as sessões "
                                "de terminal (Claude Code, Codex, Copilot CLI).")
        if dry_run:
            print(json.dumps(harness.preview(store, project, gateway, model, catalog), ensure_ascii=False, indent=2))
            return 0
        print(harness.open(store, project, gateway, model, catalog, confirm=confirm_in_console))
        store.remember(project, gateway.connection, cli, base_model(model))
        return 0
    found = worktrees.locate(project["path"], worktree) if worktree is not None else None
    plan = planned(store.home, gateway, project["path"], cli, model, resume, catalog, controlled, conversation, found, fork)
    if dry_run:
        print(plan_command(plan, gateway, resume, catalog, command_shell) if command_shell else
              json.dumps(plan_report(plan, gateway, resume, catalog), ensure_ascii=False, indent=2))
        return 0
    if found:
        plan.worktree = worktrees.create(found)
    heading(f"{'RETOMAR' if resume else 'INICIAR'} / {cli.upper()}")
    print(f"  Projeto: {project['name']}\n  Pasta:   {plan.directory}\n  Modelo:  {plan.model}")
    if found:
        print(f"  Worktree: {plan.worktree.branch} ({'novo' if plan.worktree.created else 'reaproveitado'})")
    print(f"  Conexão: {gateway.name} • {harness.connection_url(gateway)}")
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
    record_session(store.home, project, gateway, plan)  # registro do perfil
    harness.prepare(plan)
    exit_code = harness.run(plan, project)
    if exit_code:
        print(f"\n{cli} encerrou com código {exit_code}. Use 'doctor' para verificar a conexão e o catálogo.")
    return exit_code


def found_conversations(store: Store, project: dict, cli: str | None = None) -> list:
    """Conversas da pasta do projeto nos CLIs de terminal (as deste perfil, se a pasta é compartilhada), mais recente
    primeiro."""
    directory = Path(project["path"]).expanduser().resolve()
    found = []
    for harness in registry.terminals():
        if cli is None or harness.ID == cli:
            found += conversations_for(store.home, harness, directory)
    return sorted(found, key=lambda item: item.updated, reverse=True)


def conversation_usages(home: Path, conversations: list) -> dict:
    """Custo e contexto de cada conversa da lista (por id), lidos dos transcripts (cache por arquivo)."""
    cache, tables = usage.FileCache(home), usage.price_table(home)
    found = {item.id: conversation_usage(home, item.cli, item.id, tables, cache) for item in conversations}
    cache.save()
    return found


def filtered_conversations(store: Store, project: dict, cli: str | None = None, search: str = "",
                           grep: str | None = None) -> list:
    """As conversas de found_conversations; `search` fica só com as que têm o texto no título, no CLI ou na data (a mesma
    regra do filtro da janela de conversas) e `grep`, só com as que têm o texto em alguma mensagem (lê os transcripts;
    o que os tetos de conversation_grep deixaram sem ler vai como [AVISO] no stderr)."""
    found = found_conversations(store, project, cli)
    query = search.strip().casefold()
    if query:
        found = [item for item in found if query in " ".join(
            (item.as_json()["updated"], item.cli, item.title)).casefold()]
    if grep is not None:
        found, notes = grep_conversations(found, grep)
        for note in notes:
            print(f"[AVISO] {note}", file=sys.stderr)
    return found


def folder_conversations_tsv(store: Store, project: dict, cli: str | None = None, search: str = "",
                             grep: str | None = None) -> str:
    """As conversas de filtered_conversations no texto separado por tabulação que o "Copiar lista" da janela de conversas
    copia (sem a janela do modelo: contexto sem porcentagem)."""
    found = filtered_conversations(store, project, cli, search, grep)
    return conversations_tsv(found, conversation_usages(store.home, found), branches=True)


def list_conversations(store: Store, project: dict, cli: str | None = None, search: str = "",
                       grep: str | None = None) -> list[dict]:
    """As conversas de filtered_conversations com o contexto e o custo estimado de cada uma e, no fim, o ramo git que o CLI
    gravou (a chave `branch` só existe quando ele grava)."""
    found = filtered_conversations(store, project, cli, search, grep)
    used = conversation_usages(store.home, found)
    return [{**item.as_json(), "context_tokens": used[item.id].context,
             "cost_usd": None if used[item.id].cost is None else round(used[item.id].cost, 6),
             **({"branch": item.branch} if item.branch else {})} for item in found]


def conversation_command(store: Store, project: dict, reference: str, cli: str | None = None,
                         connection: str | None = None, model: str | None = None, shell: str = "cmd",
                         grep: str | None = None) -> str:
    """O comando que retoma fora do launcher a conversa `reference` (ID, ou posição de 1 em diante na lista de
    `conversations`, já filtrada por `cli` e por `grep`), para colar no `shell` (cmd ou powershell): o mesmo plano do
    `launch --resume --conversation --dry-run`."""
    found = filtered_conversations(store, project, cli, grep=grep)
    chosen = next((item for item in found if item.id == reference), None)
    if chosen is None:
        position = number_choice(reference, len(found))
        if position is None:
            options = f"use o ID ou uma posição de 1 a {len(found)}" if found else "não há o que retomar"
            raise LauncherError(f"Conversa {reference} não encontrada: a pasta tem {len(found)} conversa(s); {options} "
                                "(veja 'conversations').")
        chosen = found[position]
    gateway = Gateway.for_project(store, project, connection)
    model = model or store.last_model(project, gateway.connection, chosen.cli)
    if not model:
        raise LauncherError("Informe --model com um ID completo, ou escolha um modelo pela interface primeiro.")
    return resume_command(store.home, gateway, project["path"], chosen.cli, model, gateway.catalog(offline=True),
                          chosen.id, shell)


def sessions_command(store: Store, options) -> int:
    """`launcher sessions --reopen`: reabre as sessões das últimas --hours horas que não estão abertas, cada uma na
    conversa exata, como abas da janela única em segundo plano. Com --dry-run só lista (JSON). Devolve 1 se alguma aba
    não abriu; as linhas desabilitadas (conversa não identificada) aparecem com o motivo e nunca abrem."""
    if not options.reopen:
        raise LauncherError("Use --reopen para reabrir as sessões das últimas horas (com --dry-run só lista).")
    if options.hours is not None and options.hours <= 0:
        raise LauncherError("--hours precisa ser maior que zero.")
    hours = reopen.RECENT_HOURS if options.hours is None else options.hours
    rows = reopen.candidates(store, hours)
    if options.dry_run:
        print(json.dumps({"hours": hours, "sessions": [row.as_json() for row in rows]}, ensure_ascii=False, indent=2))
        return 0
    results = reopen.open_all(store, [row for row in rows if row.selectable])
    failed = [result for result in results if not result["ok"]]
    skipped = [row.as_json() for row in rows if not row.selectable]
    print(json.dumps({"hours": hours, "opened": len(results) - len(failed), "failed": len(failed),
                      "skipped": skipped, "results": results}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def keys_command(store: Store, options) -> int:
    """`launcher keys`: lista as sessões abertas, ou lê a tela e envia teclas a uma delas. Com --wait, espera o texto
    aparecer (até --timeout) antes de ler e de enviar; prazo estourado é erro e nenhuma tecla vai."""
    if options.list:
        print(json.dumps(keys.sessions(), ensure_ascii=False))
        return 0
    if options.pid is None and not options.project:
        raise LauncherError("Informe --project (e --cli se preciso) ou --pid; veja --list.")
    if options.timeout is not None and not options.wait:
        raise LauncherError("--timeout só vale com --wait.")
    if not options.keys and not options.screen and not options.wait:
        raise LauncherError("Informe as teclas a enviar, --screen ou --wait.")
    try:
        ids = {store.find(options.project)["id"]} if options.project else set()
        session = keys.resolve(ids, options.cli, options.pid)
        if options.wait:
            screen = keys.wait_for_text(session["pid"], options.wait,
                                        keys.WAIT_TIMEOUT if options.timeout is None else options.timeout)
        elif options.screen:
            screen = keys.read_screen(session["pid"])
        if options.screen:
            print(screen)
        done = {"ok": True, "pid": session["pid"], "title": session["title"]}
        if options.keys:
            keys.send(session["pid"], options.keys)
            done["sent"] = options.keys
        if options.wait:
            done["waited"] = options.wait
        if options.keys or (options.wait and not options.screen):
            print(json.dumps(done, ensure_ascii=False))
    except keys.KeysError as error:
        raise LauncherError(str(error)) from error
    return 0


def confirm_in_console(question: str) -> bool:
    return ask(f"{question} (s/n)", "n").lower() in ("s", "sim", "y", "yes")


def offer_install(cli: str) -> None:
    """Ferramenta que falta: no console pergunta se instala agora; sem console (ou com "não"), explica como instalar."""
    missing = (f"{label_of(cli)} não está instalado. Instale pelo cartão dele na interface ou com: "
               f"launcher.py cli --install {cli}")
    if not (sys.stdin and sys.stdin.isatty()) or not confirm_in_console(f"{label_of(cli)} não está instalado. "
                                                                        "Instalar agora, sem admin?"):
        raise LauncherError(missing)
    if not clis.ensure(cli):
        raise LauncherError(missing)


def confirm_flagged(question: str) -> bool:
    """--clean-destinations já é o pedido explícito: mostra o que vai acontecer e segue."""
    print(question.replace("\n\n", " "))
    return True


def ask_package(kind: str, *args):
    """Perguntas da importação de perfil no console: chave oculta (getpass) e conector diferente do instalado.
    Sem console interativo: não substitui conector e não pede chave (a conexão fica sem chave, com aviso)."""
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if kind == "secret":
        connection, field = args
        if not interactive:
            return None
        import getpass
        value = getpass.getpass(f"{field} da conexão {connection} (Enter deixa sem chave): ").strip()
        return value or None
    if kind == "replace_connector":
        return interactive and confirm_in_console(f"O conector {args[0]} do pacote é diferente do instalado. Substituir?")
    return None


def ask_connection_file(kind: str, *args):
    """Importação de uma conexão no console: só o conector diferente do instalado é perguntado; a chave nunca (fica por
    preencher)."""
    return None if kind == "secret" else ask_package(kind, *args)


DOCTOR_NOTE ="O diagnóstico só lista modelos: não gera tokens. Catálogo acessível não garante inferência."
MARKERS = {"ok": "[OK]", "aviso": "[AVISO]", "erro": "[ERRO]"}


def check(name: str, state: str, message: str, marker: str | None = None, indent: int = 2) -> dict:
    """Uma verificação do diagnóstico. `marker` e `indent` só mudam como o texto a mostra ("" não leva marcador)."""
    return {"name": name, "state": state, "message": message,
            "marker": MARKERS[state] if marker is None else marker, "indent": indent}


def doctor_data(store: Store, reference: str | None = None) -> dict:
    """Coleta o diagnóstico (somente leitura): CLIs, Windows Terminal, conexões com o catálogo ao vivo e endpoints.
    `ok` cai só com verificação em erro (aviso não derruba). LauncherError se `reference` não for uma conexão."""
    checks = []
    installed = 0
    for harness in registry.all():
        found = clis.locate(harness.ID)
        if not found:
            hint = f" (instale pelo cartão ou 'cli --install {harness.ID}')." if harness.install_steps() else "."
            checks.append(check(harness.ID, "aviso", f"{harness.LABEL} não instalado{hint}", "[--]"))
            continue
        if harness.KIND == "terminal":
            try:
                found = harness.command()[-1]
            except LauncherError as error:
                checks.append(check(harness.ID, "aviso", str(error)))
                continue
        installed += 1
        checks.append(check(harness.ID, "ok", f"{harness.ID}: {found}"))
    for error in registry.errors():
        checks.append(check("adaptador", "aviso", f"adaptador do usuário não carregado: {error}"))
    if not installed:
        checks.append(check("ferramentas", "aviso", "Nenhuma ferramenta instalada ainda: normal numa instalação nova. "
                                                    "Cada uma se instala pelo cartão dela na interface.", "[--]"))
    wt = terminal.find(ROOT)
    checks.append(check("windows-terminal", "ok", f"Windows Terminal: {wt}") if wt else
                  check("windows-terminal", "aviso",
                        "Windows Terminal ausente: sessões em consoles separados. Use 'terminal --install'."))
    connections = [store.find_connection(reference)] if reference else store.connections
    if not connections:
        checks.append(check("conexoes", "erro", "Nenhuma conexão cadastrada. Use 'connections add'."))
    for connection in connections:
        name = connection["name"]
        try:
            gateway = Gateway(store, connection)
            catalog = gateway.catalog(refresh=True, save_cache=False)
            checks.append(check(name, "ok", f"{gateway.name} ({gateway.module.LABEL}) • {gateway.url}"))
            for cli in [cli for cli in destinations() if cli in gateway.clis]:
                records = catalog.for_cli(cli)
                large = sum(record.one_million for record in records) if catalog.verified else 0
                checks.append(check(f"{name}: {cli}", "ok", f"{label_of(cli)}: {len(records)} modelos; "
                                                            f"{large} com entrada >= 1M informada", "", 7))
            if not catalog.verified:
                checks.append(check(f"{name}: catálogo", "aviso", catalog.status, indent=7))
            mark = connection.get("status") or {}
            if mark.get("state") == "quota_exhausted":
                checks.append(check(f"{name}: cota", "aviso",
                                    f"cota esgotada na última verificação: {mark.get('message', '')}", indent=7))
        except LauncherError as error:
            checks.append(check(name, "erro", f"{name}: {error}"))
    from agent_launcher.endpoint import registry as endpoints
    for item in endpoints.status(store.home, connections):
        if item["port"]:
            state = f"rodando, {item['requests']} requisições" if item["running"] else "parado (sobe ao abrir o destino)"
            stale = item.get("autostart_stale")
            checks.append(check(f"endpoint: {item['connection']}", "aviso" if stale else "ok", (
                f"Endpoint local de {item['connection']}: {item['url']} • {state}"
                + (" • inicia com o Windows" if item["autostart"] else "")
                + (f" • AVISO: {stale} (abrir o destino pelo launcher regrava)" if stale else "")), "[OK]"))
    return {"version": VERSION, "data_dir": str(store.home), "ok": all(item["state"] != "erro" for item in checks),
            "checks": checks}


def doctor(store: Store, reference: str | None = None, as_json: bool = False) -> int:
    """`launcher doctor [--connection NOME] [--json]`: imprime `doctor_data` em texto (o de sempre) ou em JSON, só com
    nome, estado e mensagem de cada verificação; o código de saída é 1 quando alguma está em erro."""
    data = doctor_data(store, reference)
    if as_json:
        print(json.dumps({**data, "checks": [{key: item[key] for key in ("name", "state", "message")}
                                             for item in data["checks"]]}, ensure_ascii=False, indent=2))
        return 0 if data["ok"] else 1
    heading("DIAGNÓSTICO / SOMENTE LEITURA")
    print(f"  {APP_NAME} {data['version']}\n  Dados: {data['data_dir']}")
    for item in data["checks"]:
        print(" " * item["indent"] + (item["marker"] + " " if item["marker"] else "") + item["message"])
    print(f"\n  {DOCTOR_NOTE}")
    return 0 if data["ok"] else 1


def has_source(record: ModelInfo, name: str) -> bool:
    """O modelo é da fonte `name` (sem diferença de maiúscula)? Modelo sem fonte não casa com nenhuma."""
    return record.source is not None and record.source.casefold() == name.casefold()


def listed_models(gateway, catalog, options, fields: list[str]) -> list[tuple[ModelInfo, dict]]:
    """Modelos do catálogo que passam pelos filtros de `models` (destino, família, trecho do ID, fonte e as capacidades
    `fields`), cada um com o item do `/v1/models` que o endpoint local daria. `--source` só vale quando o conector da
    conexão declara fontes; LauncherError caso contrário."""
    from agent_launcher.endpoint.server import model_entry, with_capabilities
    if options.source and not catalog.segregates_sources:
        raise LauncherError("--source: o conector desta conexão não declara fontes (SEGREGATES_SOURCES); "
                            "sem declaração, os modelos não têm fonte.")
    wanted = [options.cli] if options.cli else sorted(gateway.clis)
    listed = [record for record in catalog.records
              if any(catalog.allows(record, cli) for cli in wanted)
              and (not options.family or record.family == options.family)
              and options.search.casefold() in record.id.casefold()
              and (not options.source or has_source(record, options.source))]
    entries = {record.id: model_entry(record, record.provider or gateway.name) for record in listed}
    kept = {entry["id"] for entry in with_capabilities(list(entries.values()), fields)}
    return [(record, entries[record.id]) for record in listed if record.id in kept]


def model_test_command(store: Store, options) -> int:
    """`launcher models test MODELO [--connection X] [--yes]`: mostra o custo em tokens do teste e só faz a chamada real
    de 1 token com --yes ou com o "sim" no console. Sem confirmação nada é enviado (código 2); chamada com erro do host
    ou do endpoint imprime a causa no stderr (código 1). LauncherError para uso inválido."""
    from agent_launcher.domain import model_probe
    if options.words[:1] != ["test"]:
        raise LauncherError(f"Ação desconhecida de models: {' '.join(options.words) or '--yes'!r}. "
                            "Use 'models test MODELO [--yes]'; --yes só vale com 'test'.")
    if len(options.words) != 2:
        raise LauncherError("Informe o modelo: models test MODELO [--connection NOME] [--yes].")
    gateway = Gateway.for_project(store, reference=options.connection)
    record = model_probe.find_model(gateway, options.words[1])
    protocol = model_probe.protocol_of(gateway, record)
    print(model_probe.cost_text(model_probe.estimate_cost(record, protocol)))
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if not options.yes and not (interactive and confirm_in_console("Fazer a chamada real agora?")):
        print("\nNada foi enviado. Confirme com --yes para fazer a chamada real.")
        return 2
    result = model_probe.probe(store.home, gateway.connection["id"], record.id, protocol)
    print(model_probe.result_text(result), file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


def models_command(store: Store, options) -> int:
    """`launcher models`: os modelos da conexão que o destino aceita, filtrados por trecho do ID, família, fonte e
    capacidade. Em texto, uma linha por modelo; com --json, os itens do `/v1/models` do endpoint local, que decidem a
    capacidade (`capabilities_wanted`, `with_capabilities`). `models test MODELO` testa o modelo (ver
    `model_test_command`).
    LauncherError para opção inválida."""
    from agent_launcher.endpoint.server import capabilities_wanted
    if options.words or options.yes:
        return model_test_command(store, options)
    gateway = Gateway.for_project(store, reference=options.connection)
    if options.family and options.cli and options.cli != "claude":
        raise LauncherError(f"--family é um filtro do Claude; não se aplica ao {label_of(options.cli)}.")
    try:
        fields = capabilities_wanted(options.capability)
    except ValueError as error:
        raise LauncherError(str(error)) from error
    catalog = gateway.catalog(options.refresh)
    pairs = listed_models(gateway, catalog, options, fields)
    listed = [record for record, _entry in pairs]
    if options.sort:
        listed = order_models(listed, options.sort)
    entries = {record.id: entry for record, entry in pairs}
    if options.json:
        print(catalog.status, file=sys.stderr)
        if catalog.news():
            print(catalog.news(), file=sys.stderr)
        print(json.dumps([entries[record.id] for record in listed], ensure_ascii=False, indent=2))
        if catalog.news():
            gateway.mark_news_seen(catalog)
        return 0
    print(catalog.status)
    if catalog.news():
        print(catalog.news())
    for record in listed:
        print(catalog.describe(record))
    if catalog.news():
        gateway.mark_news_seen(catalog)
    return 0


def endpoint_command(store: Store, options) -> int:
    """Endpoint local: o VS Code e o Copilot app só conhecem esta URL; chave e host ficam no launcher."""
    from agent_launcher.endpoint import registry as endpoints
    if options.endpoint_action == "logs":
        return endpoint_log(store, options)
    if options.endpoint_action == "status":
        connections = [store.find_connection(options.connection)] if options.connection else store.connections
        print(json.dumps(endpoints.status(store.home, connections), ensure_ascii=False, indent=2))
        return 0
    if not options.connection:
        raise LauncherError("Informe --connection.")
    connection = store.find_connection(options.connection)
    if options.endpoint_action == "config":
        state = endpoints.status(store.home, [connection])[0]
        if not state["running"]:
            raise LauncherError(f"O endpoint de {connection['name']} está parado: suba com "
                                f"'endpoint start --connection \"{connection['name']}\"' e peça de novo.")
        print(endpoints.connection_config(store.home, connection["id"], state["port"], options.shell))
    elif options.endpoint_action == "start":
        port = endpoints.ensure_running(store.home, connection["id"])
        print(f"Endpoint local de {connection['name']}: http://127.0.0.1:{port}/v1")
    elif options.endpoint_action == "stop":
        print("Parado." if endpoints.stop(store.home, connection["id"]) else "Não estava rodando.")
    else:
        endpoints.autostart(store.home, connection["id"], not options.off)
        print(f"Endpoint de {connection['name']} não inicia mais com o Windows." if options.off else
              f"Endpoint de {connection['name']} inicia com o Windows (sem janela, sem admin).")
    return 0


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
        print("  Claude Code, Codex, Copilot CLI, VS Code e Copilot app em qualquer provedor compatível.")
        recent = next((project for project in store.projects if project["id"] == store.data["last_project"]), None)
        if recent:
            print(f"\n  ENTER: {recent['name']} / {store.last_cli(recent)} / último modelo")
        print("\n  1. Nova sessão        2. Retomar última sessão do projeto\n  3. Projetos           4. Catálogo de modelos\n"
              "  5. Diagnóstico        6. Importar projetos\n  7. Conexões           0. Sair")
        try:
            value = ask("Ação", "enter" if recent else ("1" if store.connections else "7"))
            if value == "0":
                return 0
            if value == "enter" and recent:
                gateway = Gateway.for_project(store, recent)
                model = store.last_model(recent, gateway.connection, store.last_cli(recent))
                if not model:
                    raise LauncherError("Escolha um modelo em 'Nova sessão' antes de usar o atalho.")
                launch(store, recent, gateway, store.last_cli(recent), model)
            elif value in ("1", "2"):
                project = choose_project(store)
                if not project:
                    continue
                connection = choose_connection(store, project)
                if not connection:
                    continue
                gateway = Gateway(store, connection)
                cli = choose_cli(gateway, store.last_cli(project), ("terminal", "ide", "app"))
                if not cli:
                    continue
                if clis.locate(cli) is None and registry.get(cli).install_steps():
                    offer_install(cli)
                elif registry.get(cli).KIND == "terminal":
                    registry.get(cli).command()  # erra antes da lista de modelos se o CLI não estiver instalado
                model = choose_model(gateway, cli, store.last_model(project, connection, cli))
                if model:
                    if value == "2":
                        warn_shared(store, project, resume=True)
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


def parse_values(module, pairs: list[str], secret_env: list[str], prompt: bool = True) -> dict:
    """--set campo=valor para campos comuns; --secret-env campo=VARIAVEL lê o segredo do ambiente (fora do histórico). Com
    prompt, o segredo obrigatório que faltou é pedido no terminal."""
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
        if item.get("secret") and item.get("required") and key not in values and prompt and sys.stdin.isatty():
            values[key] = getpass.getpass(f"{item['label']} (oculto): ")
    return values


def usage_by_source_command(store, options) -> int:
    """`launcher usage --by-source [--connection X] [--month AAAA-MM] [--tsv]`: o custo do mês por fonte que o conector da
    conexão devolve pela rota `costs`, sem cálculo do launcher; JSON por padrão, `--tsv` em tabela. LauncherError sem a
    rota, com `--all` ou com falha do conector."""
    if options.all:
        raise LauncherError("--by-source lê o custo de uma conexão (--connection); não se combina com --all.")
    try:
        month = usage.month_bounds(options.month)[0]
    except ValueError as error:
        raise LauncherError(str(error)) from error
    costs = Gateway.for_project(store, reference=options.connection).costs(month)
    print(usage.source_costs_text(month, costs) if options.tsv else json.dumps({"month": month, **costs},
                                                                                ensure_ascii=False, indent=1))
    return 0


def usage_months_command(store, options) -> int:
    """`usage --months N`: uma linha com mês, moeda e total por mês, do corrente para trás, do perfil (ou de todos)."""
    if not 1 <= options.months <= MAX_USAGE_MONTHS:
        raise LauncherError(f"--months aceita de 1 a {MAX_USAGE_MONTHS}.")
    if options.month or options.tsv or options.by_source:
        raise LauncherError("--months lista os totais por mês; não combina com --month, --tsv nem --by-source.")
    build = usage.usage_report_all if options.all else usage.usage_report
    rows = []
    for month in usage.recent_months(options.months):
        report = build(store.home, month)
        rows.append({"month": month, "currency": report["currency"], "total": report["total"]})
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


def usage_days_command(store, options) -> int:
    """`usage --days N`: o uso de cada um dos N últimos dias em que houve uso (JSON, ou `--tsv`), do de hoje para trás."""
    if not 1 <= options.days <= usage.MAX_DAYS:
        raise LauncherError(f"--days aceita de 1 a {usage.MAX_DAYS}.")
    if options.month or options.months is not None or options.by_source or options.all or options.limit is not None:
        raise LauncherError("--days não combina com --month, --months, --by-source, --all nem --limit.")
    rows = usage.usage_days_report(store.home, options.days)
    print(usage.usage_days_text(rows) if options.tsv else json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


def usage_blocks_command(store, options) -> int:
    """`usage --blocks [--active]`: os blocos de 5 h de uso dos últimos 8 dias, do mais recente para trás (JSON, ou `--tsv`);
    `--active` deixa só o bloco em andamento."""
    if (options.month or options.months is not None or options.by_source or options.all or options.limit is not None
            or options.days is not None):
        raise LauncherError("--blocks não combina com --month, --months, --by-source, --all, --limit nem --days.")
    rows = usage.usage_blocks_report(store.home)
    if options.active:
        rows = [row for row in rows if row["active"]]
    print(usage.usage_blocks_text(rows) if options.tsv else json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


def usage_command(store, options) -> int:
    """`launcher usage [--month AAAA-MM] [--all] [--tsv]`: estimativa local de uso do perfil (ou de todos), em JSON ou
    no texto separado por tabulação que o "Copiar relatório" da interface copia. Com o teto mensal ligado, o JSON do
    perfil traz também `limit`. `--limit VALOR|off` só grava ou desliga o teto, sem calcular o relatório. `--days N` lista
    o uso por dia; `--blocks`, o por bloco de 5 h."""
    if options.active and not options.blocks:
        raise LauncherError("--active só vale com --blocks.")
    if options.blocks:
        return usage_blocks_command(store, options)
    if options.days is not None:
        return usage_days_command(store, options)
    if options.limit is not None:
        if options.month or options.all or options.tsv or options.by_source:
            raise LauncherError("--limit só grava ou desliga o teto mensal; rode 'usage' sem ele para ver o relatório.")
        value = usage.parse_limit(options.limit)
        store.set_monthly_limit(value)
        print("Limite mensal desligado." if value is None else f"Limite mensal: {usage.money(value)}.")
        return 0
    if options.months is not None:
        return usage_months_command(store, options)
    if options.by_source:
        return usage_by_source_command(store, options)
    try:
        report = (usage.usage_report_all if options.all else usage.usage_report)(store.home, options.month)
    except ValueError as error:
        raise LauncherError(str(error)) from error
    if options.tsv:
        print(usage.usage_text(report))
        return 0
    status = None if options.all else usage.limit_status(report, store.monthly_limit())
    print(json.dumps({**report, "limit": status} if status else report, ensure_ascii=False, indent=1))
    return 0


def status_report(store: Store, month: str | None = None, now: float | None = None) -> dict:
    """Retrato único e sem segredos: CLIs, conexões, endpoint de cada uma, sessões abertas, projetos e o uso do mês.
    Cada parte vem da função por trás do `doctor`, do `endpoint status`, do `keys --list`, do `projects list` e do `usage`;
    cada sessão aberta traz também `activity` (session/activity.py; `now` é o relógio, só para testes)."""
    from agent_launcher.endpoint import registry as endpoints
    tools = []
    for harness in registry.all():
        found = clis.locate(harness.ID)
        tools.append({"id": harness.ID, "label": harness.LABEL, "installed": bool(found), "path": found,
                      "version": cli_update.installed_version(harness.ID, found) if found and harness.ID in clis.NAMES
                      else None})
    report = usage.usage_report(store.home, month)
    status = usage.limit_status(report, store.monthly_limit())
    return {
        "version": VERSION,
        "clis": tools,
        "connections": [{"id": connection["id"], "name": connection["name"], "connector": connection["connector"],
                         "base_url": connection["options"].get("base_url", ""),
                         "default": connection["id"] == store.data["default_connection"],
                         "quota_exhausted": (connection.get("status") or {}).get("state") == "quota_exhausted"}
                        for connection in store.connections],
        "endpoints": endpoints.status(store.home, store.connections),
        "sessions": activity.annotate(store, keys.sessions(), now),
        "projects": json.loads(projects_json(store)),
        "usage": {**{key: report[key] for key in ("month", "currency", "destinations", "total")},
                  **({"limit": status} if status else {})},
    }


def status_command(store: Store, options) -> int:
    """`launcher status [--month AAAA-MM]`: o retrato de `status_report` em JSON."""
    try:
        report = status_report(store, options.month)
    except ValueError as error:
        raise LauncherError(str(error)) from error
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def endpoint_log(store: Store, options) -> int:
    """`launcher endpoint logs`: as últimas linhas do endpoint.log do perfil (o log é um só por perfil, sem chave nem corpo)."""
    if options.lines < 1:
        raise LauncherError("--lines precisa ser 1 ou mais.")
    if options.connection:
        store.find_connection(options.connection)
    if options.json and not options.summary:
        raise LauncherError("--json em logs só vale com --summary.")
    from agent_launcher.endpoint import registry as endpoints
    path = store.home / "endpoint.log"
    if not path.is_file():
        if options.json:
            print(json.dumps(endpoints.summarize_log(""), ensure_ascii=False, indent=2))
            return 0
        print(f"{path} ainda não existe: o endpoint local não registrou nenhum pedido neste perfil.")
        return 0
    lines = [endpoints.read_log(store.home, options.lines)]
    if options.summary:
        summary = endpoints.summarize_log(lines[0])
        lines = [json.dumps(summary, ensure_ascii=False, indent=2) if options.json else endpoints.summary_text(summary)]
    if options.connection:  # as linhas não trazem a conexão (só os 8 primeiros do id nas de pedido): não dá para filtrar
        print("o log é único por perfil; mostrando todas as linhas", file=sys.stderr)
    print("\n".join(lines))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=f"{APP_NAME}: Claude Code, Codex, Copilot CLI, VS Code e Copilot app em provedores compatíveis.")
    result.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    result.add_argument("--profile", metavar="ID_OU_NOME",
                        help="Perfil de trabalho (id ou nome); omitido usa o padrão")
    commands = result.add_subparsers(dest="action")
    profile_command = commands.add_parser("profiles", help="Perfis de trabalho: listar, criar, renomear, remover, ativar")
    profile_actions = profile_command.add_subparsers(dest="profile_action")
    profile_actions.add_parser("list", help="Listar os perfis (JSON)")
    profile_actions.add_parser("add", help="Criar um perfil").add_argument("name", metavar="NOME")
    profile_rename = profile_actions.add_parser("rename", help="Renomear um perfil")
    profile_rename.add_argument("reference", metavar="REF", help="Id ou nome do perfil")
    profile_rename.add_argument("name", metavar="NOME")
    profile_remove = profile_actions.add_parser("remove", help="Tirar um perfil da lista")
    profile_remove.add_argument("reference", metavar="REF", help="Id ou nome do perfil")
    profile_remove.add_argument("--delete-data", action="store_true",
                                help="apaga também a pasta de dados do perfil (projetos e conexões dele; nunca as "
                                     "pastas dos projetos)")
    profile_remove.add_argument("--clean-destinations", action="store_true",
                                help="remove também os providers das conexões do perfil no Copilot app e no VS Code e "
                                     "para/libera o endpoint local delas")
    profile_actions.add_parser("use", help="Tornar o perfil ativo na interface").add_argument(
        "reference", metavar="REF", help="Id ou nome do perfil")
    profile_import = profile_actions.add_parser("import", help="Criar um perfil a partir de um arquivo .alprofile")
    profile_import.add_argument("file", metavar="ARQUIVO")
    profile_export = profile_actions.add_parser("export", help="Exportar um perfil para .alprofile (sem chaves)")
    profile_export.add_argument("reference", metavar="REF", help="Id ou nome do perfil")
    profile_export.add_argument("file", metavar="ARQUIVO")
    check = commands.add_parser("doctor", help="Verificar CLIs e conexões (somente leitura)")
    check.add_argument("--connection")
    check.add_argument("--json", action="store_true",
                       help="as verificações em JSON (nome, estado ok/aviso/erro e mensagem de cada uma, mais o campo ok)")
    commands.add_parser("connectors", help="Listar os tipos de conector e seus campos")
    connections = commands.add_parser("connections", help="Gerenciar conexões com provedores")
    connections.add_argument("--set-fallback", metavar="NOME", help="Define a conexão reserva de NOME (use com --to)")
    connections.add_argument("--to", metavar="RESERVA", help="Conexão reserva usada por --set-fallback")
    connections.add_argument("--clear-fallback", metavar="NOME", help="Tira a conexão reserva de NOME")
    connections.add_argument("--endpoint-sessions", choices=("on", "off"),
                             help="Liga ou desliga as sessões de terminal pelo endpoint local da conexão (use com --connection)")
    connections.add_argument("--connection", metavar="NOME", help="Conexão de --endpoint-sessions")
    connection_actions = connections.add_subparsers(dest="connection_action")
    add_connection = connection_actions.add_parser("add", help="Adicionar ou atualizar uma conexão (testa antes de salvar)")
    add_connection.add_argument("--name", required=True)
    add_connection.add_argument("--connector", required=True)
    add_connection.add_argument("--set", action="append", default=[], metavar="CAMPO=VALOR")
    add_connection.add_argument("--secret-env", action="append", default=[], metavar="CAMPO=VARIAVEL")
    for name, help_text in (("test", "Testar a conexão"), ("remove", "Remover a conexão e sua chave"),
                            ("default", "Tornar a conexão padrão")):
        sub = connection_actions.add_parser(name, help=help_text)
        if name == "test":
            sub.add_argument("reference", nargs="?", help="Nome ou ID; omitido testa todas as conexões")
        else:
            sub.add_argument("reference")
        if name == "remove":
            sub.add_argument("--clean-destinations", action="store_true",
                             help="remove também o provider no Copilot app e no VS Code e para/libera o endpoint local")
    duplicate_connection = connection_actions.add_parser(
        "duplicate", help="Criar uma conexão nova com os campos de outra (a chave não é copiada: informe a da segunda conta)")
    duplicate_connection.add_argument("reference", metavar="CONEXAO", help="Id ou nome da conexão de origem")
    duplicate_connection.add_argument("--name", required=True, help="Nome da conexão nova")
    duplicate_connection.add_argument("--secret-env", action="append", default=[], metavar="CAMPO=VARIAVEL",
                                      help="a chave da conta nova, lida da variável de ambiente")
    export_connection = connection_actions.add_parser(
        "export", help="Exportar uma conexão para um arquivo .alprofile, sem a chave (para passar a um colega)")
    export_connection.add_argument("reference", metavar="CONEXAO", help="Id ou nome da conexão")
    export_connection.add_argument("file", metavar="ARQUIVO")
    connection_actions.add_parser(
        "import", help="Importar a conexão de um arquivo .alprofile; a chave fica por preencher").add_argument(
        "file", metavar="ARQUIVO")
    models = commands.add_parser("models", help="Listar IDs completos de modelos de uma conexão")
    models.add_argument("--connection")
    models.add_argument("--cli", choices=destinations())
    models.add_argument("--search", default="", help="Filtrar por trecho do ID")
    models.add_argument("--family", choices=CLAUDE_FAMILIES, help="Filtrar uma família Claude")
    models.add_argument("--refresh", action="store_true", help="Ignorar cache e exigir catálogo ao vivo")
    models.add_argument("--source", metavar="NOME",
                        help="Só modelos desta fonte, com o nome que o conector declarou; só vale em conexão cujo conector "
                             "declara fontes")
    models.add_argument("--capability", action="append", default=[], metavar="vision|audio|video|tools",
                        help="Só modelos com a capacidade confirmada (repetida ou separada por vírgula: todas)")
    models.add_argument("--sort", choices=("name", "context", "price"),
                        help="Ordem da lista, a do 'Ordenar' da interface: name (família e versão mais nova primeiro), "
                             "context (maior entrada primeiro) ou price (menor preço de entrada primeiro); o que o host "
                             "não informou vai para o fim. Sem a opção, a ordem do catálogo (ID)")
    models.add_argument("--json", action="store_true",
                        help="Os modelos em JSON, os mesmos itens do /v1/models do endpoint local")
    models.add_argument("words", nargs="*", metavar="test MODELO",
                        help="'test MODELO': testa o modelo com uma chamada real de 1 token; mostra o custo em tokens "
                             "antes e só chama com --yes ou com a confirmação no console")
    models.add_argument("--yes", action="store_true", help="Confirma a chamada real de 'models test'")
    projects = commands.add_parser("projects", help="Gerenciar os projetos cadastrados")
    actions = projects.add_subparsers(dest="project_action")
    add = actions.add_parser("add", help="Cadastrar uma pasta existente")
    add.add_argument("path")
    add.add_argument("--name")
    edit = actions.add_parser("edit", help="Renomear o projeto ou apontar para outra pasta")
    edit.add_argument("reference", help="Nome, ID ou caminho")
    edit.add_argument("--name")
    edit.add_argument("--path")
    edit.add_argument("--pin", action="store_true", help="Fixar o projeto no topo da lista")
    edit.add_argument("--unpin", action="store_true", help="Tirar o projeto do topo da lista")
    remove = actions.add_parser("remove", help="Remover cadastro, sem excluir a pasta")
    remove.add_argument("reference", help="Nome, ID ou caminho")
    actions.add_parser("list", help="Listar os projetos (JSON)")
    restore = actions.add_parser("restore", help="Devolver projetos removidos a partir de uma cópia diária")
    restore.add_argument("--date", metavar="AAAA-MM-DD", help="Cópia daquele dia; omitido usa a mais recente com projetos a devolver")
    legacy = actions.add_parser("import", help="Importar nomes e caminhos de outro config.json")
    legacy.add_argument("source", type=Path)
    export = actions.add_parser("export", help="Exportar nomes e caminhos para um arquivo que o import lê (sem chaves)")
    export.add_argument("file", type=Path, metavar="ARQUIVO")
    open_folder = actions.add_parser("open", help="Abrir a pasta do projeto no Explorer (--folder) ou no VS Code (--vscode)")
    open_folder.add_argument("reference", metavar="NOME", help="Nome, ID ou caminho")
    open_folder.add_argument("--folder", action="store_true", help="Abrir a pasta no Explorer")
    open_folder.add_argument("--vscode", action="store_true", help="Abrir a pasta no VS Code (o seu perfil normal)")
    discover = actions.add_parser("discover", help="Descobrir pastas de projeto no histórico de conversas do Claude Code, do "
                                                   "Codex e do Copilot CLI (JSON; só leitura sem --add)")
    discover.add_argument("--add", action="store_true",
                          help="Cadastrar, com o nome da pasta, as pastas descobertas que existem, ainda não estão "
                               "cadastradas e não são raiz de disco, pasta de usuário, do sistema nem worktree do launcher; "
                               "caminho não recuperado nunca é cadastrado")
    discover.add_argument("--cli", choices=terminal_destinations(), help="Só o histórico deste CLI")
    trees = commands.add_parser("worktrees", help="Worktrees do launcher no projeto (nome, branch, pasta, marcador; só "
                                                  "leitura, o launcher nunca remove)")
    trees.add_argument("--project", help="Nome, ID ou caminho de projeto cadastrado; omitido usa a pasta atual")
    trees.add_argument("--json", action="store_true", help="Em vez do texto, a lista em JSON")
    cli_command = commands.add_parser("cli", help="Ferramentas (Claude Code, Codex, Copilot CLI, VS Code, Copilot app): "
                                                  "situação e instalação por usuário, sem admin")
    cli_command.add_argument("--install", choices=(*clis.installable(), "all"),
                             help="instala a ferramenta que falta (instalador oficial, npm ou winget); "
                                  "all = os três CLIs de terminal")
    cli_command.add_argument("--update", choices=(*clis.NAMES, "all"),
                             help="atualiza o CLI instalado (o `update` da própria CLI, npm ou winget; adia com sessão aberta)")
    endpoint = commands.add_parser("endpoint", help="Endpoint local das conexões (VS Code e Copilot app): situação, "
                                                    "subir, parar, iniciar com o Windows")
    endpoint.add_argument("endpoint_action", nargs="?", choices=("status", "start", "stop", "autostart", "logs", "config"),
                          default="status")
    endpoint.add_argument("--shell", choices=("powershell", "cmd"), default="powershell",
                          help="com config: o shell das variáveis de ambiente (padrão powershell)")
    endpoint.add_argument("--json", action="store_true", help="com logs --summary: o resumo em JSON")
    endpoint.add_argument("--connection", help="Nome ou ID da conexão (padrão: todas no status, obrigatório nas outras; "
                                               "em logs só confere o nome: o log é um por perfil)")
    endpoint.add_argument("--off", action="store_true", help="com autostart: desliga")
    endpoint.add_argument("--lines", type=int, default=50, metavar="N", help="com logs: quantas linhas finais (padrão 50)")
    endpoint.add_argument("--summary", action="store_true", help="com logs: resume (pedidos, erros por modelo, failovers, latência) em vez de listar")
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
    preferences.add_argument("--cli-auto-update", choices=("on", "off"),
                             help="Atualiza Claude Code, Codex e Copilot CLI sozinho (adia com sessão aberta)")
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
                           "delete, um caractere (1, y) ou text:ALGO (cola ALGO de uma vez)")
    send.add_argument("--project", help="Nome, ID ou caminho do projeto da sessão")
    send.add_argument("--cli", choices=terminal_destinations(),
                      help="Desempata quando o projeto tem sessões de mais de um CLI abertas")
    send.add_argument("--pid", type=int, help="pid da janela da sessão (ver --list); dispensa --project")
    send.add_argument("--list", action="store_true", help="Listar as sessões abertas (JSON) e sair")
    send.add_argument("--screen", action="store_true", help="Mostrar o que a sessão exibe agora (antes das teclas)")
    send.add_argument("--wait", metavar="TEXTO",
                      help="Esperar este texto aparecer na tela da sessão antes de ler a tela e enviar as teclas; "
                           "passado o prazo sem o texto, erro e nenhuma tecla vai")
    send.add_argument("--timeout", type=float, metavar="SEGUNDOS",
                      help=f"Com --wait: prazo da espera (padrão {keys.WAIT_TIMEOUT:g} s)")
    usage_parser = commands.add_parser("usage", help="Uso do mês do perfil: tokens e custo estimado por destino e modelo "
                                                     "(JSON; --all soma os perfis, --tsv imprime o texto do 'Copiar relatório')")
    usage_parser.add_argument("--month", metavar="AAAA-MM", help="Mês (padrão: o atual)")
    usage_parser.add_argument("--all", action="store_true", help="soma de todos os perfis, no mesmo formato")
    usage_parser.add_argument("--months", type=int, metavar="N",
                              help="o total de cada um dos N últimos meses (1 a 24), do corrente para trás (JSON)")
    usage_parser.add_argument("--days", type=int, metavar="N",
                              help="o uso de cada um dos N últimos dias (1 a 366), do de hoje para trás, sem os dias sem "
                                   "uso (JSON; --tsv em tabela); não combina com --month, --months, --by-source, --all nem --limit")
    usage_parser.add_argument("--blocks", action="store_true",
                              help="os blocos de 5 horas de uso dos últimos 8 dias, como o ccusage blocks: cada bloco começa "
                                   "na hora cheia, no fuso local, da primeira requisição e dura 5 h; mostra início, fim, se "
                                   "está ativo (e o tempo restante), tokens e custo (JSON; --tsv em tabela). O custo é o "
                                   "equivalente de API, não é o da assinatura; o bloco sai dos carimbos das conversas, não do "
                                   "limite que a assinatura informa; não combina com --month, --months, --by-source, --all, "
                                   "--limit nem --days")
    usage_parser.add_argument("--active", action="store_true",
                              help="com --blocks: só o bloco em andamento (lista vazia se não há)")
    usage_parser.add_argument("--tsv", action="store_true",
                              help="o relatório em texto separado por tabulação, o mesmo que a interface copia")
    usage_parser.add_argument("--by-source", action="store_true",
                              help="o custo do mês por fonte, como o conector da conexão o informa (JSON; --tsv em tabela); "
                                   "só vale em conexão cujo conector declara fontes e oferece a rota de custo")
    usage_parser.add_argument("--connection", help="Com --by-source: nome ou ID da conexão (padrão: a do projeto ou a padrão)")
    usage_parser.add_argument("--limit", metavar="VALOR|off",
                              help="teto de gasto do mês deste perfil, na moeda do relatório (100 ou 100,50); off desliga. "
                                   "A janela avisa a 80%% e a 100%% e nada é bloqueado")
    state = commands.add_parser("status", help="Retrato único em JSON: CLIs, conexões (cota), endpoint, sessões abertas, "
                                               "projetos e uso do mês; sem chaves")
    state.add_argument("--month", metavar="AAAA-MM", help="Mês do uso (padrão: o atual)")
    update = commands.add_parser("update", help="Atualizar agora pela última versão publicada no registry")
    update.add_argument("--check", action="store_true", help="só mostra se há versão nova, sem instalar")
    talks = commands.add_parser("conversations", help="Conversas dos CLIs de terminal na pasta do projeto (JSON, mais "
                                "recente primeiro; pasta em outro perfil: só as deste perfil)")
    talks.add_argument("--project", help="Nome, ID ou caminho de projeto cadastrado; omitido usa a pasta atual")
    talks.add_argument("--cli", choices=terminal_destinations())
    talks.add_argument("--add-here", action="store_true", help=ADD_HERE_HELP)
    talks.add_argument("--search", metavar="TEXTO", default="",
                       help="Só as conversas com o texto no título, no CLI ou na data (sem distinguir maiúsculas)")
    talks.add_argument("--grep", metavar="TEXTO", default=None,
                       help="Só as conversas com o texto em alguma mensagem (a sua e a do modelo, sem distinguir "
                            "maiúsculas; metadados e resultados de ferramenta não contam). Lê os transcripts, só leitura, "
                            "com teto de 30 s no total e 64 MiB por conversa; combina com --search, --cli, --tsv e --command")
    talks.add_argument("--tsv", action="store_true",
                       help="Em vez do JSON, o texto separado por tabulação que 'Copiar lista' da janela de conversas "
                            "copia (traço no que não se sabe)")
    talks.add_argument("--command", metavar="ID_OU_POSIÇÃO",
                       help="Em vez da lista, imprimir o comando que retoma esta conversa fora do launcher, com a "
                            "pasta, o ambiente e o modelo da conexão; a chave nunca vai no texto. Posição: 1 é a mais "
                            "recente (depois do filtro --cli)")
    talks.add_argument("--shell", choices=RESUME_SHELLS,
                       help="Com --command: o shell em que o texto será colado (padrão: cmd)")
    talks.add_argument("--connection", help="Com --command: nome ou ID; omitido usa a última do projeto ou a padrão")
    talks.add_argument("--model", help="Com --command: ID completo; omitido usa o último modelo desse CLI/projeto/conexão")
    complete = commands.add_parser("completion", help="Completar com Tab: 'powershell' imprime o script para o perfil; "
                                   "'projects' e 'connections' listam os nomes, um por linha")
    complete.add_argument("kind", choices=("powershell", "projects", "connections"))
    recent = commands.add_parser("sessions", help="Sessões das últimas horas: --reopen reabre as que não estão abertas, "
                                                  "cada uma na conversa exata, como abas da janela única")
    recent.add_argument("--reopen", action="store_true", help="Reabrir as sessões das últimas horas que não estão abertas")
    recent.add_argument("--hours", type=float, metavar="N",
                        help=f"Com --reopen: quantas horas atrás contar as aberturas (padrão {reopen.RECENT_HOURS})")
    recent.add_argument("--dry-run", action="store_true",
                        help="Com --reopen: só listar o que abriria, em JSON (linhas sem conversa identificada vêm com o motivo)")
    open_session.add_parser(commands)
    session = commands.add_parser("launch", help="Iniciar um CLI no terminal atual (ou abrir VS Code / Copilot app)")
    session.add_argument("--project", help="Nome, ID ou caminho de projeto cadastrado; omitido usa a pasta atual")
    session.add_argument("--connection", help="Nome ou ID; omitido usa a última do projeto ou a padrão")
    session.add_argument("--cli", choices=destinations())
    session.add_argument("--add-here", action="store_true", help=ADD_HERE_HELP)
    session.add_argument("--model", help="ID completo; omitido usa o último modelo desse CLI/projeto/conexão")
    session.add_argument("--resume", action="store_true", help="Retomar a última sessão do CLI no projeto")
    session.add_argument("--conversation", metavar="ID", help="Com --resume: retomar esta conversa (veja 'conversations')")
    session.add_argument("--fork", action="store_true",
                         help="Com --resume e --conversation: retomar como cópia, numa conversa nova (Claude Code e Codex)")
    session.add_argument("--dry-run", action="store_true", help="Exibir argumentos sem rede, sem salvar e sem iniciar CLI")
    session.add_argument("--command", action="store_true",
                         help="Com --dry-run: em vez do JSON, imprimir o comando que abre esta sessão fora do launcher, "
                              "com a pasta, o ambiente e o modelo da conexão; a chave nunca vai no texto")
    session.add_argument("--shell", choices=RESUME_SHELLS,
                         help="Com --command: o shell em que o texto será colado (padrão: cmd)")
    session.add_argument("--controlled", action="store_true", help="Sessão controlada por orquestrador (implica bypass)")
    session.add_argument("--worktree", nargs="?", const="", metavar="NOME",
                         help="Abrir a sessão num worktree git do projeto (Claude Code, Codex e Copilot CLI): pasta irmã "
                              "<projeto>.worktrees/NOME, branch launcher/NOME, criado se não existir e reaproveitado se "
                              "já existe; sem NOME, sessao-AAAAMMDD-HHMMSS. Nunca remove nada (git worktree remove)")
    return result


def describe_cli_version(found: dict) -> str:
    text = f"versão {found['installed'] or 'não lida'}"
    if not found["installed"] and not found["error"]:
        return text + " (a CLI não respondeu a --version)"
    if found["error"]:
        return f"{text} (última publicada não consultada: {found['error']})"
    if not found["newer"]:
        return f"{text} (em dia)"
    return f"{text} → {found['latest']} disponível" + (
        f"; {found['running']} sessão(ões) aberta(s) seguem na versão antiga até reabrir" if found["running"] else "")


def update_clis(targets) -> int:
    """`launcher cli --update`: atualiza e conta o resultado de cada CLI; arquivo preso adia sem contar como falha."""
    status = 0
    for cli in targets:
        if not clis.find(cli):
            print(f"{clis.NAMES[cli]}: não instalado")
            continue
        try:
            outcome = cli_update.update(cli)
        except cli_update.UpdateDeferred as error:
            print(error)
        except (CliInstallError, updater.UpdateError, OSError, subprocess.SubprocessError) as error:
            print(f"{clis.NAMES[cli]}: {error}", file=sys.stderr)
            status = 1
        else:
            print(f"{clis.NAMES[cli]}: {outcome['before']} → {outcome['after']}"
                  + (f"; {outcome['sessions']} sessão(ões) aberta(s) seguem na versão antiga até reabrir" if outcome["sessions"] else "")
                  if outcome["updated"]
                  else f"{clis.NAMES[cli]}: já está em dia ({outcome['after']})")
    return status


def main(arguments: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    options = parser().parse_args(arguments)
    prune_old_versions()
    cleanup_status = 0  # 1 quando a remoção de conexão funcionou mas um passo da limpeza falhou
    try:
        if options.action == "profiles":
            return profiles_command(options)
        store = Store(profiles.home_of(profiles.find(options.profile)["id"])) if options.profile else Store()
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
            return doctor(store, options.connection, options.json)
        if options.action == "endpoint":
            return endpoint_command(store, options)
        if options.action == "usage":
            return usage_command(store, options)
        if options.action == "status":
            return status_command(store, options)
        if options.action == "worktrees":
            return worktrees_command(store, options)
        if options.action == "connectors":
            modules, errors = store.connectors()
            for module in modules.values():
                fields = ", ".join(item["name"] + (" (secreto)" if item.get("secret") else "") for item in module.FIELDS)
                print(f"{module.NAME:22} {module.LABEL} — {getattr(module, 'HINT', '')}\n{'':22} campos: {fields}")
            for error in errors:
                print(f"[AVISO] {error}", file=sys.stderr)
        elif options.action == "connections":
            action = options.connection_action
            if options.set_fallback or options.clear_fallback or options.to:
                set_fallback_command(store, options)
            elif options.endpoint_sessions or options.connection:
                endpoint_sessions_command(store, options)
            elif action == "add":
                modules, _errors = store.connectors()
                if options.connector not in modules:
                    raise LauncherError(f"Conector desconhecido: {options.connector}. Veja 'connectors'.")
                values = parse_values(modules[options.connector], options.set, options.secret_env)
                existing = next((c["id"] for c in store.connections if c["name"].casefold() == options.name.casefold()), None)
                connection = store.save_connection(options.name, options.connector, values, existing)
                print(f"Conexão {'atualizada' if existing else 'salva'}: {connection['name']}")
                status = connection.get("status") or {}
                if status.get("state") == "quota_exhausted":
                    print(f"AVISO: cota esgotada, a conexão não está funcionando agora. {status.get('message', '')}")
            elif action == "duplicate":
                source = store.find_connection(options.reference)
                modules, _errors = store.connectors()
                if source["connector"] not in modules:
                    raise LauncherError(f"Conector desconhecido: {source['connector']}. Veja 'connectors'.")
                values = {**source.get("options", {}), **parse_values(modules[source["connector"]], [], options.secret_env, prompt=False)}
                connection = store.save_connection(options.name, source["connector"], values)
                print(f"Conexão criada a partir de {source['name']}: {connection['name']}")
            elif action == "test":
                if options.reference:
                    Gateway(store, store.find_connection(options.reference)).verify()
                    print("Conexão respondeu.")
                else:
                    cleanup_status |= test_all_connections(store)
            elif action == "remove":
                if options.clean_destinations:
                    from agent_launcher.harnesses.cleanup import cleanup_destinations
                    cleanup_status |= report_cleanup(cleanup_destinations(
                        store, store.find_connection(options.reference), confirm=confirm_flagged))
                from agent_launcher.harnesses.cleanup import release_endpoint
                cleanup_status |= report_cleanup(release_endpoint(store.home, store.find_connection(options.reference)["id"]))
                store.remove_connection(options.reference)
                print("Conexão removida, com a chave salva.")
            elif action == "default":
                store.set_default_connection(options.reference)
                print("Conexão padrão definida.")
            elif action == "export":
                from agent_launcher.domain import profile_package
                result = profile_package.export_connection(store, options.reference, Path(options.file))
                print(f"Conexão {result['connection']} exportada sem a chave em {result['path']}" +
                      (f", com o conector {', '.join(result['connectors'])}" if result["connectors"] else "") + ".")
            elif action == "import":
                from agent_launcher.domain import profile_package
                print(profile_package.describe_connection(
                    profile_package.import_connection(store, Path(options.file), ask=ask_connection_file)))
            else:
                show_connections(store)
        elif options.action == "models":
            return models_command(store, options)
        elif options.action == "projects":
            if options.project_action == "add":
                project = store.add(options.path, options.name)
                print(f"Projeto cadastrado: {project['name']}\n{project['path']}")
                warn_shared(store, project)
            elif options.project_action == "edit":
                if options.pin and options.unpin:
                    raise LauncherError("Use --pin ou --unpin, não os dois.")
                if options.name is None and options.path is None and not (options.pin or options.unpin):
                    raise LauncherError("Informe --name, --path, --pin e/ou --unpin.")
                if options.name is None and options.path is None:
                    project = store.find(options.reference)
                else:
                    project = store.update(options.reference, options.name, options.path)
                if options.pin or options.unpin:
                    store.set_pinned(project, options.pin)
                print(f"Projeto atualizado: {project['name']}\n{project['path']}"
                      + ("\nFixado no topo da lista." if options.pin else "\nFora do topo da lista." if options.unpin else ""))
            elif options.project_action == "remove":
                store.remove(options.reference)
                print("Cadastro removido. A pasta do projeto foi preservada.")
            elif options.project_action == "import":
                import_projects(store, options.source)
            elif options.project_action == "export":
                print(f"{store.export_projects(options.file)} projeto(s) exportado(s) para {options.file}.")
            elif options.project_action == "open":
                open_project_command(store, options)
            elif options.project_action == "discover":
                discover_projects_command(store, options)
            elif options.project_action == "list":
                print(projects_json(store))
            elif options.project_action == "restore":
                for project in store.restore_removed(options.date):
                    print(f"Projeto restaurado: {project['name']}\n{project['path']}")
            else:
                show_projects(store)
        elif options.action == "cli":
            if options.install:
                targets = tuple(clis.NAMES) if options.install == "all" else (options.install,)
                return 0 if all(clis.ensure(cli) for cli in targets) else 1
            if options.update:
                return update_clis(tuple(clis.NAMES) if options.update == "all" else (options.update,))
            for cli in clis.installable():
                print(f"{clis.label(cli)}: {clis.locate(cli) or 'não instalado'}")
                found = cli_update.check(cli) if cli in clis.NAMES and clis.find(cli) else None
                if found:
                    print("  " + describe_cli_version(found))
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
            if options.cli_auto_update:
                store.set_preference("cli_auto_update", options.cli_auto_update == "on")
            if options.sessions_monitor:
                store.set_preference("sessions_secondary_monitor", options.sessions_monitor == "other")
            print(f"Bypass marcado por padrão: {'sim' if store.preference('bypass_default') else 'não'}")
            print(f"Atualização automática: {'sim' if store.preference('auto_update') else 'não'}")
            print(f"Atualização automática das CLIs: {'sim' if store.preference('cli_auto_update') else 'não'}")
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
            return keys_command(store, options)
        elif options.action == "update":
            try:
                if options.check:
                    meta = updater.latest()
                    print(f"Instalada: {VERSION} · publicada no registry: {meta['version']}"
                          + (" (há versão nova)" if updater.newer(meta) else " (em dia)"))
                    return 0
                updater.update(ROOT, log=print)
            except updater.UpdateError as error:
                print(f"Atualização falhou: {error}")
                return 1
        elif options.action == "launch":
            project, added = resolve_project(store, options)
            gateway = Gateway.for_project(store, project, options.connection)
            last = store.last_cli(project)
            cli = options.cli or (last if last in gateway.clis else sorted(gateway.clis)[0])
            model = options.model or store.last_model(project, gateway.connection, cli)
            if not model:
                raise LauncherError("Informe --model com um ID completo, ou escolha um modelo pela interface primeiro.")
            if options.conversation and not options.resume:
                raise LauncherError("--conversation só vale com --resume.")
            if options.fork and not (options.resume and options.conversation):
                raise LauncherError("--fork pede --resume e --conversation ID.")
            if options.command and not options.dry_run:
                raise LauncherError("--command só vale com --dry-run: o launcher só imprime o comando, não abre a sessão.")
            if options.shell and not options.command:
                raise LauncherError("--shell só vale com --command.")
            require_terminal_for_worktree(cli, options.worktree)
            register_added(store, project, added, save=not options.dry_run)
            if options.resume:
                warn_shared(store, project, resume=True)
            return launch(store, project, gateway, cli, model, options.resume, options.dry_run, options.controlled,
                          options.conversation, (options.shell or "cmd") if options.command else None,
                          options.worktree, options.fork)
        elif options.action == "sessions":
            return sessions_command(store, options)
        elif options.action == "session":
            return open_session.command(store, options)
        elif options.action == "completion":
            if options.kind == "powershell":
                print(completion.powershell_script(parser()), end="")
            else:
                items = store.projects if options.kind == "projects" else store.connections
                print("\n".join(item["name"] for item in items))
        elif options.action == "conversations":
            if options.grep is not None and not options.grep.strip():
                raise LauncherError("--grep pede o texto a procurar nas mensagens; um valor vazio não filtra nada.")
            project, added = resolve_project(store, options)
            if options.command:
                if options.tsv:
                    raise LauncherError("--tsv lista as conversas; não combina com --command, que imprime um comando só.")
                output = conversation_command(store, project, options.command, options.cli, options.connection,
                                              options.model, options.shell or "cmd", options.grep)
            elif options.connection or options.model or options.shell:
                raise LauncherError("--connection, --model e --shell só valem com --command.")
            elif options.tsv:
                output = folder_conversations_tsv(store, project, options.cli, options.search, options.grep)
            else:
                output = json.dumps(list_conversations(store, project, options.cli, options.search, options.grep),
                                    ensure_ascii=False, indent=2)
            register_added(store, project, added)
            print(output)
        return cleanup_status
    except (LauncherError, OSError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nLauncher encerrado.")
        return 130


AUTOMATION_ACTIONS = ("keys",)


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


def script_main() -> int:
    """launcher.py da raiz: espera ENTER só quando o console foi aberto para esta execução. Os comandos de AUTOMATION_ACTIONS
    são chamados por script: o erro deles sai direto, sem pausa."""
    exit_code = main()
    if exit_code not in (0, 130) and parser().parse_args().action not in AUTOMATION_ACTIONS and console_of_its_own():
        try:
            input("\nPressione ENTER para fechar...")
        except (EOFError, KeyboardInterrupt):
            pass
    return exit_code
