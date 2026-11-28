"""`launcher session open`: abre a janela da sessão (aba da janela única do Windows Terminal) pelo mesmo `start_session` que
o botão Nova sessão / Retomar da interface Qt chama, para uma interface sem Python em processo (a Avalonia) pedir a
abertura sem reimplementar a regra de janela. A camada é fina: valida o pedido com a mensagem que a pessoa vê, abre em
primeiro plano como o botão e só então grava a última escolha do projeto (a mesma que o Qt grava); se essa gravação
falha com a janela já aberta, o resultado continua `opened: true` e traz o motivo em `warnings`, para ninguém repetir o
pedido e abrir uma segunda aba. `--dry-run` faz as mesmas validações e devolve o plano, sem abrir nada e sem gravar.

Também dá a `projects list` e ao `status` a CLI, a conexão e os modelos lembrados de cada projeto (`remembered`)."""
from __future__ import annotations

import json
from pathlib import Path

from agent_launcher.domain.catalog import base_model
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import project_path
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import installers as clis
from agent_launcher.harnesses import registry
from agent_launcher.session.conversations import ID_PATTERN, conversations_for, has_conversation_for, lists_conversations
from agent_launcher.session.preview import plan_report, planned


def add_parser(commands) -> None:
    """Registra `session open` no subparser de comandos do modo texto."""
    session = commands.add_parser("session", help="Janela da sessão: 'open' abre a sessão numa aba do Windows Terminal, "
                                                  "como os botões Nova sessão e Retomar da interface")
    actions = session.add_subparsers(dest="session_action", required=True)
    opened = actions.add_parser("open", help="Abrir a janela de uma sessão nova ou retomada (JSON no stdout)")
    opened.add_argument("--project", required=True, help="Nome, ID ou caminho de projeto cadastrado")
    opened.add_argument("--cli", help="Destino de terminal (claude, codex, copilot, aider); omitido usa o último do projeto")
    opened.add_argument("--connection", help="Nome ou ID; omitido usa a última do projeto ou a padrão")
    opened.add_argument("--model", help="ID completo; omitido usa o último modelo desse CLI/projeto/conexão")
    opened.add_argument("--resume", action="store_true", help="Retomar a última conversa do CLI na pasta do projeto")
    opened.add_argument("--conversation", metavar="ID", help="Com --resume: retomar esta conversa (veja 'conversations')")
    opened.add_argument("--dry-run", action="store_true",
                        help="Fazer as mesmas validações e devolver o plano, sem abrir a janela e sem gravar nada")


def remembered(store: Store, project: dict) -> dict:
    """O que o projeto lembra da última abertura, sem segredos: `last_cli` (o cartão inicial), `last_connection` (id; None
    se nunca abriu ou a conexão não existe mais) e `last_models` ({id da conexão: {destino: modelo}}, só o que existe)."""
    connections = store.data["connections"]
    known = {connection["id"] for connection in connections}
    models = {}
    for connection in connections:
        chosen = {harness.ID: model for harness in registry.all()
                  if (model := store.last_model(project, connection, harness.ID))}
        if chosen:
            models[connection["id"]] = chosen
    last = project.get("last_connection")
    return {"last_cli": store.last_cli(project), "last_connection": last if last in known else None,
            "last_models": models}


def _first_terminal(gateway: Gateway, preferred: str) -> str:
    """O destino de terminal que a conexão atende: o último do projeto, senão o primeiro do registro."""
    served = [harness.ID for harness in registry.terminals() if harness.ID in gateway.clis]
    if not served:
        raise LauncherError(f"A conexão {gateway.name} não atende nenhum CLI de terminal. Escolha outra conexão.")
    return preferred if preferred in served else served[0]


def _checked_conversation(store: Store, harness, directory: Path, resume: bool, conversation: str | None) -> None:
    """Retomar sem conversa para retomar, ou uma conversa que não é desta pasta, é erro: o plano abriria uma sessão nova
    em silêncio, e quem clicou em Retomar acharia que retomou."""
    if conversation and not resume:
        raise LauncherError("--conversation só vale com --resume.")
    if not resume:
        return
    if conversation:
        if not ID_PATTERN.fullmatch(conversation):
            raise LauncherError(f"ID de conversa inválido: {conversation}")
        if lists_conversations(harness) and not any(
                item.id == conversation for item in conversations_for(store.home, harness, directory)):
            raise LauncherError(f"Conversa {conversation} não encontrada nesta pasta ({harness.LABEL}); veja 'conversations'.")
    elif not has_conversation_for(store.home, harness.ID, directory):
        raise LauncherError(f"Nenhuma conversa anterior do {harness.LABEL} nesta pasta para retomar.")


def open_session(store: Store, options) -> dict:
    """Valida o pedido, abre a janela (salvo `dry_run`) e devolve o resultado em dicionário. Nada abre nem se grava
    enquanto alguma validação falha."""
    from agent_launcher.session.window import session_title, start_session
    project = store.find(options.project)
    directory = project_path(project["path"])
    gateway = Gateway.for_project(store, project, options.connection)
    cli = options.cli or _first_terminal(gateway, store.last_cli(project))
    harness = registry.get(cli)
    if harness.KIND != "terminal":
        raise LauncherError(f"O {harness.LABEL} não abre em janela de terminal; use a interface.")
    if cli not in gateway.clis:
        raise LauncherError(f"A conexão {gateway.name} não atende o {harness.LABEL}. Escolha outra conexão.")
    if clis.locate(cli) is None:
        hint = harness.MISSING_HINT or f"instale com 'launcher cli --install {cli}'"
        raise LauncherError(f"{harness.LABEL} não instalado: {hint}.")
    model = options.model or store.last_model(project, gateway.connection, cli)
    if not model:
        raise LauncherError("Informe --model com um ID completo, ou escolha um modelo pela interface primeiro.")
    _checked_conversation(store, harness, directory, options.resume, options.conversation)
    try:
        catalog = gateway.catalog(offline=True)
    except LauncherError as error:
        catalog = gateway.empty_catalog(status=str(error))
    plan = planned(store.home, gateway, str(directory), cli, model, options.resume, catalog,
                   conversation=options.conversation)
    result = {"ok": True, "opened": False, "dry_run": bool(options.dry_run), "project": project["name"],
              "folder": str(directory), "cli": cli, "connection": gateway.name, "connection_id": gateway.connection["id"],
              "model": model, "resume": bool(options.resume), "conversation": options.conversation,
              "title": session_title(project, cli, store.home), "warnings": list(plan.warnings)}
    if options.dry_run:
        return {**result, "plan": plan_report(plan, gateway, options.resume, catalog)}
    start_session(store, project, cli, model, options.resume, connection=gateway.connection["id"],
                  conversation=options.conversation)
    try:
        store.remember(project, gateway.connection, cli, base_model(model))
    except (LauncherError, OSError, ValueError) as error:
        result["warnings"].append(f"A janela abriu, mas não foi possível salvar a preferência: {error}")
    return {**result, "opened": True}


def command(store: Store, options) -> int:
    """`launcher session open`: o resultado de `open_session` em JSON."""
    print(json.dumps(open_session(store, options), ensure_ascii=False, indent=2))
    return 0
