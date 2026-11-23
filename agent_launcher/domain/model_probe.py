"""Teste opt-in de um modelo com uma chamada real mínima, feita pelo endpoint local da conexão: o pedido passa pelo mesmo
caminho (autenticação da conexão, UsageTap, endpoint.log) de qualquer chamada e entra no uso do mês. O custo em tokens é
estimado antes (`estimate_cost`) para quem chama pedir confirmação; nada aqui envia sem que `probe` seja chamada."""
from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.domain.catalog import ModelInfo, price_label, protocol_for
from agent_launcher.domain.errors import LauncherError

PROBE_TEXT = "ok"
CONSUMES = ("messages", "responses", "chat_completions")
ROUTES = {"messages": "/v1/messages", "responses": "/v1/responses", "chat_completions": "/v1/chat/completions"}
PROTOCOL_LABELS = {"messages": "Anthropic Messages", "responses": "OpenAI Responses",
                   "chat_completions": "Chat Completions"}
# A API Responses recusa max_output_tokens abaixo de 16; os outros protocolos aceitam 1.
OUTPUT_TOKENS = {"messages": 1, "responses": 16, "chat_completions": 1}
USER_AGENT = "agent-launcher-model-test"
RESPONSE_LIMIT = 1024 * 1024
MESSAGE_LIMIT = 500


@dataclass(frozen=True)
class ProbeCost:
    """Custo em tokens do teste de `model` por `protocol`: entrada estimada, saída no máximo e o preço por token que o host
    informou (None = não informou)."""
    model: str
    protocol: str
    input_tokens: int
    output_tokens: int
    input_price: float | None
    output_price: float | None
    price_label: str | None

    @property
    def cost(self) -> float | None:
        """Custo estimado em US$; None quando o host não informou os dois preços (nunca zero no lugar de desconhecido)."""
        if self.input_price is None or self.output_price is None:
            return None
        return self.input_tokens * self.input_price + self.output_tokens * self.output_price


@dataclass(frozen=True)
class ProbeResult:
    """Resultado da chamada real. `error` traz a causa quando `ok` é falso; os tokens são os que o host reportou (None =
    não informou); `status` 0 = sem resposta."""
    ok: bool
    status: int
    seconds: float
    input_tokens: int | None
    output_tokens: int | None
    error: str
    protocol: str


def estimate_input_tokens(text: str = PROBE_TEXT) -> int:
    """Estimativa dos tokens de entrada de `text`: um a cada quatro caracteres, no mínimo um."""
    return max(1, math.ceil(len(text) / 4))


def estimate_cost(record: ModelInfo, protocol: str) -> ProbeCost:
    """Custo do teste de `record` por `protocol`, com os preços do catálogo."""
    return ProbeCost(record.id, protocol, estimate_input_tokens(), OUTPUT_TOKENS[protocol], record.input_cost_per_token,
                     record.output_cost_per_token, price_label(record))


def _tokens(count: int) -> str:
    return f"{count} token" if count == 1 else f"{count} tokens"


def cost_text(cost: ProbeCost) -> str:
    """O custo em tokens, o protocolo e o preço do modelo ("sem preço" quando o host não os informou), para mostrar
    antes da chamada."""
    price = f"US$ {cost.price_label} por 1M (entrada/saída)" if cost.price_label else "sem preço"
    total = "sem preço" if cost.cost is None else f"US$ {cost.cost:.6f}".replace(".", ",")
    return "\n".join([
        f"Modelo: {cost.model}",
        f"Protocolo: {PROTOCOL_LABELS[cost.protocol]}",
        f"Tokens: {_tokens(cost.input_tokens)} de entrada (estimado) + {_tokens(cost.output_tokens)} de saída (no máximo)",
        f"Preço: {price}",
        f"Custo estimado: {total}",
        "A chamada é real e entra no uso do mês."])


def find_model(gateway, model: str) -> ModelInfo:
    """O modelo no catálogo em cache da conexão (sem rede). LauncherError se não estiver."""
    found = gateway.catalog(offline=True).find(model)
    if found is None:
        raise LauncherError(f"{model} não está no catálogo em cache da conexão {gateway.name}. Confira o ID completo com "
                            f"'models --connection {gateway.name}' (use --refresh com a rede para baixar o catálogo).")
    return found


def protocol_of(gateway, record: ModelInfo) -> str:
    """Protocolo para falar com `record` pela conexão: o mesmo critério dos destinos (Claude por Messages, GPT-5+ por
    Responses, o resto por Chat Completions). LauncherError se a conexão não serve nenhum protocolo do modelo."""
    protocol = protocol_for(record, gateway.protocols, gateway.any_model, CONSUMES)
    if protocol is None:
        raise LauncherError(f"A conexão {gateway.name} não serve um protocolo que {record.id} use.")
    return protocol


def request_body(protocol: str, model: str) -> dict:
    """O pedido mínimo de `protocol`: a mensagem de `PROBE_TEXT`, sem streaming, com o menor limite de saída aceito."""
    limit = OUTPUT_TOKENS[protocol]
    if protocol == "responses":
        return {"model": model, "max_output_tokens": limit, "input": PROBE_TEXT}
    return {"model": model, "max_tokens": limit, "messages": [{"role": "user", "content": PROBE_TEXT}]}


def _message(raw: bytes) -> str:
    """A mensagem de erro de um corpo de resposta (formato OpenAI, Anthropic ou do endpoint local); sem JSON, o texto."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:MESSAGE_LIMIT] or "(sem corpo)"
    if isinstance(data, dict):
        error = data.get("error", data.get("message"))
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error, ensure_ascii=False)
        if error:
            return str(error)[:MESSAGE_LIMIT]
    return text[:MESSAGE_LIMIT] or "(sem corpo)"


def _tokens_reported(data: dict) -> tuple[int | None, int | None]:
    """Tokens de entrada (com cache) e de saída que o host reportou; (None, None) quando não informou."""
    from agent_launcher.endpoint.server import usage_numbers
    numbers = usage_numbers(data.get("usage") or (data.get("response") or {}).get("usage"))
    if numbers is None:
        return None, None
    return numbers["input_tokens"] + numbers["cache_read"] + numbers["cache_write"], numbers["output_tokens"]


def probe(home: Path, connection_id: str, model: str, protocol: str, timeout: float = 60) -> ProbeResult:
    """A chamada real: sobe o endpoint local da conexão e posta o pedido mínimo a ele. Falha do host ou do endpoint vira
    um ProbeResult com `ok` falso e a causa; LauncherError só se o endpoint não sobe."""
    from agent_launcher.endpoint import registry
    port = registry.ensure_running(home, connection_id)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{ROUTES[protocol]}", data=json.dumps(request_body(protocol, model)).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()

    def failed(status: int, error: str) -> ProbeResult:
        return ProbeResult(False, status, time.monotonic() - started, None, None, error, protocol)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(RESPONSE_LIMIT)
    except urllib.error.HTTPError as error:
        return failed(error.code, f"HTTP {error.code}: {_message(error.read(RESPONSE_LIMIT))}")
    except (OSError, http.client.HTTPException) as error:
        return failed(0, f"Sem resposta do endpoint local (porta {port}): {error}")
    seconds = time.monotonic() - started
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return ProbeResult(True, status, seconds, None, None, "", protocol)
    if data.get("error"):
        return failed(status, f"HTTP {status} com erro no corpo: {_message(raw)}")
    return ProbeResult(True, status, seconds, *_tokens_reported(data), "", protocol)


def result_text(result: ProbeResult) -> str:
    """O resultado numa linha: OK com latência e tokens do host, ou ERRO com a causa."""
    if not result.ok:
        return f"ERRO: {result.error}"
    seconds = f"{result.seconds:.2f}".replace(".", ",")
    if result.input_tokens is None:
        return f"OK: respondeu em {seconds} s • o host não informou os tokens usados"
    return f"OK: respondeu em {seconds} s • tokens do host: entrada {result.input_tokens}, saída {result.output_tokens}"
