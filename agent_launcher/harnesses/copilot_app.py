"""GitHub Copilot app: provider OpenAI-compatível sem chave em ~/.copilot/data.db,
apontando para o endpoint local da conexão. O launcher não abre sessão no app; configura o provider e abre o app.

O formato é interno e não documentado (tabelas model_providers e provider_models). Por isso:
- só grava depois de confirmação do usuário, com o app fechado (fecha inclusive o da bandeja) e com backup do banco;
- confere o esquema antes (colunas e o CHECK de wire_api_override); esquema diferente = não grava e explica o manual;
- só mexe no provider "Agent Launcher — <conexão>" (nunca nos do usuário) e não faz nada se ele já está certo.
Claude e o resto vão por Chat Completions (o app só fala OpenAI-compatível aqui); GPT-5+ por Responses."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from agent_launcher.domain.catalog import Catalog, ModelInfo, prefers_responses, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.harnesses.base import Harness
from agent_launcher.platform import terminal

PROVIDER_PREFIX = "Agent Launcher"
MODELS_LIMIT = 60
REQUIRED_COLUMNS = {
    "model_providers": {"id", "name", "type", "settings_json"},
    "provider_models": {"id", "provider_id", "model_id", "display_name", "max_prompt_tokens", "max_output_tokens",
                        "wire_api_override"},
}


def app_executable() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "Programs" / "GitHub Copilot" / "github.exe"


def database() -> Path:
    return Path(os.environ.get("AGENT_LAUNCHER_COPILOT_DB") or Path.home() / ".copilot" / "data.db")


def check_schema(connection: sqlite3.Connection) -> None:
    for table, columns in REQUIRED_COLUMNS.items():
        found = {row[1] for row in connection.execute(f"pragma table_info({table})")}
        if not columns <= found:
            raise LauncherError(f"O banco do Copilot app mudou de formato (faltam {sorted(columns - found)} em {table}). "
                                "Configure pelo app: Settings → Model providers → OpenAI-compatible, com a URL do "
                                "endpoint local e os IDs mostrados no launcher.")


def wanted(gateway, catalog: Catalog, selected: ModelInfo) -> list[tuple]:
    """(model_id, nome, entrada, saída, wire_api_override) para os modelos da conexão que o app consegue usar."""
    records = [record for record in catalog.records if record.chat and record.tools is not False]
    if len(records) > MODELS_LIMIT:
        records = [record for record in records if record.provider == selected.provider][:MODELS_LIMIT] or [selected]
    if selected.id not in {record.id for record in records}:
        records.insert(0, selected)
    rows = []
    for record in records:
        responses = prefers_responses(record) and record.responses is not False and "responses" in gateway.protocols
        if not responses and "chat_completions" not in gateway.protocols:
            continue
        rows.append((record.id, record.id, record.max_input_tokens if catalog.verified else None,
                     record.max_output_tokens if catalog.verified else None, "responses" if responses else None))
    return rows


def settings(base: str) -> dict:
    return {"baseUrl": f"{base}/v1", "wireApi": "completions", "authKind": "none", "headersJson": "{}"}


def read_provider(connection: sqlite3.Connection, name: str):
    row = connection.execute("select id, type, settings_json from model_providers where name = ?", (name,)).fetchone()
    if not row:
        return None, set()
    models = {tuple(item) for item in connection.execute(
        "select model_id, display_name, max_prompt_tokens, max_output_tokens, wire_api_override from provider_models "
        "where provider_id = ?", (row[0],))}
    try:
        current = json.loads(row[2])
    except ValueError:
        current = {}
    return {"id": row[0], "type": row[1], "settings": current}, models


def is_current(provider, models: set, base: str, rows: list[tuple]) -> bool:
    return bool(provider) and provider["type"] == "openai" and provider["settings"] == settings(base) and models == set(rows)


def app_processes() -> list[int]:
    """PIDs do Copilot app em execução. Falha da consulta levanta: lista vazia faria o launcher gravar no banco com o app aberto."""
    script = ("Get-CimInstance Win32_Process | ? { $_.ExecutablePath -like '*\\GitHub Copilot\\github.exe' } "
              "| % ProcessId")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                                text=True, env=terminal.powershell_env(), timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LauncherError(f"Não foi possível listar os processos do Copilot app: {error}") from error
    if result.returncode:
        raise LauncherError("Não foi possível listar os processos do Copilot app: "
                            f"{(result.stderr or result.stdout).strip()[-300:] or 'código ' + str(result.returncode)}")
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def close_app(timeout: float = 20) -> bool:
    """Fecha o app inteiro (janela, bandeja e os copilot.exe --server filhos). True se estava aberto."""
    pids = app_processes()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=flags)
    deadline = time.monotonic() + timeout
    while app_processes() and time.monotonic() < deadline:
        time.sleep(0.5)
    for pid in app_processes():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=flags)
    return bool(pids)


def wait_unlocked(db: Path, timeout: float = 20, interval: float = 0.5) -> None:
    """Espera o banco liberar a escrita: logo depois de fechar o app, o arquivo ainda fica preso por um instante (a 1ª
    gravação deu 'disk I/O error'). Reabre a conexão a cada tentativa."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        connection = None
        try:
            connection = sqlite3.connect(db, timeout=1)
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            return
        except sqlite3.OperationalError as error:
            last = error
        finally:
            if connection is not None:
                connection.close()
        if time.monotonic() >= deadline:
            raise LauncherError(f"O banco do Copilot app continua em uso depois de {timeout:.0f} s ({last}). Feche o "
                                f"app (inclusive na bandeja) e tente de novo.")
        time.sleep(interval)


def write(db: Path, name: str, base: str, rows: list[tuple], wait: float = 20) -> str:
    """Backup consistente (inclui o WAL) e gravação numa transação. Devolve o caminho do backup."""
    wait_unlocked(db, wait)
    return retry_busy(lambda: _write(db, name, base, rows), wait)


def _write(db: Path, name: str, base: str, rows: list[tuple]) -> str:
    backup = db.with_name(f"{db.name}.agent-launcher-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(db)
    try:
        check_schema(source)
        target = sqlite3.connect(backup)
        source.backup(target)
        target.close()
        provider, _models = read_provider(source, name)
        identifier = provider["id"] if provider else str(uuid.uuid4())
        with source:
            if provider:
                source.execute("update model_providers set type='openai', settings_json=?, "
                               "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') where id=?",
                               (json.dumps(settings(base)), identifier))
                source.execute("delete from provider_models where provider_id=?", (identifier,))
            else:
                source.execute("insert into model_providers (id, name, type, settings_json) values (?,?,?,?)",
                               (identifier, name, "openai", json.dumps(settings(base))))
            for model_id, display, prompt, output, wire in rows:
                source.execute("insert into provider_models (id, provider_id, model_id, display_name, max_prompt_tokens, "
                               "max_output_tokens, wire_api_override) values (?,?,?,?,?,?,?)",
                               (str(uuid.uuid4()), identifier, model_id, display, prompt, output, wire))
    finally:
        source.close()
    return str(backup)


def retry_busy(action, wait: float = 20, interval: float = 0.5):
    """Repete a operação no banco enquanto ele estiver preso. Logo depois de fechar o app (sobretudo quando ele acabou de
    reabrir), o BEGIN IMMEDIATE já passa mas o backup ou a gravação ainda dão 'disk I/O error' ou 'database is locked':
    visto ao remover dois providers seguidos com o app aberto."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return action()
        except sqlite3.OperationalError as error:
            text = str(error).lower()
            if not ("disk i/o" in text or "locked" in text or "busy" in text) or time.monotonic() >= deadline:
                raise
        time.sleep(interval)


def delete_provider(db: Path, name: str, wait: float = 20) -> str | None:
    """Backup consistente e remoção do provider (e dos modelos dele) numa transação. None se ele não existe."""
    wait_unlocked(db, wait)
    return retry_busy(lambda: _delete_provider(db, name), wait)


def _delete_provider(db: Path, name: str) -> str | None:
    source = sqlite3.connect(db)
    try:
        check_schema(source)
        provider, _models = read_provider(source, name)
        if not provider:
            return None
        backup = db.with_name(f"{db.name}.agent-launcher-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        target = sqlite3.connect(backup)
        source.backup(target)
        target.close()
        with source:
            source.execute("delete from provider_models where provider_id=?", (provider["id"],))
            source.execute("delete from model_providers where id=?", (provider["id"],))
    finally:
        source.close()
    return str(backup)


def remove_provider(name: str, confirm=lambda question: True, db: Path | None = None, wait: float = 20) -> str | None:
    """Tira o provider \"Agent Launcher — … — <conexão>\" do app, com as regras da gravação:
    só com o banco presente, esquema conferido e o provider lá; depois da confirmação fecha o app (se aberto), espera o
    banco, faz backup, apaga e reabre o app se ele estava aberto. Devolve a mensagem, ou None quando não havia nada."""
    if not name.startswith(PROVIDER_PREFIX):
        raise LauncherError(f"O launcher só remove os providers dele ({PROVIDER_PREFIX} — …), não {name!r}.")
    db = Path(db) if db else database()
    if not db.is_file():
        return None
    reader = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        check_schema(reader)
        provider, _models = read_provider(reader, name)
    finally:
        reader.close()
    if not provider:
        return None
    running = bool(app_processes())
    question = (f"Remover o provider \"{name}\" do Copilot app?"
                + ("\n\nO app será fechado e aberto de novo (inclusive o ícone da bandeja)." if running else "")
                + "\n\nO launcher faz backup do banco do app antes e não mexe nos seus outros providers.")
    if not confirm(question):
        return "Nada foi removido do Copilot app."
    if running:
        close_app()
    try:
        backup = delete_provider(db, name, wait)
    finally:
        if running:  # reabre mesmo se a remoção falhar: o usuário não fica sem o app
            executable = app_executable()
            if executable.is_file():
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen([str(executable)], close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Provider \"{name}\" removido do Copilot app." + (f" Backup do banco: {backup}." if backup else "")


class CopilotApp(Harness):
    ID = "copilot_app"
    LABEL = "Copilot app"
    KIND = "app"
    CONSUMES = ("chat_completions", "responses")
    CAPABILITIES = frozenset({"context_window"})
    SUBTITLE = "GitHub Copilot desktop"
    SYMBOL = "app"
    PREVIEW = True
    SESSION_HINT = "Provider da conexão no app"
    MISSING_HINT = "Instale o GitHub Copilot app (github.com/features/copilot) e abra de novo."

    def find_executable(self) -> str | None:
        executable = app_executable()
        return str(executable) if executable.is_file() else None

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        if not record.chat or record.tools is False:
            return False
        responses = prefers_responses(record) and record.responses is not False and "responses" in catalog.protocols
        return responses or "chat_completions" in catalog.protocols

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        wire = "Responses" if prefers_responses(record) and "responses" in catalog.protocols else "Chat Completions"
        return (f"Entrada declarada: {token_label(record.max_input_tokens).replace('.', ',')} tokens",
                f"No app: provider \"{PROVIDER_PREFIX}\" pelo endpoint local ({wire}); o app não recebe chave nem host.")

    def command(self) -> list[str]:
        executable = self.find_executable()
        if not executable:
            raise LauncherError(f"{self.LABEL} não encontrado. {self.MISSING_HINT}")
        return [executable]

    def plan(self, context):
        raise LauncherError("O Copilot app abre pela interface ou por 'launch --cli copilot_app', não num terminal.")

    @staticmethod
    def provider_name(gateway) -> str:
        from agent_launcher.domain import profiles
        profile = profiles.label(gateway.store.home)
        return f"{PROVIDER_PREFIX} — {profile} — {gateway.name}" if profile else f"{PROVIDER_PREFIX} — {gateway.name}"

    def preview(self, store, project: dict, gateway, model: str, catalog: Catalog) -> dict:
        from agent_launcher.endpoint import registry as endpoints
        selected = catalog.find(model) or ModelInfo(model)
        base = f"http://127.0.0.1:{endpoints.port_for(store.home, gateway.connection['id'], reserve=False)}"
        rows = wanted(gateway, catalog, selected)
        current = None
        if database().is_file():
            connection = sqlite3.connect(f"file:{database().as_posix()}?mode=ro", uri=True)
            try:
                check_schema(connection)
                provider, models = read_provider(connection, self.provider_name(gateway))
                current = is_current(provider, models, base, rows)
            finally:
                connection.close()
        return {"cli": self.ID, "model": selected.id, "connection": gateway.name, "provider": self.provider_name(gateway),
                "base_url": f"{base}/v1", "models": [row[0] for row in rows], "database": str(database()),
                "already_configured": current}

    def open(self, store, project: dict, gateway, model: str, catalog: Catalog, confirm=lambda question: True) -> str:
        from agent_launcher.endpoint import registry as endpoints
        executable = self.find_executable()
        if not executable:
            raise LauncherError(f"{self.LABEL} não encontrado. {self.MISSING_HINT}")
        db = database()
        if not db.is_file():
            raise LauncherError("O Copilot app ainda não criou o banco dele: abra o app uma vez, entre com a conta "
                                "GitHub e tente de novo.")
        selected = catalog.find(model) or ModelInfo(model)
        port = endpoints.ensure_running(store.home, gateway.connection["id"])
        base = f"http://127.0.0.1:{port}"
        rows = wanted(gateway, catalog, selected)
        name = self.provider_name(gateway)
        reader = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            check_schema(reader)
            provider, models = read_provider(reader, name)
        finally:
            reader.close()
        message = f"Copilot app com o provider \"{name}\" ({len(rows)} modelos, endpoint local 127.0.0.1:{port})."
        if not is_current(provider, models, base, rows):
            running = bool(app_processes())
            question = (f"Gravar o provider \"{name}\" no Copilot app com {len(rows)} modelos de {gateway.name}?"
                        + ("\n\nO app será fechado e aberto de novo (inclusive o ícone da bandeja)." if running else "")
                        + "\n\nO launcher faz backup do banco do app antes e não mexe nos seus outros providers.")
            if not confirm(question):
                return "Nada foi gravado no Copilot app."
            close_app()
            backup = write(db, name, base, rows)
            message += f" Backup do banco: {backup}."
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([executable], close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return message + f" Escolha {selected.id} no seletor de modelo do app."


HARNESS = CopilotApp()
