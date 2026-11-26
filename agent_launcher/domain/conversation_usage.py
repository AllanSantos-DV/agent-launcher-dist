"""Custo estimado e contexto usado de uma conversa (Claude Code, Codex e Copilot CLI), lidos do transcript dela: o que a lista de
conversas mostra e o que avisa quando a retomada cabe numa janela menor. Sem preço ou sem transcript, desconhecido."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


def conversation_usage_cells(result: ConversationUsage | None, record: ModelInfo | None, verified: bool) -> tuple[str, str]:
    """Contexto (com a parte da janela do modelo escolhido, quando confirmada) e custo estimado; traço = desconhecido."""
    if result is None:
        return "—", "—"
    window = record.max_input_tokens if record and verified else None
    context = "—" if result.context is None else (f"{round(result.context / 1000)}k" if result.context >= 1000 else str(result.context))
    if result.context and window:
        context += f" ({round(result.context * 100 / window)}%)"
    cost = "—" if result.cost is None else f"US$ {result.cost:.2f}".replace(".", ",")
    return context, cost


CONVERSATIONS_HEADER = ("Data", "CLI", "Título", "Mensagens", "Contexto", "Custo (est.)")


def conversation_row(conversation, result: ConversationUsage | None, record: ModelInfo | None = None,
                     verified: bool = False) -> tuple[str, ...]:
    """Os valores de uma conversa nas colunas de CONVERSATIONS_HEADER, como a janela de conversas os mostra."""
    from agent_launcher.harnesses import registry  # import tardio: o domínio não depende dos destinos
    harness = registry.find(conversation.cli)
    return (datetime.fromtimestamp(conversation.updated).strftime("%d/%m/%Y %H:%M"),
            harness.LABEL if harness else conversation.cli,
            conversation.title or "(sem título)",
            "" if conversation.messages is None else str(conversation.messages),
            *conversation_usage_cells(result, record, verified))


def conversations_text(rows: list, header=CONVERSATIONS_HEADER) -> str:
    """A lista de conversas separada por tabulação: colada numa planilha, cada valor cai na sua coluna. `header` tem uma
    coluna por valor de cada linha."""
    return "\n".join("\t".join(row) for row in [list(header), *rows])


def conversations_tsv(conversations: list, usages: dict, record: ModelInfo | None = None, verified: bool = False,
                      branches: bool = False) -> str:
    """O texto de `conversations_text` para `conversations`, com o uso de cada uma em `usages` (por ID). Com `branches` e
    alguma conversa com ramo gravado, a coluna final "Ramo" (traço onde não se sabe); sem nenhum ramo, o texto é o de sempre."""
    rows = [conversation_row(item, usages.get(item.id), record, verified) for item in conversations]
    if branches and any(item.branch for item in conversations):
        rows = [(*row, item.branch or "—") for row, item in zip(rows, conversations)]
        return conversations_text(rows, (*CONVERSATIONS_HEADER, "Ramo"))
    return conversations_text(rows)


def resume_window_warning(home: Path, conversations: list, conversation: str | None, record: ModelInfo | None,
                          verified: bool) -> str | None:
    """Aviso da conversa que será retomada: a escolhida ou, sem escolha, a mais recente da lista."""
    target = next((item for item in conversations if item.id == conversation),
                  conversations[0] if conversations and not conversation else None)
    if target is None:
        return None
    return window_warning(conversation_usage(home, target.cli, target.id).context, record, verified)
