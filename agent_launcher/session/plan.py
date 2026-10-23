"""Plano de sessão: genérico (projeto, conexão, modelo, ambiente limpo, avisos); o específico de cada destino fica no
adaptador dele (harnesses/). Puro: sem rede e sem gravar nada."""
from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from agent_launcher.connectors._http import NO_KEY, is_local_host
from agent_launcher.domain.catalog import Catalog, ModelInfo, base_model
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import project_path, validate_model
from agent_launcher.session.controlled import without_parent_session

# Credenciais e endereços de provider que o processo herdaria do usuário: saem do ambiente de toda sessão, para a
# sessão usar só a conexão escolhida. Cada adaptador acrescenta as variáveis do seu CLI (Harness.SCRUB).
SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_OAUTH", "CLAUDE_CODE_USE_")
SCRUB_NAMES = frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY", "AGENT_LAUNCHER_API_KEY"})


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


def build_plan(gateway, cli: str, directory: str, model: str, resume: bool = False,
               catalog: Catalog | None = None, preserve_terminal_title: bool = False,
               bypass_permissions: bool = False, controlled: bool = False, admin_note: str | None = None,
               conversation: str | None = None) -> LaunchPlan:
    from agent_launcher.harnesses import registry
    harness = registry.get(cli)
    bypass_permissions = bypass_permissions or controlled
    if controlled and "controlled" not in harness.CAPABILITIES:
        raise LauncherError(f"O {harness.LABEL} não tem sessão controlada pelo orquestrador.")
    if cli not in gateway.clis:
        raise LauncherError(f"A conexão {gateway.name} não atende o {cli}. Escolha outra conexão ou o outro CLI.")
    model = validate_model(model)
    catalog = catalog or gateway.empty_catalog()
    selected = catalog.find(model) or ModelInfo(base_model(model))
    if not catalog.allows(selected, cli):
        raise LauncherError(f"O modelo {selected.id} não é compatível com o {cli} nesta conexão. Escolha um ID da lista.")
    model = harness.model_id(model, selected, catalog)
    input_limit = selected.max_input_tokens if catalog.verified else None
    warnings = []
    if bypass_permissions:
        warnings.append("Bypass ativo: comandos sem confirmação de permissões. No Codex, o sandbox também está desativado.")
    if input_limit is None:
        warnings.append("Janela de entrada não confirmada; não serão aplicados aumentos de contexto a este modelo.")
    path = project_path(directory)
    if resume:
        from agent_launcher.session.conversations import resume_target
        resume, conversation = resume_target(getattr(getattr(gateway, "store", None), "home", None), harness, path,
                                             conversation, warnings)
    else:
        conversation = None
    command = harness.command()
    environment = without_parent_session(dict(os.environ))
    for key in list(environment):
        upper = key.upper()
        if upper.startswith(SCRUB_PREFIXES + harness.SCRUB_PREFIXES) or upper in SCRUB_NAMES | harness.SCRUB:
            environment.pop(key)
    context = LaunchContext(gateway, catalog, selected, model, model, path, resume, bypass_permissions, controlled,
                            preserve_terminal_title, admin_note, command, environment, warnings, input_limit, conversation)
    plan = harness.plan(context)
    plan.resume = resume
    if resume and conversation:
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


def has_previous_conversation(cli: str, directory: Path) -> bool:
    """Existe conversa desta pasta para o destino retomar (--continue do Claude, resume --last do Codex...)?"""
    from agent_launcher.harnesses import registry
    harness = registry.find(cli)
    return bool(harness and harness.has_previous_conversation(Path(directory)))


def run_plan(plan: LaunchPlan) -> int:
    from agent_launcher.harnesses import registry
    harness = registry.get(plan.cli)
    try:
        with tempfile.TemporaryDirectory(prefix="agent-launcher-session-") as directory:
            command, warning = harness.final_command(plan, Path(directory))
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
