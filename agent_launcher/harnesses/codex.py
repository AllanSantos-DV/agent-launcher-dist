"""Codex: OpenAI Responses, provider próprio passado por -c só no processo; sessão controlada com app-server + TUI."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

from agent_launcher.domain.catalog import Catalog, ModelInfo, base_model, codex_context
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, path_key
from agent_launcher.harnesses import installers
from agent_launcher.harnesses.base import (Conversation, Harness, conversation_title, count_lines, head_lines, is_exe,
                                           key_mcp_urls, native_wrapper, node_script, parse_time, same_folder, unrecognized)
from agent_launcher.platform.jobs import close_job, free_port, kill_with_parent, stop_tree
from agent_launcher.session import controlled as controlled_state
from agent_launcher.session.plan import LaunchPlan

CODEX_PROVIDER = "agent_launcher"
CODEX_KEY_VARIABLE = "AGENT_LAUNCHER_API_KEY"


def codex_options(plan: LaunchPlan) -> list[str]:
    """Os pares -c do plano (provider, permissões, confiança da pasta)."""
    options, index = [], 0
    while index < len(plan.command):
        if plan.command[index] == "-c":
            options += plan.command[index:index + 2]
            index += 2
        else:
            index += 1
    return options



INTERACTIVE_SOURCES = ("cli", "vscode")  # sessões que o `codex resume --last` lista; exec e subagentes não
CONTEXT_PREFIXES = ("<environment_context", "<user_instructions", "<permissions", "# AGENTS.md")  # injetadas pelo Codex


def codex_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def _rollout_meta(rollout: Path) -> dict | None:
    """payload da 1ª linha (session_meta) de um rollout interativo; None nos demais."""
    try:
        with rollout.open(encoding="utf-8", errors="replace") as stream:
            meta = json.loads(stream.readline() or "{}")
    except (OSError, ValueError):
        return None
    payload = (meta.get("payload") or {}) if isinstance(meta, dict) else {}
    if not isinstance(payload, dict) or payload.get("source", "cli") not in INTERACTIVE_SOURCES:
        return None
    return {**payload, "_timestamp": meta.get("timestamp")}


def _codex_user_text(entry: dict) -> str | None:
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict) or entry.get("type") != "response_item" or payload.get("role") != "user":
        return None
    text = " ".join(part.get("text", "") for part in payload.get("content") or []
                    if isinstance(part, dict) and part.get("type") == "input_text").strip()
    return None if not text or text.startswith(CONTEXT_PREFIXES) else text


def _counted_codex_line(line: bytes) -> bool:
    return (b'"role":"user"' in line and b'"type":"response_item"' in line
            and not any(b'"text":"' + prefix.encode() in line for prefix in CONTEXT_PREFIXES))


def codex_conversation(rollout: Path, payload: dict) -> Conversation | None:
    identifier = payload.get("id") or payload.get("session_id")
    if not isinstance(identifier, str) or not identifier:
        return None
    title = None
    for line in head_lines(rollout)[1:]:
        try:
            title = _codex_user_text(json.loads(line))
        except ValueError:
            continue
        if title:
            break
    try:
        status = rollout.stat()
    except OSError:
        return None
    started = parse_time(payload.get("timestamp") or payload.get("_timestamp"), status.st_ctime)
    return Conversation(identifier, "codex", started, max(status.st_mtime, started), conversation_title(title),
                        count_lines(rollout, _counted_codex_line, b'"role":"user"'))

class Codex(Harness):
    ID = "codex"
    LABEL = "Codex"
    KIND = "terminal"
    CONSUMES = ("responses",)
    CAPABILITIES = frozenset({"resume", "controlled", "keys", "bypass", "context_window"})
    SUBTITLE = "OpenAI Responses"
    SYMBOL = "code"
    TAB_COLOR = "#10A37F"
    BYPASS_RISK = "Executa comandos sem confirmações e sem sandbox no Codex."
    BYPASS_HINT = "Sem sandbox ou confirmações"
    EXECUTABLE = "codex"
    SCRUB = frozenset({CODEX_KEY_VARIABLE})

    def find_executable(self) -> str | None:
        return installers.find(self.ID)

    def install_steps(self) -> list:
        return installers.steps(self.ID)

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        if not record.chat or record.responses is False:
            return False
        return catalog.any_model or record.native_cli == "codex"

    def model_id(self, requested: str, selected: ModelInfo, catalog: Catalog) -> str:
        if requested != base_model(requested):
            raise LauncherError("[1m] é um sufixo do Claude, não do Codex. No Codex, use o ID sem sufixo.")
        return selected.id

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        from agent_launcher.domain.catalog import token_label
        budget = token_label(record.max_input_tokens).replace(".", ",")
        return f"Entrada declarada: {budget} tokens", "Contexto informado pelo host. O Codex mantém sua margem interna."

    def command(self) -> list[str]:
        executable = self.find_executable()
        if not executable:
            raise LauncherError(f"{self.LABEL} não está instalado. Use 'Instalar' na interface ou "
                                f"'launcher.py cli --install {self.ID}'.")
        path = Path(executable)
        if is_exe(path):
            return [str(path)]
        command = node_script(path, "@openai/codex/bin/codex.js") or native_wrapper(path)
        if command is None:
            raise unrecognized(path)
        return command

    def connection_url(self, gateway) -> str:
        return gateway.targets["codex"] or ""

    def plan(self, context) -> LaunchPlan:
        gateway, catalog, selected, model = context.gateway, context.catalog, context.selected, context.model
        warnings, command = context.warnings, context.command
        overrides = {CODEX_KEY_VARIABLE: gateway.token}
        provider = f"model_providers.{CODEX_PROVIDER}"
        options = {
            "model_provider": CODEX_PROVIDER,
            f"{provider}.name": gateway.name,
            f"{provider}.base_url": gateway.targets["codex"],
            f"{provider}.env_key": CODEX_KEY_VARIABLE,
            f"{provider}.wire_api": "responses",
            f"{provider}.requires_openai_auth": False,
        }
        for name, url in key_mcp_urls(gateway).items():
            options[f"mcp_servers.{name}.url"] = url  # a chave do perfil vai no caminho; só neste processo
        if context.preserve_terminal_title:
            options["tui.terminal_title"] = []
        context_window, auto_compact_limit = codex_context(selected, catalog.verified)
        if context_window is not None:
            options["model_context_window"] = context_window
            options["model_auto_compact_token_limit"] = auto_compact_limit
            options["model_auto_compact_token_limit_scope"] = "total"
        if selected.responses is not True and not catalog.any_model:
            warnings.append("O catálogo não confirma suporte à API Responses; a compatibilidade precisa ser validada na sessão.")
        command += ["--no-daemon"]
        if context.resume:
            command += ["resume", context.conversation or "--last"]
        command += ["--model", model]
        command += (["--dangerously-bypass-approvals-and-sandbox"] if context.bypass_permissions else
                    ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"])
        if context.controlled:
            # Valem no app-server para conversa nova e retomada: com --remote, a TUI não aceita flag de permissão
            # ao retomar ("Permission overrides are not supported when resuming a remote task").
            options.update(check_for_update_on_startup=False, approval_policy="never", sandbox_mode="danger-full-access")
        for key, value in options.items():
            command += ["-c", f"{key}={json.dumps(value, ensure_ascii=True)}"]
        if context.controlled:
            # Sessão aberta pelo orquestrador (que já validou o projeto): nada pode esperar clique. Confiança da
            # pasta só para esta pasta cadastrada e só neste processo (tabela inline TOML; nada é gravado em disco).
            # Os hooks não usam bypass: approve_codex_hooks grava a confiança deles antes de abrir.
            command += ["-c", f"projects={{{json.dumps(str(context.directory), ensure_ascii=True)}={{trust_level=\"trusted\"}}}}"]
        return LaunchPlan(self.ID, model, context.directory, command, context.environment, overrides, context.input_limit,
                          context_window, auto_compact_limit, {}, warnings, context.controlled, gateway.name,
                          (CODEX_KEY_VARIABLE,))

    def final_command(self, plan, directory: Path) -> tuple[list[str], str]:
        return prepare_codex_command(plan, directory, self.command)

    def run(self, plan, project: dict) -> int:
        if plan.controlled:
            return run_controlled_codex(plan, project["name"])
        return super().run(plan, project)

    def dry_run_extra(self, plan) -> dict:
        if not plan.controlled:
            return {}
        return dict(zip(("server", "tui"), controlled_codex_commands(plan, "ws://127.0.0.1:PORTA")))

    def has_previous_conversation(self, directory: Path) -> bool:
        """Sessão que o `resume --last` retoma: só as interativas (source cli/vscode). As de `codex exec` e as de
        subagente ficam no mesmo lugar, mas o resume as ignora e abre uma sessão nova (verificado no 0.162)."""
        for rollout in sorted(codex_sessions().glob("*/*/*/rollout-*.jsonl"), reverse=True):
            payload = _rollout_meta(rollout)
            if payload and same_folder(payload.get("cwd"), directory):
                return True
        return False

    def conversations(self, directory: Path) -> list[Conversation]:
        found = []
        for rollout in codex_sessions().glob("*/*/*/rollout-*.jsonl"):
            payload = _rollout_meta(rollout)
            if payload and same_folder(payload.get("cwd"), directory):
                conversation = codex_conversation(rollout, payload)
                if conversation:
                    found.append(conversation)
        return sorted(found, key=lambda item: item.updated, reverse=True)


HARNESS = Codex()


def approve_codex_hooks(directory: Path, environment: dict | None = None, command: list[str] | None = None,
                        timeout: float = 60, options: list[str] = ()) -> list[str]:
    """Sessão controlada do Codex: aprova, pelo app-server do próprio Codex, os hooks novos ou alterados daquela pasta
    (o mesmo que o "Trust all" da TUI: hooks/list e config/batchWrite em hooks.state com trusted_hash=currentHash),
    em conversa nova e na retomada. options são os -c do plano: sem a confiança da pasta (projects=...) o Codex nem
    lista os hooks do projeto. Devolve as chaves aprovadas; levanta LauncherError se o app-server recusar ou se,
    depois de gravar, algum hook da pasta continuar sem confiança."""
    import queue
    import threading
    process = subprocess.Popen((command or HARNESS.command()) + ["app-server", *options], cwd=directory, env=environment,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    replies = queue.Queue()
    threading.Thread(target=lambda: [replies.put(line) for line in process.stdout], daemon=True).start()

    def send(message: dict) -> None:
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        process.stdin.flush()

    def call(identifier: int, method: str, params: dict) -> dict:
        send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = json.loads(replies.get(timeout=max(0.1, deadline - time.monotonic())))
            except queue.Empty as error:
                raise LauncherError(f"O app-server do Codex não respondeu {method} em {timeout:.0f} s.") from error
            except ValueError:
                continue
            if message.get("id") == identifier:
                if "error" in message:
                    raise LauncherError(f"O app-server do Codex recusou {method}: {message['error']}")
                return message.get("result") or {}

    def untrusted(identifier: int) -> dict:
        listing = call(identifier, "hooks/list", {"cwds": [str(directory)]})
        return {hook.get("key"): hook.get("currentHash")
                for entry in listing.get("data", []) if path_key(entry.get("cwd", "")) == path_key(str(directory))
                for hook in entry.get("hooks", []) if hook.get("trustStatus") in ("untrusted", "modified")}

    try:
        call(1, "initialize", {"clientInfo": {"name": "agent-launcher", "version": "1"}})
        send({"method": "initialized"})
        pending = untrusted(2)
        if not pending:
            return []
        if not all(pending) or not all(pending.values()):
            raise LauncherError(f"O app-server do Codex listou hooks sem chave ou hash; não dá para confiá-los: {pending}")
        call(3, "config/batchWrite", {"edits": [{"keyPath": "hooks.state", "mergeStrategy": "upsert",
                                                 "value": {key: {"trusted_hash": digest} for key, digest in pending.items()}}],
                                      "filePath": None, "expectedVersion": None, "reloadUserConfig": True})
        left = untrusted(4)
        if left:
            raise LauncherError(f"O Codex continua sem confiar nestes hooks depois da aprovação: {', '.join(sorted(left))}")
        return sorted(pending)
    finally:
        stop_tree(process)


def prepare_codex_command(plan: LaunchPlan, directory: Path, resolve=None) -> tuple[list[str], str]:
    """Catálogo nativo temporário para o Codex aplicar o contexto do host a um ID com prefixo de provider.
    Sem metadados nativos correspondentes (modelo de outro fornecedor via roteador), segue sem o catálogo e avisa."""
    command = list(plan.command)
    if plan.cli != "codex" or plan.context_window is None:
        return command, ""
    environment = dict(plan.environment)
    environment["CODEX_HOME"] = str(directory / "native-config")
    try:
        Path(environment["CODEX_HOME"]).mkdir(exist_ok=True)
        result = subprocess.run(
            (resolve or HARNESS.command)() + ["debug", "models", "--bundled"],
            cwd=directory, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
        if result.returncode:
            raise ValueError("Catálogo nativo indisponível.")
        payload = json.loads(result.stdout)
        records = payload["models"]
        if not isinstance(records, list):
            raise ValueError("Formato de catálogo inválido.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        raise LauncherError(
            "Não foi possível ler os metadados do Codex instalado (debug models --bundled). "
            "A sessão não foi iniciada para evitar uma janela de contexto incorreta. Verifique a versão do CLI."
        ) from error
    candidates = [
        record for record in records
        if isinstance(record, dict) and isinstance(record.get("slug"), str) and record["slug"]
        and (plan.model == record["slug"] or any(plan.model.endswith(separator + record["slug"]) for separator in (".", "/")))
    ]
    if not candidates:
        return command, (f"O Codex não tem metadados nativos de {plan.model}: o contexto informado pelo host foi "
                         "repassado, mas o CLI pode usar uma janela menor.")
    record = dict(max(candidates, key=lambda candidate: len(candidate["slug"])))
    record.update({
        "slug": plan.model, "display_name": plan.model, "visibility": "list",
        "context_window": plan.context_window, "max_context_window": plan.context_window,
        "upgrade": None,
    })
    catalog_path = directory / "codex-model-catalog.json"
    atomic_json(catalog_path, {"models": [record]})
    command += ["-c", "model_catalog_json=" + json.dumps(str(catalog_path), ensure_ascii=True)]
    return command, ""


def controlled_codex_commands(plan: LaunchPlan, url: str, token_sha256: str = "<sha256>") -> tuple[list[str], list[str]]:
    """App-server autenticado (capability token) com o provider do plano (os -c) + TUI conectada por --remote."""
    base = HARNESS.command()
    options = codex_options(plan)
    server = base + ["app-server", "--listen", url, "--ws-auth", "capability-token", "--ws-token-sha256", token_sha256] + options
    # A TUI faz o aviso de atualização; a confiança dos hooks já foi gravada por approve_codex_hooks.
    unattended = [part for pair in zip(options[::2], options[1::2]) if pair[1].startswith(
        ("check_for_update_on_startup=", "projects=")) for part in pair]
    resuming = "resume" in plan.command
    tui = base + ["--remote", url, "--remote-auth-token-env", "ORQ_CODEX_TOKEN", "--model", plan.model, *unattended]
    if not resuming:
        tui.append("--dangerously-bypass-approvals-and-sandbox")
    if resuming:
        tui += plan.command[plan.command.index("resume"):plan.command.index("resume") + 2]  # --last ou o id escolhido
    return server, tui


def run_controlled_codex(plan: LaunchPlan, project: str) -> int:
    """Codex controlável: o orchestrador encontra o app-server pelo registro e manda turnos na janela visível.
    O registro (codex-controlled/<porta>.json) tem "project", "cwd", "url", "pid", "serverPid", "token", "startedAt"."""
    try:
        approved = approve_codex_hooks(plan.directory, plan.environment, options=codex_options(plan))
    except OSError as error:
        raise LauncherError(f"Não foi possível abrir o app-server do Codex para aprovar os hooks: {error}") from error
    if approved:
        print(f"  Hooks aprovados para a sessão controlada: {len(approved)}")
    port = free_port()
    url = f"ws://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    server_command, tui_command = controlled_codex_commands(plan, url, hashlib.sha256(token.encode()).hexdigest())
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    server = subprocess.Popen(server_command, cwd=plan.directory, env=plan.environment, shell=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    job = kill_with_parent(server)
    registry = controlled_state.CODEX_CONTROLLED_DIR / f"{port}.json"
    try:
        deadline = time.monotonic() + 30
        while True:
            if server.poll() is not None:
                raise LauncherError(f"O app-server do Codex saiu ao iniciar (código {server.returncode}).")
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f"http://127.0.0.1:{port}/readyz", timeout=2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise LauncherError("O app-server do Codex não ficou pronto em 30 s.")
                time.sleep(0.3)
        controlled_state.CODEX_CONTROLLED_DIR.mkdir(parents=True, exist_ok=True)
        atomic_json(registry, {"project": project, "cwd": str(plan.directory), "url": url, "pid": os.getpid(),
                              "serverPid": server.pid, "token": token, "startedAt": time.time()})
        tui = subprocess.Popen(tui_command, cwd=plan.directory, env={**plan.environment, "ORQ_CODEX_TOKEN": token}, shell=False)
        while True:
            try:
                return tui.wait()
            except KeyboardInterrupt:
                continue
    finally:
        registry.unlink(missing_ok=True)
        stop_tree(server)
        close_job(job)
