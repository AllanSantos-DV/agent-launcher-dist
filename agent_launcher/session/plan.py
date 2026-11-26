"""Plano de sessão: genérico (projeto, conexão, modelo, ambiente limpo, avisos); o específico de cada destino fica no
adaptador dele (harnesses/). Puro: sem rede e sem gravar nada."""
from __future__ import annotations

import os
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from agent_launcher.connectors._http import NO_KEY, is_local_host
from agent_launcher.domain import cache
from agent_launcher.domain.catalog import Catalog, ModelInfo, base_model
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import project_path, validate_model
from agent_launcher.session.controlled import without_parent_session

# Credenciais e endereços de provider que o processo herdaria do usuário: saem do ambiente de toda sessão, para a
# sessão usar só a conexão escolhida. Cada adaptador acrescenta as variáveis do seu CLI (Harness.SCRUB).
SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_OAUTH", "CLAUDE_CODE_USE_")
SCRUB_NAMES = frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY", "AGENT_LAUNCHER_API_KEY"})


@dataclass(frozen=True)
class EndpointRoute:
    """Sessão pelo endpoint local da conexão: a pasta de dados e a conexão que o sobem, e a porta que o plano usa."""
    home: Path
    connection_id: str
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class LaunchPlan:
    cli: str
    model: str
    directory: Path
    command: list[str]
    environment: dict[str, str]
    overrides: dict[str, str]
    input_limit: int | None = None
    context_window: int | None = None
    auto_compact_limit: int | None = None
    families: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    controlled: bool = False
    connection: str = ""
    secret_variables: tuple = ()
    session_id: str | None = None   # id da conversa conhecido na abertura (Claude novo: --session-id; retomada por id)
    resume: bool = False            # retomada de fato (retomar sem conversa vira sessão nova)
    vision: bool | None = None      # o conector diz se o modelo aceita imagem; None = não informou
    window_pid: int | None = None   # primeiro processo da janela, quando não é este (janela nativa); None = este processo
    scrubbed: tuple[str, ...] = ()  # nomes das variáveis herdadas que saem do ambiente da sessão
    endpoint: EndpointRoute | None = None  # a sessão vai pelo endpoint local, não direto ao host
    worktree: agent_launcher.session.worktree.Worktree | None = None  # a sessão abre num worktree do projeto (`launch --worktree`); `directory` é a pasta dele
    fork: bool = False              # retomada como cópia da conversa (`launch --resume --conversation ID --fork`)

    @property
    def public_overrides(self) -> dict[str, str]:
        """Overrides para exibir (dry-run/diagnóstico): a chave real nunca aparece."""
        return {key: ("<chave>" if key in self.secret_variables and value and value != NO_KEY else value)
                for key, value in self.overrides.items()}


@dataclass
class LaunchContext:
    """Tudo o que o adaptador precisa para montar o plano dele."""
    gateway: object
    catalog: Catalog
    selected: ModelInfo
    requested: str
    model: str
    directory: Path
    resume: bool
    bypass_permissions: bool
    controlled: bool
    preserve_terminal_title: bool
    admin_note: str | None
    command: list[str]
    environment: dict
    warnings: list[str]
    input_limit: int | None
    conversation: str | None = None  # id da conversa a retomar (None: a última, --continue/resume --last)
    endpoint: EndpointRoute | None = None  # preferência da conexão ligada: o adaptador aponta a sessão para o endpoint
    fork: bool = False  # retomar a conversa como cópia: a original fica como está e a sessão abre numa conversa nova


def uses_endpoint(gateway, harness) -> bool:
    """A conexão pede as sessões de terminal pelo endpoint local e o adaptador as aceita?"""
    return harness.ENDPOINT_SESSIONS and gateway.store.endpoint_sessions_of(gateway.connection)


def endpoint_route(gateway, harness) -> EndpointRoute | None:
    """O destino e a porta do endpoint local quando a sessão vai por ele; None é o caminho direto ao host. Só consulta a
    porta, sem reservá-la nem subir nada (quem abre sobe, em `Harness.ensure_endpoint`)."""
    if not uses_endpoint(gateway, harness):
        return None
    from agent_launcher.endpoint import registry as endpoints
    store, connection_id = gateway.store, gateway.connection["id"]
    return EndpointRoute(store.home, connection_id, endpoints.port_for(store.home, connection_id, reserve=False))


def build_plan(gateway, cli: str, directory: str, model: str, resume: bool = False,
               catalog: Catalog | None = None, preserve_terminal_title: bool = False,
               bypass_permissions: bool = False, controlled: bool = False, admin_note: str | None = None,
               conversation: str | None = None, worktree: agent_launcher.session.worktree.Worktree | None = None, fork: bool = False) -> LaunchPlan:
    """O plano da sessão. Com `worktree`, a sessão roda na pasta dele (que pode ainda não existir, no `--dry-run`) em vez
    da de `directory`. Com `fork` (exige `resume` e `conversation`), a conversa é retomada como cópia: o CLI cria uma
    conversa nova a partir dela e a original não é tocada."""
    from agent_launcher.harnesses import registry
    harness = registry.get(cli)
    bypass_permissions = bypass_permissions or controlled
    if controlled and "controlled" not in harness.CAPABILITIES:
        raise LauncherError(f"O {harness.LABEL} não tem sessão controlada pelo orquestrador.")
    if cli not in gateway.clis:
        raise LauncherError(f"A conexão {gateway.name} não atende o {cli}. Escolha outra conexão ou o outro CLI.")
    if fork:
        if "fork" not in harness.CAPABILITIES:
            raise LauncherError(f"O {harness.LABEL} não tem retomar como cópia.")
        if controlled and not harness.FORK_CONTROLLED:
            raise LauncherError(f"O {harness.LABEL} não retoma como cópia em sessão controlada pelo orquestrador.")
        if not (resume and conversation):
            raise LauncherError("--fork pede --resume e --conversation ID: a cópia sai de uma conversa escolhida.")
    model = validate_model(model)
    catalog = catalog or gateway.empty_catalog()
    listed = catalog.find(model)
    selected = listed or ModelInfo(base_model(model))
    if not catalog.allows(selected, cli):
        raise LauncherError(f"O modelo {selected.id} não é compatível com o {cli} nesta conexão. Escolha um ID da lista.")
    model = harness.model_id(model, selected, catalog)
    input_limit = selected.max_input_tokens if catalog.verified else None
    warnings = []
    if bypass_permissions:
        warnings.append("Bypass ativo: comandos sem confirmação de permissões. No Codex, o sandbox também está desativado.")
    if input_limit is None:
        warnings.append("Janela de entrada não confirmada; não serão aplicados aumentos de contexto a este modelo.")
    if catalog.records and not catalog.any_model and listed is None:
        warnings.append(f"O modelo {selected.id} não está no catálogo de {gateway.name}; o host pode tê-lo aposentado. "
                        "Escolha outro modelo da lista.")
    path = worktree.path if worktree else project_path(directory)
    if resume:
        from agent_launcher.session.conversations import resume_target
        resume, conversation = resume_target(getattr(getattr(gateway, "store", None), "home", None), harness, path,
                                             conversation, warnings)
        fork = fork and resume
    else:
        conversation = None
    command = harness.command()
    environment = without_parent_session(dict(os.environ))
    scrubbed = set(SCRUB_NAMES | harness.SCRUB)
    for key in list(environment):
        upper = key.upper()
        if upper.startswith(SCRUB_PREFIXES + harness.SCRUB_PREFIXES) or upper in SCRUB_NAMES | harness.SCRUB:
            environment.pop(key)
            scrubbed.add(upper)
    route = endpoint_route(gateway, harness)
    context = LaunchContext(gateway, catalog, selected, model, model, path, resume, bypass_permissions, controlled,
                            preserve_terminal_title, admin_note, command, environment, warnings, input_limit, conversation,
                            route, fork)
    plan = harness.plan(context)
    plan.endpoint = route
    plan.worktree = worktree
    plan.resume = resume
    plan.fork = fork
    plan.scrubbed = tuple(sorted(scrubbed))
    if resume and conversation and not fork:
        plan.session_id = conversation
    environment.update(plan.overrides)
    local_hosts = ["127.0.0.1", "localhost"]
    host = urllib.parse.urlparse(harness.target_url(context) or "").hostname or ""
    if host and is_local_host(host):
        local_hosts.append(host)
    bypass = environment.get("NO_PROXY", environment.get("no_proxy", ""))
    bypass = ",".join(dict.fromkeys([value.strip() for value in bypass.split(",") if value.strip()] + local_hosts))
    environment["NO_PROXY"] = bypass
    environment["no_proxy"] = bypass
    plan.environment = environment
    return plan


def quota_notice(gateway, quota: str, cli: str | None = None) -> str:
    """Aviso de cota esgotada da abertura. Com conexão reserva configurada, diz qual é; a sessão direta ao host nunca
    troca de conexão sozinha, porque isso mudaria quem cobra, mas a que vai pelo endpoint local (`cli` diz o destino) tem o
    failover dele. `quota` vazio (cota disponível) não gera aviso."""
    if not quota:
        return ""
    reserve = gateway.store.fallback_of(gateway.connection)
    if reserve is None:
        return quota
    from agent_launcher.harnesses import registry
    harness = registry.find(cli) if cli else None
    if harness and uses_endpoint(gateway, harness):
        return (f"{quota} • Conexão reserva configurada: {reserve['name']}. Esta sessão passa pelo endpoint local, que "
                "refaz os pedidos na reserva enquanto a cota estiver esgotada.")
    return (f"{quota} • Conexão reserva configurada: {reserve['name']}. Esta sessão não troca de conexão sozinha; "
            f"para usar a reserva, abra a sessão pela conexão {reserve['name']}.")


def has_previous_conversation(cli: str, directory: Path) -> bool:
    """Existe conversa desta pasta para o destino retomar (--continue do Claude, resume --last do Codex...)?"""
    from agent_launcher.harnesses import registry
    harness = registry.find(cli)
    return bool(harness and harness.has_previous_conversation(Path(directory)))


def session_command(harness, plan: LaunchPlan) -> tuple[list[str], str]:
    """Comando final do plano, (comando, aviso). Os arquivos que o adaptador precisa (`session_files_key`) ficam na
    pasta estável do cache, uma por chave: a mesma entrada acha a mesma pasta e nada novo nasce. Sem chave, nada é gravado."""
    key = harness.session_files_key(plan)
    if key is None:
        return harness.final_command(plan, None)
    cache.prune_old_versions()
    with cache.keyed_folder("sessao", key) as directory:
        return harness.final_command(plan, directory)


def run_plan(plan: LaunchPlan) -> int:
    from agent_launcher.harnesses import registry
    harness = registry.get(plan.cli)
    try:
        command, warning = session_command(harness, plan)
        if warning:
            print(f"  Aviso: {warning}")
        process = subprocess.Popen(command, cwd=plan.directory, env=plan.environment, shell=False)
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                continue
    except OSError as error:
        raise LauncherError(f"Não foi possível iniciar {plan.cli}: {error.strerror or 'erro do sistema'}.") from error
