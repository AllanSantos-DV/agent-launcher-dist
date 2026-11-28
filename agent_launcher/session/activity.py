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
from agent_launcher.platform import keys
from agent_launcher.session import conversations, registry

WORKING_WINDOW_S = 30  # Claude, Codex e Copilot gravam a cada mensagem e a cada ferramenta; 30 s cobre o intervalo comum entre elas
OPENING_WINDOW_S = 120  # a abertura é gravada logo após o registro da sessão; a espera pela conexão (BOOT_RETRY_DELAYS) soma 30 s
USAGE_BUDGET_S = 10  # teto do tempo lendo transcripts para o uso; o que passar disso volta como ausente
IDLE_WAIT_TIMEOUT_S = 300.0  # prazo padrão de `keys --wait-idle`: "idle" já pede WORKING_WINDOW_S de silêncio, então o de `--wait` (30 s) nunca bastaria
IDLE_POLL_S = 2.0  # cada leitura lista as conversas da pasta do projeto
IDLE_CONFIRM_S = 1.0  # intervalo entre as duas leituras "idle" seguidas, para o CLI gravar a mensagem que acabou de receber
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


def annotate(store, sessions: list[dict], now: float | None = None, with_usage: bool = False,
             budget_s: float | None = None) -> list[dict]:
    """As mesmas sessões, cada uma com `activity` = {"state", "last_activity"} (ISO com fuso; None em "unknown").
    `now` (epoch) é o relógio; sem ele vale o do sistema. Com `with_usage`, `activity` ganha depois dessas duas chaves as
    de `activity_usage` (contexto, custo estimado, aviso de janela e `usage_reason`), com teto de `budget_s` segundos
    (padrão USAGE_BUDGET_S) de leitura; sem ele nenhum transcript é lido além da data."""
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
    results = []
    targets = []
    for session in sessions:
        result = dict(UNKNOWN)
        target = (session.get("cli"), None, None)
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
                target = (session.get("cli"), conversation.id, entry)
        results.append(result)
        targets.append(target)
    if with_usage:
        from agent_launcher.session import activity_usage
        extras = activity_usage.read(store, targets, USAGE_BUDGET_S if budget_s is None else budget_s)
        results = [{**result, **extra} for result, extra in zip(results, extras)]
    return [{**session, "activity": result} for session, result in zip(sessions, results)]


def session_item(store, pid: int, now: float | None = None) -> dict:
    """A sessão aberta `pid` como `annotate` a devolve. Anota todas as abertas, não só ela: duas sessões abertas juntas no
    mesmo projeto e CLI só se distinguem olhando as duas. Sessão que não está aberta é LauncherError."""
    for item in annotate(store, keys.sessions(), now):
        if item.get("pid") == pid:
            return item
    raise LauncherError(f"A sessão (pid {pid}) não está mais aberta.")


def _unknown_reason(item: dict) -> str:
    from agent_launcher.harnesses import registry as harnesses
    harness = harnesses.find(item.get("cli", ""))
    if harness is None or not conversations.lists_conversations(harness):
        return f"o launcher não lê transcript do CLI {item.get('cli')!r}."
    return ("o launcher não achou com certeza o transcript dela. Pode ser: duas sessões do mesmo projeto e CLI abertas "
            "juntas, sessão retomada sem id de conversa, conversa ainda sem transcript ou projeto de outro perfil.")


def wait_idle(store, pid: int, timeout: float, poll: float = IDLE_POLL_S) -> dict:
    """Relê o estado da sessão `pid` a cada `poll` s até ele ser "idle" e devolve a `activity` dela. Passado `timeout` s
    ainda "working" é LauncherError; o estado é lido ao menos uma vez. "unknown" é LauncherError na hora, com o motivo.
    "idle" é a idade da última gravação do transcript (WORKING_WINDOW_S): vale como "sem atividade por 30 s", não como
    "terminou de pensar"; uma geração ou ferramenta longa que não grava por esse tempo já conta como parada.
    "idle" só vale com duas leituras seguidas, IDLE_CONFIRM_S s uma da outra (a confirmação pode passar do prazo por esse
    tempo). Corrida que sobra: logo depois de enviar teclas, se o CLI demorar mais que esse intervalo para gravar a
    mensagem recebida e a sessão já estava parada, as duas leituras ainda dizem "idle" e a espera volta cedo demais."""
    if timeout < 0:
        raise LauncherError(f"O prazo da espera não pode ser negativo ({timeout:g} s).")
    end = time.monotonic() + timeout
    confirming = False
    while True:
        item = session_item(store, pid)
        state = item["activity"]["state"]
        if state == "idle":
            if confirming:
                return item["activity"]
            confirming = True
            time.sleep(IDLE_CONFIRM_S)
            continue
        confirming = False
        if state == "unknown":
            raise LauncherError(f"Não dá para saber se a sessão (pid {pid}) terminou de trabalhar: {_unknown_reason(item)} "
                                "Use --wait TEXTO, que olha a tela.")
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise LauncherError(f"A sessão (pid {pid}) não ficou {WORKING_WINDOW_S} s sem atividade no transcript em "
                                f"{timeout:g} s.")
        time.sleep(min(poll, remaining))
