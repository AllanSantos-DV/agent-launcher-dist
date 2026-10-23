"""GitHub Copilot CLI: BYOK só pelo ambiente do processo da sessão, sem login GitHub e sem gravar nada
em ~/.copilot. Protocolo por modelo: Claude por Anthropic Messages, GPT-5+ por Responses, o resto por Chat Completions.

Testado com o Copilot CLI 1.0.101 num host LiteLLM: sem COPILOT_PROVIDER_MODEL_ID reconhecido, o CLI manda
`temperature` e o host recusa Claude 5.5 com 400, e recusa gpt-4.1-mini (`Invalid value: 'custom'`). Por isso o ID
conhecido sai de um mapa por família e o ID do catálogo vai em COPILOT_PROVIDER_WIRE_MODEL/COPILOT_MODEL."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path

from agent_launcher.connectors._http import NO_KEY, is_local_host
from agent_launcher.domain.catalog import Catalog, ModelInfo, generation, protocol_for, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.harnesses import installers
from agent_launcher.harnesses.base import (Conversation, Harness, conversation_title, count_lines, head_lines, is_exe,
                                           native_wrapper, node_script, parse_time, same_folder, unrecognized)
from agent_launcher.session.plan import LaunchPlan

PROVIDER_TYPES = {"messages": "anthropic", "responses": "openai", "chat_completions": "openai"}
WIRE_APIS = {"responses": "responses", "chat_completions": "completions"}
SECRET_VARIABLE = "COPILOT_PROVIDER_API_KEY"


def known_model_id(model: str) -> str:
    """ID que o Copilot CLI reconhece (configuração do agente e limites) para o ID do catálogo do host."""
    lowered = model.lower()
    claude = re.search(r"claude-(opus|sonnet|haiku)", lowered)
    if claude:
        return {"opus": "claude-opus-4.5", "sonnet": "claude-sonnet-4.5", "haiku": "claude-haiku-4.5"}[claude.group(1)]
    gpt = re.search(r"gpt-(\d+(?:\.\d+)?)(-mini|-nano|-codex)?", lowered)
    if gpt:
        version, variant = gpt.group(1), gpt.group(2) or ""
        if generation(lowered) >= 5:
            return f"gpt-{version}{variant}"
        if version.startswith("4.1"):
            return "gpt-4.1"
        return "gpt-4o"
    # Outros fornecedores: o próprio ID do catálogo (testado: com "gemini-2.5-pro" o Copilot manda
    # tool_choice=validated e o host recusa; com o ID do host ele usa os padrões seguros e funciona).
    return model


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def _workspace(folder: Path) -> dict:
    """Campos simples (chave: valor) do começo do workspace.yaml de uma sessão."""
    fields = {}
    for line in head_lines(folder / "workspace.yaml", 30):
        key, separator, value = line.partition(":")
        if separator and key and not key.startswith((" ", "#")):
            fields[key.strip()] = value.strip().strip("'\"")
    return fields


def _counted_copilot_line(line: bytes) -> bool:
    return b'"type":"user.message"' in line


def copilot_conversation(folder: Path, fields: dict) -> Conversation | None:
    events = folder / "events.jsonl"
    title = None
    for line in head_lines(events):
        if '"user.message"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "user.message":
            title = (entry.get("data") or {}).get("content") if isinstance(entry.get("data"), dict) else None
            if title:
                break
    try:
        status = (events if events.is_file() else folder / "workspace.yaml").stat()
    except OSError:
        return None
    started = parse_time(fields.get("created_at"), status.st_ctime)
    updated = max(parse_time(fields.get("updated_at"), started), status.st_mtime, started)
    messages = count_lines(events, _counted_copilot_line, b'"type":"user.message"') if events.is_file() else 0
    return Conversation(fields.get("id") or folder.name, "copilot", started, updated,
                        conversation_title(title or fields.get("summary")), messages)


class CopilotCli(Harness):
    ID = "copilot"
    LABEL = "Copilot CLI"
    KIND = "terminal"
    CONSUMES = ("messages", "responses", "chat_completions")
    CAPABILITIES = frozenset({"resume", "keys", "bypass", "context_window"})
    SUBTITLE = "GitHub Copilot no terminal"
    SYMBOL = "copilot"
    TAB_COLOR = "#8957E5"
    BYPASS_RISK = "Libera todas as ferramentas, caminhos e URLs do Copilot CLI sem confirmação (--allow-all)."
    BYPASS_HINT = "Sem pedir permissão"
    EXECUTABLE = "copilot"
    SCRUB_PREFIXES = ("COPILOT_PROVIDER_",)
    SCRUB = frozenset({"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_MODEL", "COPILOT_OFFLINE",
                       "COPILOT_ALLOW_ALL"})

    def find_executable(self) -> str | None:
        return installers.find(self.ID)

    def install_steps(self) -> list:
        return installers.steps(self.ID)

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        # O Copilot CLI exige ferramentas: tools=False sai (record.chat), desconhecido fica (o host decide).
        if not record.chat:
            return False
        if record.native_cli == "" and not catalog.any_model and "chat_completions" not in catalog.protocols:
            return False
        return protocol_for(record, catalog.protocols, catalog.any_model, self.CONSUMES) is not None

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        protocol = protocol_for(record, catalog.protocols, catalog.any_model, self.CONSUMES)
        names = {"messages": "Anthropic Messages", "responses": "OpenAI Responses", "chat_completions": "Chat Completions"}
        return (f"Entrada declarada: {token_label(record.max_input_tokens).replace('.', ',')} tokens",
                f"Protocolo: {names.get(protocol, '?')} • ID conhecido pelo Copilot: {known_model_id(record.id)}")

    def command(self) -> list[str]:
        executable = self.find_executable()
        if not executable:
            raise LauncherError(f"{self.LABEL} não está instalado. Use 'Instalar' na interface ou "
                                f"'launcher.py cli --install {self.ID}'.")
        path = Path(executable)
        if is_exe(path):
            return [str(path)]
        command = node_script(path, "@github/copilot/npm-loader.js") or native_wrapper(path)
        if command is None:
            raise unrecognized(path)
        return command

    def protocol(self, gateway, selected: ModelInfo, catalog: Catalog) -> str:
        protocol = protocol_for(selected, gateway.protocols, gateway.any_model, self.CONSUMES)
        if protocol is None:
            raise LauncherError(f"A conexão {gateway.name} não serve um protocolo que o Copilot CLI use para {selected.id}.")
        return protocol

    def connection_url(self, gateway) -> str:
        return gateway.protocols.get("messages") or gateway.protocols.get("responses") or gateway.protocols.get(
            "chat_completions") or ""

    def target_url(self, context) -> str:
        return context.gateway.protocols[self.protocol(context.gateway, context.selected, context.catalog)]

    def plan(self, context) -> LaunchPlan:
        gateway, catalog, selected, model = context.gateway, context.catalog, context.selected, context.model
        protocol = self.protocol(gateway, selected, catalog)
        url = gateway.protocols[protocol]
        if protocol == "messages":
            url = url.removesuffix("/v1")  # o tipo anthropic acrescenta /v1/messages, como o Claude Code
        overrides = {
            "COPILOT_PROVIDER_BASE_URL": url,
            "COPILOT_PROVIDER_TYPE": PROVIDER_TYPES[protocol],
            "COPILOT_MODEL": model,
            "COPILOT_PROVIDER_MODEL_ID": known_model_id(model),
            "COPILOT_PROVIDER_WIRE_MODEL": model,
            SECRET_VARIABLE: gateway.key,
        }
        if protocol in WIRE_APIS:
            overrides["COPILOT_PROVIDER_WIRE_API"] = WIRE_APIS[protocol]
        if gateway.key and protocol == "messages" and gateway.messages_auth == "bearer":
            overrides["COPILOT_PROVIDER_BEARER_TOKEN"] = gateway.key
            overrides[SECRET_VARIABLE] = ""
        if catalog.verified and selected.max_input_tokens:
            overrides["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] = str(selected.max_input_tokens)
        if catalog.verified and selected.max_output_tokens:
            overrides["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"] = str(selected.max_output_tokens)
        host = urllib.parse.urlparse(url).hostname or ""
        if host and is_local_host(host):
            # Host local/privado: nada sai para o GitHub (login, telemetria, atualização, MCP do GitHub).
            overrides["COPILOT_OFFLINE"] = "true"
        command = list(context.command)
        command += ["--allow-all"] if context.bypass_permissions else []
        if context.resume:
            command += ["--resume", context.conversation] if context.conversation else ["--continue"]
        warnings = context.warnings
        if not catalog.verified:
            warnings.append("Limites do modelo não confirmados: o Copilot CLI usa os do catálogo dele.")
        secret = ("COPILOT_PROVIDER_BEARER_TOKEN", SECRET_VARIABLE)
        return LaunchPlan(self.ID, model, context.directory, command, context.environment, overrides, context.input_limit,
                          selected.max_input_tokens if catalog.verified else None, None, {}, warnings, False,
                          gateway.name, secret)

    def has_previous_conversation(self, directory: Path) -> bool:
        """Sessões em ~/.copilot/session-state/<id>/workspace.yaml com `cwd: <pasta>` (a mais recente primeiro)."""
        target = os.path.normcase(os.path.normpath(str(directory)))
        base = copilot_home() / "session-state"
        try:
            folders = sorted(base.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)[:400]
        except OSError:
            return False
        for folder in folders:
            try:
                with (folder / "workspace.yaml").open(encoding="utf-8", errors="replace") as stream:
                    for _number, line in zip(range(20), stream):
                        if line.startswith("cwd:"):
                            value = line[4:].strip().strip("'\"")
                            if value and os.path.normcase(os.path.normpath(value)) == target:
                                return True
                            break
            except OSError:
                continue
        return False

    def conversations(self, directory: Path) -> list[Conversation]:
        try:
            folders = sorted(copilot_home().joinpath("session-state").iterdir(), key=lambda path: path.stat().st_mtime,
                             reverse=True)[:400]
        except OSError:
            return []
        found = []
        for folder in folders:
            fields = _workspace(folder)
            if same_folder(fields.get("cwd"), directory):
                conversation = copilot_conversation(folder, fields)
                if conversation:
                    found.append(conversation)
        return sorted(found, key=lambda item: item.updated, reverse=True)

    def dry_run_extra(self, plan) -> dict:
        return {}


HARNESS = CopilotCli()
