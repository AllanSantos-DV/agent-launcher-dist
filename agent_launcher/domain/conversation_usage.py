"""Custo estimado e contexto usado de uma conversa (Claude Code, Codex e Copilot CLI), lidos do transcript dela: o que a lista de
conversas mostra e o que avisa quando a retomada cabe numa janela menor. Sem preço ou sem transcript, desconhecido."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_launcher.domain import usage
from agent_launcher.domain.catalog import ModelInfo

READERS = {"claude": usage.read_claude, "codex": usage.read_codex, "copilot": usage.read_copilot}
WARN_FRACTION = 0.9


@dataclass(frozen=True)
class ConversationUsage:
    cost: float | None = None      # US$ estimados; None quando falta preço de algum modelo usado ou o transcript
    context: int | None = None     # tokens do último pedido (o que a retomada carrega); None quando desconhecido


def transcript(cli: str, conversation_id: str) -> Path | None:
    """Arquivo da conversa no CLI; None para o CLI sem leitor ou conversa que sumiu."""
    if cli == "claude":
        found = list(usage.claude_projects().glob(f"*/{conversation_id}.jsonl"))
    elif cli == "codex":
        found = list(usage.codex_sessions().glob(f"*/*/*/rollout-*-{conversation_id}.jsonl"))
    elif cli == "copilot":
        events = usage.copilot_sessions() / conversation_id / "events.jsonl"
        found = [events] if events.is_file() else []
    else:
        return None
    return found[0] if found else None


def _cost(result: dict, tables: dict) -> float | None:
    total = 0.0
    for models in (result.get("months") or {}).values():
        for model, values in models.items():
            value = usage.cost(usage.find_price(tables, model), values)
            if value is None:
                return None
            total += value
    return total


def conversation_usage(home: Path, cli: str, conversation_id: str, tables: dict | None = None,
                       cache: usage.FileCache | None = None) -> ConversationUsage:
    """Custo e contexto de uma conversa. tables e cache evitam reler o catálogo e o transcript em cada linha da lista."""
    path = transcript(cli, conversation_id)
    if path is None:
        return ConversationUsage()
    tables = usage.price_table(home) if tables is None else tables
    result = (cache or usage.FileCache(home)).get(path, READERS[cli])
    return ConversationUsage(_cost(result, tables) if result.get("months") else None, result.get("context") or None)


def window_warning(context: int | None, record: ModelInfo | None, verified: bool) -> str | None:
    """Aviso quando a conversa que se retoma já ocupa quase toda a janela do modelo escolhido (ou mais que ela)."""
    window = record.max_input_tokens if record and verified else None
    if not context or not window or context < window * WARN_FRACTION:
        return None
    share = round(context * 100 / window)
    return (f"A conversa ocupa {share}% da janela de {record.id} ({context:,} de {window:,} tokens): a retomada pode "
            "falhar ou ser compactada. Escolha um modelo de janela maior.").replace(",", ".")


def resume_window_warning(home: Path, conversations: list, conversation: str | None, record: ModelInfo | None,
                          verified: bool) -> str | None:
    """Aviso da conversa que será retomada: a escolhida ou, sem escolha, a mais recente da lista."""
    target = next((item for item in conversations if item.id == conversation),
                  conversations[0] if conversations and not conversation else None)
    if target is None:
        return None
    return window_warning(conversation_usage(home, target.cli, target.id).context, record, verified)
