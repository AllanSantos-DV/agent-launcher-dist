"""Estado e última atividade das sessões abertas (campo `activity` de cada sessão do `launcher status`).

`last_activity` é o carimbo da última gravação do transcript da sessão: o arquivo do Claude Code (~/.claude/projects),
o rollout do Codex (~/.codex/sessions) ou os eventos do Copilot CLI (~/.copilot/session-state), na mesma leitura que a
lista de conversas usa (`harness.conversations`). O launcher só lê; nunca grava nem toca o transcript.

`state` vem só da idade dessa gravação: "working" quando tem menos de WORKING_WINDOW_S segundos, "idle" a partir daí,
"unknown" quando não há transcript seguro para a sessão. Não diz se o CLI espera por você: uma geração longa ou uma
ferramenta lenta, sem gravar por mais de WORKING_WINDOW_S, aparece como "idle".

Qual transcript é da sessão: o registro de sessões abertas (platform/keys.py) sabe pid, projeto, CLI e quando a sessão
abriu; o registro do perfil (session/registry.py) guarda cada abertura, na pasta do projeto ou na do worktree dele
(session/worktree.py). Cada abertura pertence à sessão aberta do mesmo projeto e CLI que registrou mais recentemente
antes dela (até OPENING_WINDOW_S depois), e a sessão só vale com exatamente uma abertura sua. Daí
`conversations.conversation_of_opening` dá a conversa, na pasta em que a abertura aconteceu: Claude novo e retomada por id
pelo id gravado; Codex e Copilot CLI novos pela primeira conversa que começou depois da abertura, se nenhuma outra abertura
a reivindica. Em qualquer dúvida (duas sessões abertas juntas, abertura sem id como `--continue`, conversa que ainda não
tem transcript, projeto de outro perfil, registro do perfil sem a abertura) a resposta é "unknown", nunca um palpite; e se
o mesmo projeto e CLI tem aberturas na pasta do projeto e em worktree e alguma sessão aberta ficou sem atribuição certa,
todas as sessões desse projeto e CLI ficam "unknown"."""
from __future__ import annotations

import time
from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import path_key
from agent_launcher.session import conversations, registry

WORKING_WINDOW_S = 30  # Claude, Codex e Copilot gravam a cada mensagem e a cada ferramenta; 30 s cobre o intervalo comum entre elas
OPENING_WINDOW_S = 120  # a abertura é gravada logo após o registro da sessão; a espera pela conexão (BOOT_RETRY_DELAYS) soma 30 s
UNKNOWN = {"state": "unknown", "last_activity": None}


def _project_entries(home: Path, project_folder: str, cli: str) -> list[tuple[dict, str, bool]]:
    """As aberturas do projeto neste CLI como (abertura, pasta onde abriu, em worktree?): na pasta do projeto e nos worktrees
    dele (`<projeto>.worktrees/<nome>`)."""
    from agent_launcher.session import worktree
    found = []
    for entry in registry.entries(home, None, cli):
        if registry.same_folder(entry["folder"], project_folder):
            found.append((entry, project_folder, False))
        elif worktree.name_of(Path(project_folder), entry["folder"]):
            found.append((entry, entry["folder"], True))
    return found


def _openings(home: Path, sessions: list[dict], folders: dict[tuple, str]) -> dict[int, tuple[dict, str]]:
    """pid -> (a única abertura do registro do perfil que é dessa sessão, a pasta em que ela abriu); ver o docstring do módulo."""
    groups: dict[tuple, list[dict]] = {}
    for session in sessions:
        if isinstance(session.get("startedAt"), (int, float)) and (session.get("project_id"), session.get("cli")) in folders:
            groups.setdefault((session["project_id"], session["cli"]), []).append(session)
    settled: dict[int, tuple[dict, str]] = {}
    for key, group in groups.items():
        claims: dict[int, list[tuple[dict, str, bool]]] = {}
        ambiguous: set[int] = set()
        for entry, folder, in_worktree in _project_entries(home, folders[key], key[1]):
            if not isinstance(entry.get("at"), (int, float)):
                continue
            before = [item for item in group if item["startedAt"] <= entry["at"]]
            if not before:
                continue
            latest = max(item["startedAt"] for item in before)
            owners = [item for item in before if item["startedAt"] == latest]
            if entry["at"] - latest > OPENING_WINDOW_S:
                continue
            for owner in owners:
                claims.setdefault(int(owner["pid"]), []).append((entry, folder, in_worktree))
                if len(owners) > 1:
                    ambiguous.add(int(owner["pid"]))
        sure = {pid: (found[0][0], found[0][1]) for pid, found in claims.items() if len(found) == 1 and pid not in ambiguous}
        mixed = {in_worktree for found in claims.values() for _entry, _folder, in_worktree in found} == {False, True}
        if mixed and any(int(item["pid"]) not in sure for item in group):
            continue
        settled.update(sure)
    return settled


def _state(updated: float, now: float) -> str:
    return "working" if now - updated < WORKING_WINDOW_S else "idle"


def annotate(store, sessions: list[dict], now: float | None = None) -> list[dict]:
    """As mesmas sessões, cada uma com `activity` = {"state", "last_activity"} (ISO com fuso; None em "unknown").
    `now` (epoch) é o relógio; sem ele vale o do sistema."""
    from agent_launcher.harnesses import registry as harnesses
    moment = time.time() if now is None else now
    projects: dict[str, dict] = {}
    for session in sessions:
        try:
            project = store.find(str(session.get("project_id")))
        except LauncherError:
            continue
        if project["id"] == session.get("project_id"):
            projects[project["id"]] = project
    folders = {(session["project_id"], session.get("cli")): projects[session["project_id"]]["path"]
               for session in sessions if session.get("project_id") in projects}
    openings = _openings(store.home, sessions, folders)
    found: dict[tuple, list] = {}
    annotated = []
    for session in sessions:
        result = dict(UNKNOWN)
        opening = openings.get(int(session["pid"]))
        harness = harnesses.find(session.get("cli", ""))
        key = (session.get("project_id"), session.get("cli"))
        if opening is not None and harness is not None and conversations.lists_conversations(harness) and key in folders:
            entry, folder = opening
            where = (key, path_key(folder))
            try:
                if where not in found:
                    found[where] = harness.conversations(Path(folder), detail=False)
                identifier = conversations.conversation_of_opening(store.home, harness.ID, folder, entry, found[where])
            except OSError:
                identifier = None
            conversation = next((item for item in found.get(where, []) if item.id == identifier), None)
            if conversation is not None:
                result = {"state": _state(conversation.updated, moment),
                          "last_activity": conversation.as_json()["updated"]}
        annotated.append({**session, "activity": result})
    return annotated
