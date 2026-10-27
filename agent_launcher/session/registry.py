"""Registro das sessões abertas pelo launcher: uma linha JSON por abertura em
<home do perfil>/sessions.jsonl. Serve para saber quais conversas cada perfil abriu (pasta compartilhada entre perfis)
e para o uso por perfil. Só biblioteca padrão: a janela de sessão importa este módulo. Nunca guarda chave nem prompt."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from agent_launcher.domain.files import replace_retrying

FILE = "sessions.jsonl"
KEEP = 2000
FIELDS = ("at", "profile", "project", "folder", "cli", "connection", "model", "resume", "session_id")


def _path(home: Path) -> Path:
    return Path(home) / FILE


def _key(folder) -> str:
    return os.path.normcase(os.path.normpath(str(folder)))


def record(home: Path, *, profile: str, project: str, folder, cli: str, connection: str, model: str,
           resume: bool = False, session_id: str | None = None, at: float | None = None) -> dict:
    """Acrescenta a abertura; mantém as últimas KEEP linhas. Falha de disco não impede a sessão (devolve a entrada)."""
    entry = {"at": at if at is not None else time.time(), "profile": profile, "project": project,
             "folder": str(folder), "cli": cli, "connection": connection, "model": model, "resume": bool(resume),
             "session_id": session_id}
    path = _path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if path.stat().st_size > KEEP * 600:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP:]
            temporary = path.with_suffix(".tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            replace_retrying(temporary, path)
    except OSError:
        pass
    return entry


def entries(home: Path, folder=None, cli: str | None = None) -> list[dict]:
    """Aberturas registradas (mais antigas primeiro), filtradas por pasta e CLI. Linha inválida é ignorada."""
    try:
        lines = _path(home).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    wanted = _key(folder) if folder is not None else None
    result = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("folder"), str):
            continue
        if wanted is not None and _key(entry["folder"]) != wanted:
            continue
        if cli is not None and entry.get("cli") != cli:
            continue
        result.append(entry)
    return result


def opened_since(home: Path, folder, cli: str) -> list[float]:
    """Instantes das aberturas deste perfil na pasta (para atribuir conversas sem id conhecido: Codex, Copilot CLI)."""
    return [float(entry["at"]) for entry in entries(home, folder, cli) if isinstance(entry.get("at"), (int, float))]


def conversation_ids(home: Path, folder, cli: str) -> set[str]:
    """Ids de conversa conhecidos na abertura (Claude recebe --session-id do launcher; retomadas guardam o id)."""
    return {entry["session_id"] for entry in entries(home, folder, cli) if isinstance(entry.get("session_id"), str)}
