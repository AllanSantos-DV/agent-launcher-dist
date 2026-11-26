"""Busca pelo texto das mensagens das conversas (Claude Code, Codex e Copilot CLI), para `conversations --grep`.

Só leitura: o transcript é aberto em modo binário e lido em fluxo, uma linha por vez; nada é copiado, movido nem gravado.
Conta só o que as pessoas escreveram e o que o modelo respondeu (nada de metadados, resultado de ferramenta, raciocínio ou
contexto de ambiente). Linha inválida, bytes nulos ou arquivo ausente apenas deixam a conversa sem casar.

Tetos, para a busca nunca travar: MAX_SECONDS no total (as conversas ainda não lidas ficam de fora), MAX_FILE_BYTES por
transcript (o resto do arquivo não é lido) e MAX_LINE_BYTES por linha (a linha maior é pulada sem ser carregada). O que
ficou sem ler volta em `notes`."""
from __future__ import annotations

import json
import time
from pathlib import Path

from agent_launcher.domain.conversation_usage import transcript
from agent_launcher.harnesses.claude_code import _user_text
from agent_launcher.harnesses.codex import _codex_user_text

MAX_SECONDS = 30.0
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
DRAIN_BLOCK = 1024 * 1024
MARKERS = {"claude": (b'"user"', b'"assistant"'), "codex": (b'"message"',), "copilot": (b"user.message", b"assistant.message")}


def _parts(content, kind: str) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == kind and isinstance(part.get("text"), str)]


def message_texts(cli: str, entry) -> list[str]:
    """Os textos de mensagem (usuário e assistente) de uma linha do transcript do `cli`; [] nas demais."""
    if not isinstance(entry, dict):
        return []
    kind = entry.get("type")
    if cli == "claude":
        if kind == "user":
            return [found] if (found := _user_text(entry)) else []
        message = entry.get("message")
        return _parts(message.get("content"), "text") if kind == "assistant" and isinstance(message, dict) else []
    if cli == "codex":
        payload = entry.get("payload")
        if kind != "response_item" or not isinstance(payload, dict) or payload.get("type") != "message":
            return []
        if payload.get("role") == "user":
            return [found] if (found := _codex_user_text(entry)) else []
        return _parts(payload.get("content"), "output_text") if payload.get("role") == "assistant" else []
    if cli == "copilot":
        data = entry.get("data")
        if kind in ("user.message", "assistant.message") and isinstance(data, dict) and isinstance(data.get("content"), str):
            return [data["content"]]
    return []


def _drain(stream, deadline: float) -> int:
    """Descarta o resto de uma linha longa demais sem guardá-la; devolve os bytes descartados (-1 se o tempo acabou)."""
    drained = 0
    while time.monotonic() <= deadline:
        block = stream.readline(DRAIN_BLOCK)
        drained += len(block)
        if not block or block.endswith(b"\n"):
            return drained
    return -1


def _scan(path: Path, cli: str, query: str, deadline: float, max_bytes: int) -> str:
    """"found", "none", "cut" (parou no teto de bytes), "late" (acabou o tempo) ou "unreadable"."""
    markers, read = MARKERS[cli], 0
    try:
        with open(path, "rb") as stream:
            while True:
                if time.monotonic() > deadline:
                    return "late"
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    return "none"
                if read >= max_bytes:
                    return "cut"
                read += len(raw)
                if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    drained = _drain(stream, deadline)
                    if drained < 0:
                        return "late"
                    read += drained
                    continue
                if not any(marker in raw for marker in markers):
                    continue
                try:
                    entry = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    continue
                if any(query in found.casefold() for found in message_texts(cli, entry)):
                    return "found"
    except OSError:
        return "unreadable"


def grep_conversations(conversations: list, text: str, seconds: float | None = None,
                       max_bytes: int | None = None) -> tuple[list, list[str]]:
    """(as conversas com `text` em alguma mensagem, na ordem recebida; avisos do que ficou sem ler). Maiúsculas não
    contam, como no `--search`."""
    query = text.strip().casefold()
    deadline = time.monotonic() + (MAX_SECONDS if seconds is None else seconds)
    limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    matched, outcomes = [], {"cut": 0, "late": 0, "unreadable": 0}
    for item in conversations:
        if time.monotonic() > deadline:
            outcomes["late"] += 1
            continue
        path = transcript(item.cli, item.id) if item.cli in MARKERS else None
        result = _scan(path, item.cli, query, deadline, limit) if path else "unreadable"
        if result == "found":
            matched.append(item)
        elif result in outcomes:
            outcomes[result] += 1
    notes = []
    if outcomes["unreadable"]:
        notes.append(f"{outcomes['unreadable']} conversa(s) sem transcript legível ficaram fora da busca no conteúdo.")
    if outcomes["cut"]:
        notes.append(f"{outcomes['cut']} conversa(s) foram lidas só até {limit / 2**20:.4g} MiB: o texto depois disso "
                     "não foi buscado.")
    if outcomes["late"]:
        notes.append(f"A busca no conteúdo parou após {MAX_SECONDS if seconds is None else seconds:g} s: "
                     f"{outcomes['late']} conversa(s) não foram lidas.")
    return matched, notes
