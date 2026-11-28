"""Ollama no próprio computador (porta padrão 11434): os modelos rodam local e o código não sai da máquina.

A conexão declara a API Messages na raiz (Claude Code) e OpenAI Chat Completions em /v1 (Aider e Copilot CLI). O Codex
não é oferecido: a prova com `qwen3:4b` terminou sem editar o arquivo, e o `llama3.2:3b` não emitiu chamada de
ferramenta no Claude Code; só o Claude Code com `qwen3:4b` editou o arquivo, então o texto de ajuda pede modelo com
ferramentas.
"""
from dataclasses import replace

from agent_launcher.connectors import ConnectorError
from agent_launcher.connectors._http import EXTRA_MODELS, bearer, gateway, get_json, NO_KEY
from agent_launcher.domain.catalog import positive_integer

SHOW_TIMEOUT = 5
SHOW_WORKERS = 8
SHOW_BUDGET = 20

NAME = "ollama"
LABEL = "Ollama (local)"
HINT = ("Ollama neste computador, sem chave e sem enviar código para a nuvem. Para o Aider, o Copilot CLI e o Claude Code; "
        "o Claude Code só edita arquivo com modelo que usa ferramentas (por exemplo qwen3:4b).")
EXAMPLE = "http://127.0.0.1:11434"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "placeholder": EXAMPLE,
     "hint": "Raiz do Ollama, sem /v1."},
    EXTRA_MODELS,
)

_targets, _verify, _models = gateway(EXAMPLE, claude="", codex=None, chat="/v1", models_path="/v1/models")


def _root(options: dict) -> dict:
    url = str(options.get("base_url") or "").strip().rstrip("/")
    return {**options, "base_url": url.removesuffix("/v1")}


def targets(options):
    return _targets(_root(options))


def verify(options):
    _verify(_root(options))


def _show(base: str, identifier: str) -> dict | None | bool:
    """`/api/show` do modelo; None quando o host não o descreve (Ollama antigo, modelo só digitado em "Modelos extras":
    HTTP 404) e False quando a consulta falhou (tempo, rede, erro do host). Nos dois o que não veio continua
    desconhecido, nunca vira capacidade; só o False é avisado, porque muda de uma consulta para outra."""
    try:
        return get_json(base + "/api/show", bearer(NO_KEY), SHOW_TIMEOUT, body={"model": identifier})
    except ConnectorError as error:
        return None if getattr(error, "status", None) == 404 else False


def _described(record, shown: dict | None):
    """`record` com o que o `/api/show` informa: `capabilities` (completion, tools, vision, embedding, thinking) e a
    janela máxima do modelo (`model_info["<arquitetura>.context_length"]`). Devolve (registro, janela)."""
    if not shown:
        return record, None
    info = shown.get("model_info") if isinstance(shown.get("model_info"), dict) else {}
    window = positive_integer(info.get(f"{info.get('general.architecture')}.context_length"))
    caps = shown.get("capabilities")
    if not isinstance(caps, list):
        return record, window
    caps = {item for item in caps if isinstance(item, str)}
    kind = {"mode": "other"} if "embedding" in caps and "completion" not in caps else {}
    return replace(record, tools="tools" in caps, vision="vision" in caps, **kind), window


def inspect(options):
    """(registros, janela confirmada, janelas por modelo, modelos sem resposta): a lista do `/v1/models` com as
    capacidades do `/api/show` de cada modelo. A janela máxima do modelo não é a que o Ollama carrega (4096 por padrão,
    medido no 0.32.3), por isso ela fica em `windows` e não em `max_input_tokens`. As consultas dividem um orçamento
    único (`SHOW_BUDGET`): o modelo que não respondeu até lá conta como sem resposta, e a lista nunca espera mais que isso."""
    from concurrent.futures import ThreadPoolExecutor, wait
    root = _root(options)
    records, confirmed = _models(root)
    base = root["base_url"] or EXAMPLE
    pool = ThreadPoolExecutor(SHOW_WORKERS)
    futures = [pool.submit(_show, base, record.id) for record in records]
    wait(futures, timeout=SHOW_BUDGET)
    pool.shutdown(wait=False, cancel_futures=True)
    shown = [future.result() if future.done() and not future.cancelled() else False for future in futures]
    pairs = [_described(record, one) for record, one in zip(records, shown)]
    failed = [record.id for record, one in zip(records, shown) if one is False]
    return ([record for record, _ in pairs], confirmed, {record.id: window for record, window in pairs if window},
            failed)


def unanswered_note(failed: list[str]) -> str:
    """A frase que diz quantos modelos ficaram sem o `/api/show`, o efeito e o que fazer; a mesma na procura e no catálogo."""
    return (f"capacidades: {len(failed)} {'modelo' if len(failed) == 1 else 'modelos'} sem resposta do /api/show "
            f"(tempo ou erro do host): {', '.join(failed[:3])}{'…' if len(failed) > 3 else ''}; as capacidades "
            f"{'dele ficam desconhecidas' if len(failed) == 1 else 'deles ficam desconhecidas'}. Atualize o catálogo (r) com o Ollama estável.")


def models(options):
    """(registros, janela confirmada, avisos) do conector: `inspect` sem as janelas por modelo; o aviso de modelo sem
    resposta chega ao status do catálogo."""
    records, confirmed, _windows, failed = inspect(options)
    return records, confirmed, ((unanswered_note(failed),) if failed else ())
