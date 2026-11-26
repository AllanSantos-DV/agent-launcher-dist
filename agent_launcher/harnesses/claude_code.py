"""Claude Code: Anthropic Messages, configurado pelo ambiente e por --settings só no processo da sessão."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from agent_launcher.domain import usage
from agent_launcher.domain.catalog import CLAUDE_FAMILIES, Catalog, ModelInfo, base_model, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import read_text_retrying, replace_retrying
from agent_launcher.harnesses import installers
from agent_launcher.harnesses.base import (Conversation, Harness, HistoryItem, conversation_title, count_lines,
                                           head_lines, key_mcp_urls, npm_cli_command, parse_time, recorded_branch)
from agent_launcher.session.controlled import CONTROLLED_NOTE, UNATTENDED_RESUME_THRESHOLD, delegation_code
from agent_launcher.session.plan import LaunchPlan

ALL_MODELS_LIMIT = 150  # availableModels vai na linha de comando: roteadores com 1000 IDs estourariam o limite do Windows.


def _resumable_claude_transcript(transcript: Path, max_lines: int = 2000) -> bool:
    """Conversa que o --continue aceita: a do terminal ou do app. As de -p/SDK (entrypoint sdk-*) ele ignora."""
    try:
        with transcript.open(encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream):
                if number >= max_lines:
                    return False
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "user":
                    return not str(entry.get("entrypoint") or "").startswith("sdk-")
    except OSError:
        return False
    return False


def _user_text(entry: dict, meta: bool = False) -> str | None:
    """Texto digitado pelo usuário numa linha "user" do transcript (None em resultado de ferramenta; as linhas meta,
    como mensagens de outra sessão, só com meta=True)."""
    if entry.get("isCompactSummary") or (entry.get("isMeta") and not meta):
        return None
    content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
    if isinstance(content, list):
        if any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
            return None
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    if not isinstance(content, str) or not content.strip() or content.lstrip().startswith(("<command-", "<local-command")):
        return None
    return content


CLAUDE_USER_MARKER = b'"type":"user"'


def _counted_user_line(line: bytes) -> bool:
    return b'"type":"user"' in line and b'"tool_result"' not in line and b'"isMeta":true' not in line


def claude_conversation(transcript: Path, detail: bool = True) -> Conversation | None:
    """Conversa de um transcript, se o --continue/--resume a aceita (entrypoint que não é sdk-*). Sem `detail` não procura o
    título (vazio) e para de ler assim que sabe o começo e o entrypoint: linhas de transcript chegam a centenas de KB."""
    started = resumable = branch = None
    titles = {}  # custom (nome dado pelo usuário), user, meta, summary: o primeiro de cada, nesta ordem de preferência
    for line in head_lines(transcript):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if started is None and entry.get("timestamp"):
            started = parse_time(entry["timestamp"], None)
        branch = branch or recorded_branch(entry.get("gitBranch"))
        kind = entry.get("type")
        if kind == "custom-title" and entry.get("customTitle"):
            titles["custom"] = entry["customTitle"]
        elif kind == "summary" and entry.get("summary"):
            titles.setdefault("summary", entry["summary"])
        if kind != "user":
            continue
        if resumable is None:
            resumable = not str(entry.get("entrypoint") or "").startswith("sdk-")
            if not resumable:
                return None
        if not detail and started is not None:
            break
        for key, meta in (("user", False), ("meta", True)):
            if key not in titles and _user_text(entry, meta):
                titles[key] = _user_text(entry, meta)
    title = next((titles[key] for key in ("custom", "user", "meta", "summary") if key in titles), "")
    if resumable is None and not _resumable_claude_transcript(transcript):
        return None
    try:
        status = transcript.stat()
    except OSError:
        return None
    started = started or status.st_ctime
    return Conversation(transcript.stem, "claude", started, max(status.st_mtime, started), conversation_title(title),
                        count_lines(transcript, _counted_user_line, CLAUDE_USER_MARKER) if detail else None, branch)


RECORDED_FOLDER_LINES = 50  # o `cwd` aparece nas primeiras linhas do transcript


def claude_folder_name(directory) -> str:
    """O nome da pasta de projeto no histórico do Claude Code: todo caractere não alfanumérico do caminho vira `-`, então o
    caminho não volta a partir do nome."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(directory))


def claude_project_folder(directory: Path) -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    return base / claude_folder_name(directory)


def claude_recorded_folder(transcript: Path, encoded: str) -> str | None:
    """O `cwd` de uma linha do transcript que, codificado como o Claude Code o codifica, dá exatamente `encoded` (o nome da
    pasta de projeto): o caminho real da pasta. None quando nenhuma linha traz `cwd` ou nenhum confere (o `cwd` pode ser o
    de uma subpasta)."""
    for line in head_lines(transcript, RECORDED_FOLDER_LINES):
        if '"cwd"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(cwd, str) and claude_folder_name(cwd).casefold() == encoded.casefold():
            return cwd
    return None


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def claude_config_file() -> Path:
    """Onde o Claude Code guarda o estado do usuário (inclusive a confiança por pasta)."""
    folder = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(folder) / ".claude.json" if folder else Path.home() / ".claude.json"


def _trusted(config: Path, keys: list[str]) -> bool:
    try:
        projects = json.loads(read_text_retrying(config)).get("projects")
    except (OSError, ValueError):
        return False
    return isinstance(projects, dict) and all(isinstance(projects.get(name), dict)
                                              and projects[name].get("hasTrustDialogAccepted") is True for name in keys)


def trust_claude_folder(path: Path, config: Path | None = None, attempts: int = 8, before_replace=None) -> bool:
    """Grava projects[<pasta>].hasTrustDialogAccepted=true no estado do Claude Code. Só para sessões controladas
    em projetos cadastrados. O arquivo é reescrito pelas sessões abertas: troca atômica e só se ele não mudou desde
    a leitura; senão relê e tenta de novo. Devolve False se a pasta já era confiável."""
    config = config or claude_config_file()
    key = str(path).replace("\\", "/")
    for attempt in range(attempts):
        try:
            before = config.stat()
            data = json.loads(read_text_retrying(config))
        except FileNotFoundError as error:
            raise LauncherError(f"Estado do Claude Code não encontrado em {config}; abra o Claude Code uma vez.") from error
        except ValueError as error:
            raise LauncherError(f"{config} não é JSON válido; confiança da pasta não gravada.") from error
        projects = data.get("projects")
        if not isinstance(projects, dict):
            raise LauncherError(f"Formato inesperado em {config} (sem 'projects'); confiança da pasta não gravada.")
        keys = [name for name in projects if name.replace("\\", "/").casefold() == key.casefold()]
        if key not in keys:
            keys.append(key)  # o Claude Code no Windows lê a chave com barra normal
        if all(isinstance(projects.get(name), dict) and projects[name].get("hasTrustDialogAccepted") is True
               for name in keys):
            return False
        for name in keys:
            entry = projects.get(name)
            projects[name] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
        descriptor, temporary = tempfile.mkstemp(prefix=config.name + ".", suffix=".tmp", dir=config.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            if before_replace:
                before_replace()
            now = config.stat()
            if (now.st_mtime_ns, now.st_size) == (before.st_mtime_ns, before.st_size):
                replace_retrying(temporary, config)
                if _trusted(config, keys):  # outra sessão pode ter regravado o arquivo entre a conferência e a troca
                    return True
                continue
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        time.sleep(0.1 * (attempt + 1))  # outra sessão gravou no meio: relê e tenta de novo
    raise LauncherError(f"{config} mudou durante todas as {attempts} tentativas; confiança da pasta não gravada.")


CLAUDE_DEFAULT_OUTPUT = 32_000  # padrão do Claude Code para ID que ele não reconhece


def host_output_limit(record, verified: bool) -> int | None:
    """Saída máxima que o host declara para um modelo que o Claude Code não reconhece, limitada ao padrão dele (o host
    com saída menor recusaria o pedido); None quando o host não informou ou o modelo é da família Claude."""
    if not verified or record.family or not record.max_output_tokens:
        return None
    return min(record.max_output_tokens, CLAUDE_DEFAULT_OUTPUT)


class ClaudeCode(Harness):
    ID = "claude"
    LABEL = "Claude Code"
    KIND = "terminal"
    CONSUMES = ("messages",)
    CAPABILITIES = frozenset({"resume", "fork", "controlled", "keys", "bypass", "context_window", "families"})
    FORK_CONTROLLED = True
    SUBTITLE = "Anthropic Messages"
    SYMBOL = "terminal"
    TAB_COLOR = "#D97757"
    BYPASS_RISK = "Ignora as verificações de permissão do Claude Code."
    BYPASS_HINT = "Sem pedir permissão"
    EXECUTABLE = "claude"
    ENDPOINT_SESSIONS = True

    def find_executable(self) -> str | None:
        return installers.find(self.ID, usable=True)

    def install_steps(self) -> list:
        return installers.steps(self.ID)

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        return record.chat and (catalog.any_model or record.native_cli == "claude")

    def model_id(self, requested: str, selected: ModelInfo, catalog: Catalog) -> str:
        if requested != base_model(requested):
            if not catalog.verified or not selected.one_million or not selected.family:
                raise LauncherError("Não foi possível confirmar 1M para esse ID/provider. Atualize o catálogo; não force [1m].")
        return selected.claude_id(catalog.verified)

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        budget = token_label(record.max_input_tokens).replace(".", ",")
        suffix = "   ·   [1m] aplicado pelo Claude" if record.one_million else ""
        mapped = catalog.claude_families(record)
        return (f"Entrada declarada: {budget} tokens{suffix}",
                "Famílias: " + "   ·   ".join(f"{family.title()} {token_label(model.max_input_tokens)}"
                                              for family, model in mapped.items()))

    def command_for(self, path: Path, repair: bool = True) -> list[str]:
        try:
            native = installers.claude_binary(path, repair)
        except installers.CliInstallError as error:
            raise LauncherError(str(error)) from error
        if native is not None:
            return [str(native)]
        return npm_cli_command(path, "@anthropic-ai/claude-code/cli.js")

    def connection_url(self, gateway) -> str:
        return gateway.targets["claude"] or ""

    def plan(self, context) -> LaunchPlan:
        gateway, catalog, selected, model = context.gateway, context.catalog, context.selected, context.model
        warnings, command = context.warnings, context.command
        token, base_url = gateway.token, gateway.targets["claude"]
        if context.endpoint:
            from agent_launcher.endpoint.registry import PLACEHOLDER_KEY
            token, base_url = PLACEHOLDER_KEY, context.endpoint.url
        context_window = None
        family_records = catalog.claude_families(selected)
        families = {family: record.claude_id(catalog.verified) for family, record in family_records.items()}
        missing = [family for family in ("opus", "sonnet", "haiku") if family not in families]
        if missing:
            # Alias sem modelo cairia no ID padrão do Claude Code, que o host pode não ter: usa o modelo principal.
            warnings.append("Famílias sem modelo nesta conexão: " + ", ".join(missing) + ". Usarão o modelo principal.")
        fast_model = families.get("haiku") or families.get("sonnet") or model
        if catalog.verified and selected.one_million and selected.family:
            context_window = 1_000_000
        elif catalog.verified and not selected.family and selected.max_input_tokens:
            # Modelo de outro fornecedor via roteador: o Claude Code não reconhece o ID e assumiria a janela padrão.
            # Sem [1m] no ID, CLAUDE_CODE_MAX_CONTEXT_TOKENS vale direto (code.claude.com/docs/en/model-config).
            context_window = selected.max_input_tokens
        api_key = gateway.targets.get("claude_auth") == "api_key"
        secret_variable = "ANTHROPIC_API_KEY" if api_key else "ANTHROPIC_AUTH_TOKEN"
        overrides = {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": "" if api_key else token,
            "ANTHROPIC_API_KEY": token if api_key else "",
            "ANTHROPIC_CUSTOM_HEADERS": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_CODE_USE_BEDROCK": "0",
            "CLAUDE_CODE_USE_VERTEX": "0",
            "CLAUDE_CODE_USE_FOUNDRY": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # NONESSENTIAL_TRAFFIC desliga a atualização nativa de plugins; esta variável documentada a restaura.
            "FORCE_AUTOUPDATE_PLUGINS": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "0" if catalog.verified and any(
                record.one_million and record.family for record in [selected, *family_records.values()]) else "1",
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": fast_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
        }
        if context.preserve_terminal_title:
            overrides["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
        if context_window and not selected.family:
            overrides["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_window)
        output_limit = host_output_limit(selected, catalog.verified)
        if output_limit:
            overrides["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(output_limit)
        if gateway.options.get("tool_search") is True:
            # Fora da Anthropic o Claude Code carrega todo MCP de uma vez; a conexão declara que o host aceita
            # tool_reference, então as ferramentas passam a carregar sob demanda.
            overrides["ENABLE_TOOL_SEARCH"] = "true"
        if context.controlled:
            # Sem humano para responder: o --continue de conversa grande (> 100 mil tokens) parada há mais de 70 min abre
            # o diálogo nativo "Resume from summary" e a sessão trava nele. Os limites são do próprio Claude Code e
            # valem só para este processo (a config global e as sessões normais seguem com a recomendação).
            overrides["CLAUDE_CODE_RESUME_THRESHOLD_MINUTES"] = UNATTENDED_RESUME_THRESHOLD
            overrides["CLAUDE_CODE_RESUME_TOKEN_THRESHOLD"] = UNATTENDED_RESUME_THRESHOLD
        for family in CLAUDE_FAMILIES:
            variable = f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL"
            record = family_records.get(family)
            if record is None and family == "fable":
                overrides.update({variable: "", variable + "_NAME": "", variable + "_DESCRIPTION": ""})
                continue
            overrides[variable] = families.get(family, model)
            overrides[variable + "_NAME"] = f"{family.title()} | {(record or selected).provider or gateway.name}"
            overrides[variable + "_DESCRIPTION"] = catalog.describe(record or selected)
        allowed = {record.id for record in catalog.for_cli("claude")}
        if len(allowed) > ALL_MODELS_LIMIT:
            allowed = {record.id for record in family_records.values()}
        allowed = sorted(allowed | {selected.id})
        # A chave fica só no ambiente do processo; no --settings vai vazia a variável que não é usada, para que
        # settings de usuário não a reativem.
        settings_env = {key: value for key, value in overrides.items() if key != secret_variable}
        settings = json.dumps({"apiKeyHelper": "", "env": settings_env, "availableModels": allowed}, ensure_ascii=True, separators=(",", ":"))
        command += ["--settings", settings, "--model", model]
        mcp = key_mcp_urls(gateway)
        if mcp:
            # Só neste processo (sem --strict-mcp-config, os MCPs do próprio usuário continuam): a chave do perfil vai no caminho.
            command += ["--mcp-config", json.dumps({"mcpServers": {name: {"type": "http", "url": url}
                                                                    for name, url in mcp.items()}}, separators=(",", ":"))]
        command += ["--dangerously-skip-permissions"] if context.bypass_permissions else ["--permission-mode", "default"]
        notes = ([CONTROLLED_NOTE.format(code=delegation_code())] if context.controlled else []) + (
            [context.admin_note] if context.admin_note else [])
        if notes:
            command += ["--append-system-prompt", "\n\n".join(notes)]
        if context.resume:
            command += ["--resume", context.conversation] if context.conversation else ["--continue"]
            if context.fork:
                command.append("--fork-session")
            session_id = None if context.fork else context.conversation
        else:
            # Sessão nova com id escolhido pelo launcher: a conversa fica conhecida na hora (registro por perfil, §1).
            session_id = str(uuid.uuid4())
            command += ["--session-id", session_id]
        plan = LaunchPlan(self.ID, model, context.directory, command, context.environment, overrides, context.input_limit,
                          context_window, None, families, warnings, context.controlled, gateway.name,
                          () if context.endpoint else (secret_variable,))
        plan.session_id = session_id
        return plan

    def prepare(self, plan) -> None:
        """Antes de abrir: sobe o endpoint local se a sessão vai por ele; em sessão controlada, confia a pasta do projeto
        cadastrado. Falha visível."""
        self.ensure_endpoint(plan)
        if plan.controlled:
            try:
                if trust_claude_folder(plan.directory):
                    print(f"  Pasta marcada como confiável no Claude Code: {plan.directory}")
            except (LauncherError, OSError) as error:
                print(f"  Aviso: {error} A sessão pode pedir confirmação de confiança da pasta.")

    def has_previous_conversation(self, directory: Path) -> bool:
        return any(_resumable_claude_transcript(transcript) for transcript in claude_project_folder(directory).glob("*.jsonl"))

    def conversations(self, directory: Path, detail: bool = True) -> list[Conversation]:
        found = (claude_conversation(transcript, detail) for transcript in claude_project_folder(directory).glob("*.jsonl"))
        return sorted((item for item in found if item), key=lambda item: item.updated, reverse=True)

    def history(self) -> list[HistoryItem]:
        """Cada pasta de ~/.claude/projects com o caminho real lido do `cwd` das linhas (só vale o que confere com o nome da
        pasta); sem `cwd` que confira, `folder` é None e `label` é o nome da pasta."""
        try:
            folders = sorted(path for path in usage.claude_projects().iterdir() if path.is_dir())
        except OSError:
            return []
        items = []
        for folder in folders:
            transcripts = sorted(folder.glob("*.jsonl"), key=_modified, reverse=True)
            found = [item for item in (claude_conversation(transcript, False) for transcript in transcripts) if item]
            if not found:
                continue
            real = next(filter(None, (claude_recorded_folder(transcript, folder.name) for transcript in transcripts)), None)
            items += [HistoryItem(real, folder.name, item) for item in found]
        return items


HARNESS = ClaudeCode()
