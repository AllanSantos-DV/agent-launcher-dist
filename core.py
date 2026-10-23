"""Fachada: o orquestrador faz `from core import Store`, e scripts antigos usam core.*. O código mora em
agent_launcher/ (domain, session, harnesses); aqui só se reexporta."""
from agent_launcher import ROOT  # noqa: F401
from agent_launcher.connectors import ConnectorError  # noqa: F401
from agent_launcher.connectors._http import NO_KEY, is_local_host  # noqa: F401
from agent_launcher.domain.catalog import (CLAUDE_FAMILIES, CLIS, MODEL_PATTERN, Catalog, ModelInfo,  # noqa: F401
                                           base_model, codex_context)
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, clean_text, path_key, project_path, validate_model  # noqa: F401
from agent_launcher.domain.gateway import CACHE_TTL, Gateway  # noqa: F401
from agent_launcher.domain.store import (BACKUP_DAYS, CONNECTION_KEYS, DEFAULT_HOME, PREFERENCES,  # noqa: F401
                                         PROJECT_KEYS, Store, legacy_configs)
from agent_launcher.domain.vault import Vault  # noqa: F401
from agent_launcher.harnesses.claude_code import (ALL_MODELS_LIMIT, _resumable_claude_transcript,  # noqa: F401
                                                  claude_config_file, trust_claude_folder)
from agent_launcher.harnesses.codex import (CODEX_KEY_VARIABLE, CODEX_PROVIDER, approve_codex_hooks,  # noqa: F401
                                            codex_options, controlled_codex_commands, prepare_codex_command,
                                            run_controlled_codex)
from agent_launcher.platform.jobs import free_port, kill_with_parent, stop_tree  # noqa: F401
from agent_launcher.session.close import CLOSE_REQUEST_MAX_AGE_S, CLOSE_REQUESTS_DIR, consume_close_request  # noqa: F401
from agent_launcher.session.controlled import (CODEX_CONTROLLED_DIR, CONTROLLED_NOTE, DELEGATION_CODE_FILE,  # noqa: F401
                                               PARENT_SESSION_MARKERS, UNATTENDED_RESUME_THRESHOLD, delegation_code,
                                               without_parent_session)
from agent_launcher.session.plan import LaunchPlan, build_plan, has_previous_conversation, run_plan  # noqa: F401


def resolve_cli(cli: str) -> list[str]:
    """Comando para rodar o destino de terminal (executável nativo ou node + script do pacote npm)."""
    from agent_launcher.harnesses import registry
    harness = registry.get(cli)
    if harness.KIND != "terminal":
        raise LauncherError(f"O {harness.LABEL} não roda em terminal.")
    return harness.command()


def prepare_unattended(plan: LaunchPlan) -> None:
    """Antes de abrir uma sessão controlada do Claude Code: confia a pasta do projeto cadastrado. Falha visível."""
    if plan.controlled and plan.cli == "claude":
        try:
            if trust_claude_folder(plan.directory):
                print(f"  Pasta marcada como confiável no Claude Code: {plan.directory}")
        except (LauncherError, OSError) as error:
            print(f"  Aviso: {error} A sessão pode pedir confirmação de confiança da pasta.")
