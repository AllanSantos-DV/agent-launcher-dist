"""Reabrir várias sessões de uma vez, por exemplo depois de reiniciar o Windows.

A lista vem das aberturas das últimas RECENT_HOURS horas no registro do perfil (session/registry.py), menos o que está
aberto agora, uma por projeto e CLI (a mais recente). Cada linha leva a conversa exata (session/conversations.py); sem
certeza de qual é, a linha fica desabilitada com o motivo, nunca retoma outra. Abrir é em sequência, uma aba por vez,
pelo caminho de segundo plano do start_session."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import project_path
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import registry as harnesses
from agent_launcher.platform import keys
from agent_launcher.session import registry, worktree as worktrees
from agent_launcher.session.conversations import conversation_of_opening, conversations_for

RECENT_HOURS = 24
NOT_IDENTIFIED = "conversa não identificada"


def cli_label(cli: str) -> str:
    harness = harnesses.find(cli)
    return harness.LABEL if harness else cli


@dataclass(frozen=True)
class Reopening:
    """Uma sessão que estava aberta. `reason` vazio: dá para reabrir; preenchido: por que a linha fica desabilitada."""
    project_id: str
    name: str
    folder: str
    cli: str
    connection: str
    model: str
    at: float
    conversation: str | None
    reason: str | None
    worktree: str | None = None

    @property
    def selectable(self) -> bool:
        return self.reason is None

    def as_json(self) -> dict:
        moment = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.at))
        found = {"project": self.name, "folder": self.folder, "cli": self.cli, "connection": self.connection,
                 "model": self.model, "last_opened": moment, "conversation": self.conversation,
                 "selectable": self.selectable, "reason": self.reason}
        if self.worktree:
            found["worktree"] = self.worktree
        return found


def _worktree_of(project: dict | None, entry: dict) -> str | None:
    """O nome do worktree em que a abertura do registro aconteceu (`launch --worktree`); None se foi na pasta do projeto."""
    if project is None:
        return None
    try:
        return worktrees.name_of(project_path(project["path"]), entry["folder"])
    except LauncherError:
        return None


def _row(store: Store, entry: dict) -> Reopening:
    project_id = str(entry.get("project") or "")
    project = next((item for item in store.projects if item["id"] == project_id), None)
    worktree = _worktree_of(project, entry)
    cli = str(entry.get("cli") or "")
    harness = harnesses.find(cli)
    conversation, reason = None, None
    if project is None:
        reason = "projeto não cadastrado"
    elif harness is None or harness.KIND != "terminal":
        reason = "CLI indisponível"
    else:
        try:
            directory = project_path(project["path"])
        except LauncherError:
            reason = "pasta do projeto não encontrada"
        else:
            where = Path(entry["folder"]) if worktree else directory
            found = conversations_for(store.home, harness, where)
            conversation = conversation_of_opening(store.home, cli, where, entry, found)
            reason = None if conversation else NOT_IDENTIFIED
    return Reopening(project_id=project_id, name=project["name"] if project else project_id or "?",
                     folder=entry["folder"], cli=cli, connection=str(entry.get("connection") or ""),
                     model=str(entry.get("model") or ""), at=float(entry["at"]), conversation=conversation,
                     reason=reason, worktree=worktree)


def candidates(store: Store, hours: float = RECENT_HOURS, now: float | None = None, open_now=None) -> list[Reopening]:
    """As sessões que dá para reabrir, da mais recente para a mais antiga: aberturas das últimas `hours` horas, sem as
    que estão abertas agora (`open_now`, padrão platform/keys.py:sessions), uma por projeto e CLI, e uma só linha para
    aberturas que apontam para a mesma conversa."""
    since = (time.time() if now is None else now) - hours * 3600
    running = {(item.get("project_id"), item.get("cli")) for item in (open_now or keys.sessions)()}
    projects = {item["id"]: item for item in store.projects}
    latest: dict[tuple, dict] = {}
    for entry in registry.entries(store.home):
        at = entry.get("at")
        if not isinstance(at, (int, float)) or at < since:
            continue
        key = (entry.get("project"), entry.get("cli"), _worktree_of(projects.get(entry.get("project")), entry))
        if key[:2] not in running and at >= latest.get(key, {}).get("at", at):
            latest[key] = entry
    rows = sorted((_row(store, entry) for entry in latest.values()), key=lambda row: row.at, reverse=True)
    seen: set[tuple] = set()
    unique = []
    for row in rows:
        if row.conversation is not None:
            if (row.cli, row.conversation) in seen:
                continue
            seen.add((row.cli, row.conversation))
        unique.append(row)
    return unique


def open_all(store: Store, rows: list[Reopening], start=None) -> list[dict]:
    """Abre cada linha em sequência, retomando a conversa dela como aba da janela única, em segundo plano (sem tirar o
    foco; start_session mantém a aba atrás). Falha numa aba vira o resultado dela e não impede as outras. Linha
    desabilitada é erro de quem chama: nunca abre. Um resultado por linha: project, cli, ok e, se falhou, error."""
    refused = next((row for row in rows if not row.selectable), None)
    if refused is not None:
        raise LauncherError(f"{refused.name} ({cli_label(refused.cli)}) não pode ser reaberta: {refused.reason}.")
    if start is None:
        from agent_launcher.session.window import start_session as start
    results = []
    for row in rows:
        result = {"project": row.name, "cli": row.cli, "conversation": row.conversation}
        try:
            start(store, store.find(row.project_id), row.cli, row.model, resume=True,
                  connection=row.connection or None, background=True, minimized=True, conversation=row.conversation,
                  **({"worktree": row.worktree} if row.worktree else {}))
        except (LauncherError, OSError, ValueError) as error:
            results.append({**result, "ok": False, "error": str(error)})
        else:
            results.append({**result, "ok": True})
    return results


def describe(results: list[dict]) -> str:
    """O resumo para a pessoa: quantas abriram e, de cada uma que não abriu, projeto, CLI e motivo."""
    opened = sum(1 for result in results if result["ok"])
    failed = [result for result in results if not result["ok"]]
    text = f"{opened} {'sessão reaberta' if opened == 1 else 'sessões reabertas'}."
    if failed:
        lines = [f"{result['project']} ({cli_label(result['cli'])}): {result['error']}" for result in failed]
        text += f" {len(failed)} não abriram:\n" + "\n".join(lines)
    return text
