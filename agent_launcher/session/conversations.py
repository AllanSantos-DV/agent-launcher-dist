"""Conversas do projeto por perfil.

O histórico dos CLIs é por pasta; quando a mesma pasta está em mais de um perfil, cada perfil só vê (e só retoma) as
conversas que ele abriu, segundo o registro de sessões (session/registry.py):
- Claude Code: pelos ids (o launcher passa --session-id na sessão nova; retomadas guardam o id);
- Codex e Copilot CLI (sem id na abertura): a primeira conversa que começou até 10 min depois de cada abertura nova
  deste perfil, mais as retomadas por id.
Pasta só neste perfil: todas as conversas da pasta. Só biblioteca padrão (a janela de sessão importa)."""
from __future__ import annotations

import re
from pathlib import Path

from agent_launcher.domain import profiles
from agent_launcher.domain.errors import LauncherError
from agent_launcher.harnesses.base import Conversation, Harness
from agent_launcher.session import registry

ATTRIBUTION_WINDOW = 600  # s: a conversa nova do Codex/Copilot aparece até aqui depois da abertura
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def lists_conversations(harness: Harness) -> bool:
    return type(harness).conversations is not Harness.conversations


def shared_folder(home: Path | None, directory) -> bool:
    """A pasta também está cadastrada em outro perfil (o histórico dos CLIs é compartilhado)?"""
    if home is None:
        return False
    try:
        return bool(profiles.other_profiles_with_path(str(directory), Path(home)))
    except (LauncherError, OSError, ValueError):
        return False


def attributed(home: Path, cli: str, directory, conversations: list[Conversation]) -> list[Conversation]:
    """As conversas que este perfil abriu (ver o docstring do módulo)."""
    opened = registry.entries(home, directory, cli)
    known = {entry["session_id"] for entry in opened if isinstance(entry.get("session_id"), str)}
    if cli != "claude":
        moments = sorted(float(entry["at"]) for entry in opened
                         if not entry.get("resume") and isinstance(entry.get("at"), (int, float)))
        ordered = sorted(conversations, key=lambda item: item.started)
        for moment in moments:
            first = next((item for item in ordered if moment <= item.started < moment + ATTRIBUTION_WINDOW), None)
            if first is not None:
                known.add(first.id)
    return [item for item in conversations if item.id in known]


def conversations_for(home: Path | None, harness: Harness, directory) -> list[Conversation]:
    """Conversas da pasta que este perfil pode ver, mais recente primeiro."""
    found = harness.conversations(Path(directory))
    if found and shared_folder(home, directory):
        return attributed(Path(home), harness.ID, directory, found)
    return found


def has_conversation_for(home: Path | None, cli: str, directory) -> bool:
    """Há conversa para 'Retomar última' neste perfil? Pasta compartilhada: só as deste perfil."""
    from agent_launcher.harnesses import registry as harnesses
    harness = harnesses.find(cli)
    if harness is None:
        return False
    if lists_conversations(harness) and shared_folder(home, directory):
        return bool(conversations_for(home, harness, directory))
    return harness.has_previous_conversation(Path(directory))


def resume_target(home: Path | None, harness: Harness, directory: Path, conversation: str | None,
                  warnings: list[str]) -> tuple[bool, str | None]:
    """(retomar?, id) para o plano. Com id: confere que é uma conversa desta pasta. Sem id: a última (--continue), ou,
    em pasta compartilhada, a última deste perfil por id. Sem conversa: sessão nova com aviso."""
    cli = harness.ID
    if conversation:
        if not ID_PATTERN.fullmatch(str(conversation)):
            raise LauncherError(f"ID de conversa inválido: {conversation}")
        if lists_conversations(harness) and not any(item.id == conversation
                                                    for item in conversations_for(home, harness, directory)):
            warnings.append(f"Conversa {conversation} do {cli} não encontrada nesta pasta: abrindo uma sessão nova.")
            return False, None
        return True, conversation
    if lists_conversations(harness) and shared_folder(home, directory):
        mine = conversations_for(home, harness, directory)
        if mine:
            return True, mine[0].id
    elif harness.has_previous_conversation(directory):
        return True, None
    # Retomar sem conversa anterior falha ("No conversation found to continue") e a janela para esperando ENTER;
    # numa sessão controlada isso parecia travamento. Abre sessão nova e diz por quê.
    warnings.append(f"Nenhuma conversa anterior do {cli} nesta pasta: abrindo uma sessão nova em vez de retomar.")
    return False, None


def record_session(home: Path, project: dict, gateway, plan) -> dict:
    """Grava a abertura no registro do perfil (§1). Nunca impede a sessão: falha de disco só deixa de registrar."""
    try:
        profile = profiles.profile_of_home(Path(home), profiles.base_of(Path(home)))["id"]
    except (LauncherError, OSError, ValueError):
        profile = profiles.DEFAULT
    fields = dict(profile=profile, project=project.get("id", ""), folder=str(plan.directory), cli=plan.cli,
                  connection=(getattr(gateway, "connection", None) or {}).get("id", ""), model=plan.model,
                  resume=bool(getattr(plan, "resume", False)), session_id=getattr(plan, "session_id", None))
    try:
        return registry.record(Path(home), **fields)
    except (OSError, ValueError):
        return fields
