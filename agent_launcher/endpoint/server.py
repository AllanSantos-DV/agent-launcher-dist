"""Endpoint local de UMA conexão: escuta só em 127.0.0.1 e repassa ao host da conexão.

- Rotas: /v1/messages, /v1/responses, /v1/chat/completions (e /v1/messages/count_tokens), cada uma só se a conexão
  serve o protocolo; /v1/models devolve o catálogo do launcher (só modelos com ferramentas, com limites, capacidades,
  preço e fonte; `?capability=vision|audio|video|tools` exige as capacidades confirmadas); /usage devolve o uso do mês
  (`?month=AAAA-MM`, `?all=1` soma os perfis); /health; /shutdown.
- Autenticação: a da conexão (chave da DPAPI como Bearer ou x-api-key; nada quando o host já injeta). Qualquer
  credencial que venha do destino (Authorization, x-api-key, api-key) é descartada.
- Streaming (SSE) repassado sem buffer; sem seguir redirecionamento; proxy do sistema só para host público.
- Lê a conexão a cada requisição: trocar host ou chave no launcher vale na próxima chamada, sem mexer no destino.
- Failover: a conexão com `fallback_connection` que responde cota esgotada (a detecção dos conectores: 429 com código de
  cota, 402 de pagamento) antes de entregar qualquer byte da resposta tem o MESMO pedido refeito na reserva, uma vez só;
  a conexão fica marcada com `quota_exhausted` (e a reserva também, se ela responde sem cota). Enquanto a marca da
  primeira vale (QUOTA_RETRY_SECONDS) e a reserva não está marcada, o pedido vai direto à reserva; passado o prazo a
  primeira é tentada de novo, e a resposta 2xx de uma conexão marcada tira a marca. Sem reserva (ou reserva inválida), o
  erro é repassado como é.
- Log em ~/.agent-launcher/endpoint.log: método, rota, modelo, status e duração. Nunca corpo nem cabeçalho.
- Uso: o `usage` da resposta (JSON depois de enviado; SSE observado enquanto passa, sem
  segurar nenhum pedaço) vira uma linha em <raiz>/usage.jsonl, só com números. Falha no parse nunca afeta a resposta.
- Encerra sozinho depois de IDLE_SECONDS sem requisição; o supervisor desliga o encerramento por ociosidade (o `serve`
  supervisionado leva `--no-idle-exit`).
- `resolve` imprime a conexão já resolvida (JSON, com a reserva quando há) para o endpoint nativo, que nunca executa
  conector; `usage` imprime o relatório de uso (JSON) para o nativo, que nunca lê as conversas dos CLIs; `quota` diz se
  uma resposta de erro é cota esgotada e, sendo, marca a conexão.
- `supervise` mantém o `serve` da conexão de pé (ver supervisor.py); é o que o início no logon e o `ensure_running` lançam.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_launcher.connectors._http import (ERROR_BODY_LIMIT, is_local_host, openai_quota, payment_required,
                                             quota_suffix)
from agent_launcher.domain.catalog import PRICES, ModelInfo
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import append_capped, log_line

IDLE_SECONDS = 4 * 60 * 60
LOG_MAX_BYTES = 1_000_000  # endpoint.log: passando disso, fica a metade mais recente
BODY_LIMIT = 64 * 1024 * 1024
ROUTES = {"/v1/messages": "messages", "/v1/messages/count_tokens": "messages", "/v1/responses": "responses",
          "/v1/chat/completions": "chat_completions"}
CLIENT_CREDENTIALS = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
HOP_BY_HOP = {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
              "content-length", "accept-encoding"}
SAMPLING_FIX = {"messages", "chat_completions"}
RESPONSE_SKIP = {"transfer-encoding", "connection", "keep-alive", "content-length", "content-encoding"}
USAGE_FILE = "usage.jsonl"
QUOTA_STATUSES = (429, 402)
# Quanto vale a marca `quota_exhausted` no roteamento: com reserva, a primeira só é tentada de novo passado esse prazo.
QUOTA_RETRY_SECONDS = 600
CAPABILITIES = {"vision": "vision", "audio": "audio_input", "video": "video_input", "tools": "tools"}
JSON_LIMIT = 8 * 1024 * 1024   # corpo JSON maior que isso não é guardado para ler o usage (a resposta passa igual)
LINE_LIMIT = 1024 * 1024       # linha SSE maior que isso é descartada pelo observador


INT64_MAX = 2 ** 63 - 1


def strict_loads(data: bytes | str):
    """`json.loads` só para JSON de verdade: NaN, Infinity e -Infinity (que o Python aceita) são ValueError, como no
    endpoint nativo, para um corpo que não é JSON passar intacto nas duas formas."""
    def refuse(name):
        raise ValueError(f"constante fora do JSON: {name}")
    return json.loads(data, parse_constant=refuse)


def _count(value) -> int:
    """Contagem de tokens: inteiro positivo até 2^63 - 1; fração, texto, booleano e o que passa disso valem 0."""
    return value if type(value) is int and 0 < value <= INT64_MAX else 0


def usage_numbers(usage) -> dict | None:
    """Normaliza o `usage` dos três protocolos para {input_tokens, output_tokens, cache_read, cache_write}, com
    input_tokens sem o cache (Chat Completions/Responses contam o cache dentro de prompt/input_tokens)."""
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" in usage or "completion_tokens" in usage:  # Chat Completions
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        cached = _count(details.get("cached_tokens"))
        return {"input_tokens": max(0, _count(usage.get("prompt_tokens")) - cached),
                "output_tokens": _count(usage.get("completion_tokens")), "cache_read": cached, "cache_write": 0}
    if "input_tokens_details" in usage:  # Responses
        details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        cached = _count(details.get("cached_tokens"))
        return {"input_tokens": max(0, _count(usage.get("input_tokens")) - cached),
                "output_tokens": _count(usage.get("output_tokens")), "cache_read": cached, "cache_write": 0}
    if "input_tokens" in usage or "output_tokens" in usage:  # Messages (ou Responses sem detalhes)
        return {"input_tokens": _count(usage.get("input_tokens")), "output_tokens": _count(usage.get("output_tokens")),
                "cache_read": _count(usage.get("cache_read_input_tokens")),
                "cache_write": _count(usage.get("cache_creation_input_tokens"))}
    return None


class UsageTap:
    """Observa os pedaços da resposta DEPOIS de repassados (nunca segura nem altera nada) e acha o usage.
    JSON: guarda uma cópia (até JSON_LIMIT) e lê no fim. SSE: processa linha a linha e só olha as `data:` com usage
    (Chat Completions: chunk com usage; Responses: response.completed; Messages: message_start + message_delta)."""

    def __init__(self, content_type: str):
        content_type = (content_type or "").lower()
        self.kind = "sse" if "event-stream" in content_type else "json" if "json" in content_type else None
        self.body, self.partial, self.usage, self.model, self.broken = bytearray(), b"", None, "", False

    def feed(self, chunk: bytes) -> None:
        if self.broken or not self.kind:
            return
        try:
            if self.kind == "json":
                if len(self.body) + len(chunk) > JSON_LIMIT:
                    self.broken, self.body = True, bytearray()
                else:
                    self.body += chunk
                return
            data = self.partial + chunk
            lines = data.split(b"\n")
            self.partial = lines.pop()
            if len(self.partial) > LINE_LIMIT:
                self.partial = b""
            for line in lines:
                if line.startswith(b"data:") and b"usage" in line:
                    self.event(line[5:].strip())
        except Exception:  # observador nunca derruba a resposta
            self.broken = True

    def event(self, raw: bytes) -> None:
        try:
            data = strict_loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "message_start" and isinstance(data.get("message"), dict):
            self.merge(data["message"].get("usage"), data["message"].get("model"))
        elif kind == "message_delta":
            self.merge(data.get("usage"))
        elif isinstance(kind, str) and kind.startswith("response.") and isinstance(data.get("response"), dict):
            if data["response"].get("usage"):
                self.merge(data["response"]["usage"], data["response"].get("model"), replace=True)
        elif data.get("usage"):
            self.merge(data["usage"], data.get("model"), replace=True)

    def merge(self, usage, model=None, replace: bool = False) -> None:
        numbers = usage_numbers(usage)
        if numbers is None:
            return
        if isinstance(model, str) and model:
            self.model = model[:200]
        if replace or self.usage is None:
            self.usage = numbers
        else:  # Messages: message_delta traz o acumulado da saída (e às vezes repete a entrada)
            self.usage = {key: max(self.usage[key], numbers[key]) for key in numbers}

    def result(self) -> dict | None:
        try:
            if self.kind == "json" and not self.broken and self.body:
                data = strict_loads(bytes(self.body))
                if isinstance(data, dict):
                    self.merge(data.get("usage") or (data.get("response") or {}).get("usage"), data.get("model"))
            elif self.kind == "sse" and self.partial.startswith(b"data:") and b"usage" in self.partial:
                self.event(self.partial[5:].strip())
        except Exception:
            return None
        return self.usage


def destination_of(user_agent: str) -> str:
    """Quem chamou o endpoint, pelo User-Agent: Claude Code, Codex, VS Code (Copilot Chat) ou Copilot app; senão "endpoint".
    Claude Code e Codex vêm primeiro: o Codex acrescenta o terminal ao User-Agent, e dentro do VS Code ele diz "vscode"."""
    agent = (user_agent or "").lower()
    if "claude-cli" in agent or "claude-code" in agent:
        return "claude"
    if "codex" in agent:
        return "codex"
    if "vscode" in agent or "copilotchat" in agent or "copilot-chat" in agent:
        return "vscode"
    if "copilot" in agent or "electron" in agent:
        return "copilot_app"
    return "endpoint"


def model_entry(record: ModelInfo, owner: str) -> dict:
    """Item de /v1/models: limites, capacidades e preço por token (US$) como o catálogo os conhece; null = o host não
    informou. `source` é o nome que o conector declarou e só vem quando o modelo tem fonte."""
    entry = {"id": record.id, "object": "model", "owned_by": owner, "max_input_tokens": record.max_input_tokens,
             "max_output_tokens": record.max_output_tokens,
             "capabilities": {"vision": record.vision, "audio_input": record.audio_input,
                              "video_input": record.video_input, "tools": record.tools},
             "pricing": {name: getattr(record, name) for name in PRICES}}
    if record.source:
        entry["source"] = record.source
    return entry


class Upstream:
    """A conexão lida do cadastro, com cache curto: troca de host/chave vale em poucos segundos."""

    def __init__(self, home: Path, connection_id: str, ttl: float = 5):
        self.home, self.connection_id, self.ttl = Path(home), connection_id, ttl
        self._cached, self._reserve, self._at, self._lock = None, None, 0.0, threading.Lock()
        from agent_launcher.domain import profiles
        self.profile_id = profiles.profile_of_home(self.home)["id"]

    def get(self):
        with self._lock:
            if self._cached is None or time.monotonic() - self._at > self.ttl:
                from agent_launcher.domain.gateway import Gateway
                from agent_launcher.domain.store import Store
                store = Store(self.home)
                connection = store.find_connection(self.connection_id)
                self._cached = Gateway(store, connection)
                reserve = store.fallback_of(connection)
                try:
                    self._reserve = Gateway(store, reserve) if reserve else None
                except LauncherError:
                    self._reserve = None  # reserva com conector indisponível: a conexão atende sozinha, como sem reserva
                self._at = time.monotonic()
            return self._cached

    def fallback(self):
        """O Gateway da conexão reserva da conexão atendida, ou None (sem reserva, ou reserva inválida)."""
        self.get()
        with self._lock:
            return self._reserve

    def invalidate(self) -> None:
        """Descarta o cache: o próximo `get` lê o cadastro de novo (depois de gravar ou limpar uma marca de cota)."""
        with self._lock:
            self._cached = None


def model_entries(gateway, online: bool) -> list[dict]:
    """Itens de /v1/models: só modelos de conversa com ferramentas. Catálogo vazio no cache busca no host (`online`)."""
    catalog = gateway.catalog(offline=True)
    if not catalog.records and online:
        catalog = gateway.catalog()
    return [model_entry(record, record.provider or gateway.name)
            for record in catalog.records if record.chat and record.tools is not False]


def capabilities_wanted(values: list[str]) -> list[str]:
    """Campos de `capabilities` exigidos pelo `?capability=` (valor repetido ou separado por vírgula: todos).
    ValueError para valor desconhecido."""
    fields = []
    for value in values:
        for name in value.split(","):
            if name not in CAPABILITIES:
                raise ValueError(f"Capacidade desconhecida: {name!r}. Use vision, audio, video ou tools.")
            fields.append(CAPABILITIES[name])
    return fields


def with_capabilities(entries: list[dict], fields: list[str]) -> list[dict]:
    """Itens que têm todas as capacidades em `fields` confirmadas; `null` (o host não informou) não conta."""
    return [entry for entry in entries if all(entry["capabilities"][field] is True for field in fields)]


def single_value(query: dict[str, list[str]], name: str) -> str | None:
    """Valor de um parâmetro da URL que só aceita um; ValueError se veio repetido."""
    values = query.get(name, [])
    if len(values) > 1:
        raise ValueError(f"Parâmetro repetido: {name}.")
    return values[0] if values else None


def usage_request(query: dict[str, list[str]]) -> tuple[str | None, bool]:
    """(mês, soma dos perfis) do `GET /usage`. ValueError para parâmetro repetido ou `all` fora de 1/true/0/false."""
    month, every = single_value(query, "month"), single_value(query, "all")
    if every not in (None, "1", "true", "0", "false"):
        raise ValueError(f"Valor inválido para all: {every!r}. Use 1, true, 0 ou false.")
    return month, every in ("1", "true")


def usage_of(home: Path, month: str | None, every: bool) -> dict:
    """Relatório de uso do mês do perfil dono de `home`, ou de todos os perfis. ValueError para mês fora de AAAA-MM."""
    from agent_launcher.domain import usage
    return (usage.usage_report_all if every else usage.usage_report)(home, month)


def usage_file_of(home: Path) -> Path | None:
    """<raiz dos perfis>/usage.jsonl, ou None se a raiz não se resolve."""
    try:
        from agent_launcher.domain import profiles
        return profiles.base_of(home) / USAGE_FILE
    except Exception:
        return None


def target_url(gateway, protocol: str, path: str, query: str) -> str:
    """URL no host para a rota pedida. As bases da conexão seguem o padrão dos clientes: Messages sem /v1 (o cliente
    acrescenta /v1/messages), Responses e Chat Completions terminando em /v1."""
    base = gateway.protocols[protocol].rstrip("/")
    if protocol == "messages":
        url = base + path if not base.endswith("/v1") else base + path[len("/v1"):]
    else:
        url = base + path[len("/v1"):]
    return url + (f"?{query}" if query else "")


def drop_top_p(protocol: str, body: bytes) -> bytes:
    """Claude (Haiku 4.5 no Vertex, por exemplo) recusa `temperature` junto com `top_p`, e o Copilot app manda os dois:
    tira `top_p` (o padrão é 1, a amostragem prática não muda). Outros modelos e corpo que não é JSON passam intactos."""
    if protocol not in SAMPLING_FIX or not body:
        return body
    try:
        data = strict_loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or "temperature" not in data or "top_p" not in data \
            or "claude" not in str(data.get("model") or "").lower():
        return body
    del data["top_p"]
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def ask_stream_usage(protocol: str, body: bytes) -> bytes:
    """Chat Completions com `stream: true` só manda o `usage` no fim do streaming quando o cliente pede
    `stream_options.include_usage` (o VS Code não pede): acrescenta o pedido para o UsageTap ver o uso. O chunk extra
    tem `choices: []`, previsto pela especificação. Cliente que já mandou `stream_options` (qualquer valor), corpo sem
    streaming, outro protocolo ou corpo que não é JSON passam intactos."""
    if protocol != "chat_completions" or not body:
        return body
    try:
        data = strict_loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or data.get("stream") is not True or "stream_options" in data:
        return body
    data["stream_options"] = {"include_usage": True}
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def quota_detail(status: int, body: bytes) -> str | None:
    """O texto do aviso de cota esgotada se a resposta de erro é o que os conectores classificam assim
    (`_http.openai_quota` e `_http.payment_required`); senão None. Corpo acima de ERROR_BODY_LIMIT não é lido (os
    conectores também leem só esse tanto); o que não é um objeto JSON vale como `{}`."""
    if len(body) > ERROR_BODY_LIMIT:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if not (openai_quota(status, payload) or payment_required(status, payload)):
        return None
    return f"Cota ou crédito esgotado (HTTP {status}) no uso pelo endpoint local" + quota_suffix(payload)


def quota_mark_at(connection: dict) -> float | None:
    """A hora (epoch em segundos) da marca `quota_exhausted` da conexão, ou None sem marca ou com marca sem hora."""
    status = connection.get("status") or {}
    at = status.get("at")
    if status.get("state") == "quota_exhausted" and isinstance(at, (int, float)) and not isinstance(at, bool):
        return float(at)
    return None


def quota_active(connection: dict, now: float | None = None) -> bool:
    """A marca de cota esgotada da conexão ainda vale (gravada há menos de QUOTA_RETRY_SECONDS). Marca sem hora ou com
    hora no futuro não vale: a conexão é tentada como sempre."""
    at = quota_mark_at(connection)
    return at is not None and 0 <= (time.time() if now is None else now) - at < QUOTA_RETRY_SECONDS


def mark_quota_exhausted(home: Path, gateway, message: str) -> None:
    """Marca a conexão com o estado `quota_exhausted` (a marca que `launcher connections` e o painel já mostram), sem
    valor de campo secreto na mensagem. Lê o config.json de novo para não regravar uma cópia velha; marca ainda válida
    não é regravada, e a vencida (ou sem hora) é renovada: recomeça o prazo."""
    from agent_launcher.domain.store import Store, secret_values
    store = Store(home)
    connection = store.find_connection(gateway.connection["id"])
    if not quota_active(connection):
        store.mark_quota_exhausted(connection["id"], message, secret_values(gateway.module, gateway.options))


def clear_quota_mark(home: Path, gateway) -> bool:
    """Tira a marca `quota_exhausted` da conexão (a cota voltou). Lê o config.json de novo; True se havia marca."""
    from agent_launcher.domain.store import Store
    store = Store(home)
    connection = store.find_connection(gateway.connection["id"])
    if (connection.get("status") or {}).get("state") != "quota_exhausted":
        return False
    store.clear_connection_status(connection["id"])
    return True


def auth_headers(gateway, protocol: str) -> dict:
    key = gateway.key
    if not key:
        return {}
    if protocol == "messages" and gateway.messages_auth == "api_key":
        return {"x-api-key": key}
    return {"Authorization": f"Bearer {key}"}


def connect(url: str, timeout: float):
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    proxy = None if is_local_host(host) else urllib.request.getproxies().get(parsed.scheme)
    if proxy and not urllib.request.proxy_bypass(host):
        proxied = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(proxied.hostname, proxied.port or 8080, timeout=timeout,
                                                     context=ssl.create_default_context())
            connection.set_tunnel(host, port)
        else:
            connection = http.client.HTTPConnection(proxied.hostname, proxied.port or 8080, timeout=timeout)
            return connection, url
        return connection, urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, ""))
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    return connection, urllib.parse.urlunparse(("", "", parsed.path or "/", "", parsed.query, ""))


class ClientGone(Exception):
    """O cliente do endpoint fechou a conexão enquanto a resposta era entregue."""


@contextlib.contextmanager
def client_side():
    """Escritas ao cliente: a queda da conexão vira ClientGone, para não se confundir com a do host (também
    ConnectionError, como o RemoteDisconnected de um host que fecha sem responder)."""
    try:
        yield
    except ConnectionError as error:
        raise ClientGone from error


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "EndpointServer"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.timeout):
            pass  # o destino fechou a conexão (cancelou a resposta): normal, não é erro do endpoint

    def reply_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: int, message: str, kind: str = "agent_launcher_error") -> None:
        self.reply_json(status, {"error": {"type": kind, "message": message}})

    def do_GET(self):
        self.server.touch()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path == "/health":
            gateway = self.server.upstream.get()
            return self.reply_json(200, {"agent_launcher_endpoint": True, "connection": gateway.name,
                                         "connection_id": self.server.upstream.connection_id,
                                         "profile": self.server.upstream.profile_id,
                                         "protocols": sorted(gateway.protocols), "requests": self.server.requests,
                                         "started": self.server.started})
        if path in ("/v1/models", "/models"):
            return self.models(query)
        if path == "/usage":
            return self.usage(query)
        self.error(404, f"Rota não servida pelo endpoint local do Agent Launcher: {path}")

    def models(self, query: dict[str, list[str]]):
        try:
            fields = capabilities_wanted(query.get("capability", []))
        except ValueError as error:
            return self.error(400, str(error))
        try:
            data = model_entries(self.server.upstream.get(), online=True)
        except LauncherError as error:
            return self.error(502, str(error))
        self.reply_json(200, {"object": "list", "data": with_capabilities(data, fields)})

    def usage(self, query: dict[str, list[str]]):
        try:
            report = usage_of(self.server.upstream.home, *usage_request(query))
        except ValueError as error:
            return self.error(400, str(error))
        except Exception as error:  # o cliente recebe a causa; o relatório lê arquivos de outros programas
            return self.error(500, f"{type(error).__name__}: {error}")
        self.reply_json(200, report)

    def do_POST(self):
        self.server.touch()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/shutdown":
            self.reply_json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if not path.startswith("/v1/"):
            path = "/v1" + path  # clientes que usam a base sem /v1 (ex.: tipo anthropic)
        protocol = ROUTES.get(path)
        if protocol is None:
            return self.error(404, f"Rota não servida pelo endpoint local do Agent Launcher: {parsed.path}")
        length = int(self.headers.get("Content-Length") or 0)
        if length > BODY_LIMIT:
            return self.error(413, "Requisição acima do limite do endpoint local.")
        body = self.rfile.read(length) if length else b""
        self.forward(protocol, path, parsed.query, body)

    def forward(self, protocol: str, path: str, query: str, body: bytes) -> None:
        started = time.monotonic()
        model = ""
        try:
            declared = strict_loads(body or b"{}").get("model")
            model = declared[:200] if isinstance(declared, str) else ""
        except (ValueError, AttributeError):
            pass
        try:
            gateway = self.server.upstream.get()
        except LauncherError as error:
            return self.error(502, str(error))
        if protocol not in gateway.protocols:
            return self.error(400, f"A conexão {gateway.name} não serve {protocol}. Escolha outro modelo ou conexão.")
        body = ask_stream_usage(protocol, drop_top_p(protocol, body))
        connection = None
        status = 0
        tap = None
        served_id = None
        try:
            served, head = gateway, b""
            reserve = self.server.upstream.fallback()
            if reserve is not None and protocol not in reserve.protocols:
                reserve = None
            hop = None
            if reserve is not None and quota_active(gateway.connection) and not quota_active(reserve.connection):
                hop = self.go_direct(gateway, reserve, protocol, path, query, body)
            if hop is not None:
                connection, response, head = hop
                served, served_id = reserve, reserve.connection["id"]
                status = response.status
            else:
                connection, response = self.open_upstream(gateway, protocol, path, query, body)
                status = response.status
                if reserve is not None and status in QUOTA_STATUSES:
                    head = response.read(ERROR_BODY_LIMIT + 1)  # erro de cota vem antes de qualquer byte entregue ao cliente
                    hop = self.fail_over(gateway, reserve, protocol, path, query, body, status, head)
                    if hop is not None:
                        connection.close()
                        connection, response, head = hop
                        status, served, served_id = response.status, reserve, reserve.connection["id"]
            if 200 <= status < 300:
                self.server.clear_quota(served)
            if 300 <= status < 400:
                response.read()
                return self.error(502, "O host tentou redirecionar; o endpoint local não segue redirecionamentos "
                                       "(para não vazar a chave). Use o endereço final na conexão.")
            self.send_response(status)
            for key, value in response.getheaders():
                if key.lower() not in RESPONSE_SKIP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            with client_side():
                self.end_headers()
            if 200 <= status < 300 and not path.endswith("/count_tokens"):
                tap = UsageTap(response.getheader("Content-Type") or "")
            chunk = head or response.read1(65536)
            while chunk:
                with client_side():
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                if tap is not None:
                    tap.feed(chunk)  # depois de enviado: o destino nunca espera pelo observador
                chunk = response.read1(65536)
            with client_side():
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except ClientGone:
            status = 499
        except (OSError, http.client.HTTPException) as error:
            if not status:
                status = 502
                self.error(502, f"Sem resposta do host da conexão {gateway.name}: {error}")
        finally:
            if connection is not None:
                connection.close()
            self.server.record(protocol, path, model, status, time.monotonic() - started)
            if tap is not None:
                usage = tap.result()  # antes de ler tap.model: o modelo de uma resposta JSON só aparece aqui
                self.server.record_usage(protocol, model or tap.model, usage,
                                         destination_of(self.headers.get("User-Agent") or ""), served_id)

    def open_upstream(self, gateway, protocol: str, path: str, query: str, body: bytes):
        """POST ao host da conexão com a autenticação dela (a do cliente é descartada). Devolve (conexão HTTP, resposta);
        quem chama lê a resposta e fecha a conexão."""
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in HOP_BY_HOP | CLIENT_CREDENTIALS}
        headers.update(auth_headers(gateway, protocol))
        headers["Content-Length"] = str(len(body))
        connection, target = connect(target_url(gateway, protocol, path, query), timeout=900)
        try:
            connection.request("POST", target, body=body, headers=headers)
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    def settle(self, gateway, response) -> bytes:
        """Resposta de `gateway` que pode ser erro de cota (429/402): lê o corpo do erro antes de entregar qualquer byte e,
        sendo cota, marca a conexão. Devolve o corpo lido (vazio para qualquer outra resposta, que segue intacta)."""
        if response.status not in QUOTA_STATUSES:
            return b""
        head = response.read(ERROR_BODY_LIMIT + 1)
        message = quota_detail(response.status, head)
        if message is not None:
            self.server.mark_quota(gateway, message)
        return head

    def go_direct(self, gateway, reserve, protocol: str, path: str, query: str, body: bytes):
        """A conexão atendida está marcada sem cota: o pedido vai direto à reserva, sem tocar nela. Devolve (conexão HTTP,
        resposta, corpo de erro já lido) da reserva, ou None quando ela não respondeu ou redirecionou: quem chama tenta
        então a conexão atendida."""
        try:
            connection, response = self.open_upstream(reserve, protocol, path, query, body)
        except (OSError, http.client.HTTPException) as error:
            self.server.note(f"reserva {reserve.name} não respondeu ({error}); tentando {gateway.name}")
            return None
        if 300 <= response.status < 400:
            connection.close()
            self.server.note(f"reserva {reserve.name} tentou redirecionar; tentando {gateway.name}")
            return None
        self.server.note(f"failover {gateway.name} -> {reserve.name} motivo=cota marcada")
        return connection, response, self.settle(reserve, response)

    def fail_over(self, gateway, reserve, protocol: str, path: str, query: str, body: bytes, status: int, head: bytes):
        """A conexão atendida respondeu `status` com o corpo de erro `head`: sendo cota esgotada (a detecção dos
        conectores), marca a conexão e refaz o MESMO pedido, com o corpo já lido, na reserva (que, respondendo também sem
        cota, fica marcada). Devolve (conexão HTTP, resposta, corpo de erro da reserva já lido) ou None quando não é cota
        ou a reserva não respondeu: quem chama repassa então o erro original."""
        message = quota_detail(status, head)
        if message is None:
            return None
        self.server.mark_quota(gateway, message)
        try:
            connection, response = self.open_upstream(reserve, protocol, path, query, body)
        except (OSError, http.client.HTTPException) as error:
            self.server.note(f"failover para {reserve.name} não feito: {error}")
            return None
        if 300 <= response.status < 400:
            connection.close()
            self.server.note(f"failover para {reserve.name} não feito: o host tentou redirecionar")
            return None
        self.server.note(f"failover {gateway.name} -> {reserve.name} motivo={status}")
        return connection, response, self.settle(reserve, response)


class EndpointServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, upstream: Upstream, log: Path | None, idle: float | None):
        super().__init__(("127.0.0.1", port), Handler)
        self.upstream, self.log, self.idle = upstream, log, idle
        self.requests, self.started, self.last = 0, time.time(), time.monotonic()
        self._log_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self.usage_file = usage_file_of(upstream.home)

    def touch(self):
        self.last = time.monotonic()

    def handle_error(self, request, client_address):
        """Exceção de um pedido (o stderr deste processo oculto vai para o vazio): só o tipo e a mensagem no endpoint.log."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return  # cliente que desistiu (cancelou o stream): rotina, não defeito
        self.note(f"erro no pedido: {type(error).__name__}: {error}")

    def _append(self, line: str) -> None:
        """Acrescenta ao endpoint.log (sob _log_lock); passando de LOG_MAX_BYTES, fica só a metade mais recente."""
        try:
            append_capped(self.log, line, LOG_MAX_BYTES)
        except OSError:
            pass

    def note(self, message: str) -> None:
        if not self.log:
            return
        with self._log_lock:
            self._append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    def record(self, protocol: str, path: str, model: str, status: int, seconds: float) -> None:
        self.requests += 1
        if not self.log:
            return
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.upstream.connection_id[:8]} POST {path} {protocol} "
                f"model={model!r} status={status} {seconds:.1f}s\n")
        with self._log_lock:
            self._append(line)

    def mark_quota(self, gateway, message: str) -> None:
        """Grava a marca de cota esgotada na conexão (ver `mark_quota_exhausted`). Falha vai ao log; nunca levanta."""
        try:
            mark_quota_exhausted(self.upstream.home, gateway, message)
            self.upstream.invalidate()
        except Exception as error:
            self.note(f"marca de cota não gravada: {type(error).__name__}: {error}")

    def clear_quota(self, gateway) -> None:
        """Resposta 2xx de uma conexão que estava marcada sem cota: a cota voltou, a marca sai. Nunca levanta."""
        if (gateway.connection.get("status") or {}).get("state") != "quota_exhausted":
            return
        try:
            if clear_quota_mark(self.upstream.home, gateway):
                self.upstream.invalidate()
        except Exception as error:
            self.note(f"marca de cota não limpa: {type(error).__name__}: {error}")

    def record_usage(self, protocol: str, model: str, usage: dict | None, destination: str,
                     connection_id: str | None = None) -> None:
        """Uma linha por resposta com usage em <raiz>/usage.jsonl (sem corpo, sem prompt). `connection_id` é da conexão que
        respondeu quando não é a atendida (failover). Nunca levanta."""
        if not usage or not self.usage_file or not any(usage.values()):
            return
        try:
            line = json.dumps({"at": round(time.time(), 3), "profile": self.upstream.profile_id,
                               "connection": connection_id or self.upstream.connection_id, "model": model,
                               "protocol": protocol, "destination": destination, **usage}, ensure_ascii=False) + "\n"
            with self._usage_lock:
                with self.usage_file.open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except Exception as error:
            self.note(f"uso não gravado: {type(error).__name__}: {error}")

    def service_actions(self):
        if self.idle and time.monotonic() - self.last > self.idle:
            threading.Thread(target=self.shutdown, daemon=True).start()


def serve(home: Path, connection_id: str, port: int | None = None, idle: float | None = IDLE_SECONDS) -> int:
    from agent_launcher.endpoint import registry
    port = port or registry.port_for(home, connection_id)
    upstream = Upstream(home, connection_id)
    upstream.get()  # conexão inexistente falha aqui, antes de ocupar a porta
    try:
        server = EndpointServer(port, upstream, Path(home) / "endpoint.log", idle)
    except OSError as error:
        raise LauncherError(f"Não foi possível escutar em 127.0.0.1:{port}: {error}") from error
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close()
    return 0


def watched_files(home: Path, gateway) -> list[str]:
    """Arquivos cuja mudança de mtime invalida a resolução: o cadastro (config.json) e o cofre (secrets.json) da pasta
    de dados, o cache de modelos da conexão, e a pasta de conectores do usuário (a pasta, para pegar arquivo novo ou
    removido, e cada .py dentro). endpoints.json e profiles.json ficam de fora: a porta já está ocupada e o perfil
    não muda durante a vida do processo, então mexer neles não muda nada do que o endpoint serve."""
    from agent_launcher.domain import profiles
    user_connectors = profiles.base_of(home) / "connectors"
    files = [Path(home) / "config.json", Path(home) / "secrets.json", gateway.cache_path, user_connectors]
    if user_connectors.is_dir():
        files += sorted(user_connectors.glob("*.py"))
    return [str(path) for path in files]


def resolve(home: Path, connection_id: str, online: bool = False) -> dict:
    """A conexão resolvida em dados puros (versão 1 do contrato): é tudo o que o endpoint nativo precisa saber sem
    executar conector. LauncherError para cadastro inválido, host fora do ar (online) ou porta indisponível."""
    from agent_launcher.endpoint import registry
    home = Path(home)
    upstream = Upstream(home, connection_id)
    gateway = upstream.get()
    identifier = gateway.connection["id"]
    usage_file = usage_file_of(home)
    resolved = {"version": 1, "connection": gateway.name, "connection_id": identifier, "profile": upstream.profile_id,
                "port": registry.port_for(home, identifier), "protocols": gateway.protocols,
                "messages_auth": gateway.messages_auth, "key": gateway.key, "models": model_entries(gateway, online),
                "usage_file": str(usage_file) if usage_file else None, "log": str(home / "endpoint.log"),
                "watch": watched_files(home, gateway)}
    if quota_mark_at(gateway.connection) is not None:
        resolved["quota_exhausted_at"] = quota_mark_at(gateway.connection)
    reserve = upstream.fallback()
    if reserve is not None:
        resolved["fallback"] = {"connection": reserve.name, "connection_id": reserve.connection["id"],
                                "protocols": reserve.protocols, "messages_auth": reserve.messages_auth, "key": reserve.key}
        if quota_mark_at(reserve.connection) is not None:
            resolved["fallback"]["quota_exhausted_at"] = quota_mark_at(reserve.connection)
    return resolved


def _log_failure(home: Path, text: str) -> None:
    log_line(home / "endpoint.log", text, LOG_MAX_BYTES)


def _main_resolve(options) -> int:
    try:
        text = json.dumps(resolve(options.home, options.connection, options.online), ensure_ascii=False)
    except LauncherError as error:
        message = str(error)
    except Exception as error:  # a mensagem vai para o stderr lido pelo endpoint nativo, que a mostra ao usuário
        message = f"{type(error).__name__}: {error}"
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    sys.stderr.buffer.write(message.encode("utf-8"))
    sys.stderr.buffer.flush()
    return 1


def _main_usage(options) -> int:
    """`usage`: o relatório em JSON UTF-8 no stdout. Código 3 para pedido inválido (mês fora de AAAA-MM), 1 para falha
    na leitura; a mensagem vai ao stderr, lido pelo endpoint nativo."""
    try:
        text = json.dumps(usage_of(options.home, options.month, options.all), ensure_ascii=False)
    except ValueError as error:
        message, code = str(error), 3
    except Exception as error:
        message, code = f"{type(error).__name__}: {error}", 1
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    sys.stderr.buffer.write(message.encode("utf-8"))
    sys.stderr.buffer.flush()
    return code


def quota_check(home: Path, connection_id: str, status: int, body: bytes) -> bool:
    """A resposta de erro `status`/`body` da conexão é cota esgotada? Sendo, grava a marca na conexão. Resposta 2xx limpa a
    marca (a cota voltou) e vale False. LauncherError para conexão inexistente (conferida antes, também quando a resposta
    não é cota)."""
    gateway = Upstream(home, connection_id).get()
    if 200 <= status < 300:
        clear_quota_mark(Path(home), gateway)
        return False
    message = quota_detail(status, body)
    if message is None:
        return False
    mark_quota_exhausted(Path(home), gateway, message)
    return True


def _main_quota(options) -> int:
    """`quota`: o corpo do erro (até ERROR_BODY_LIMIT bytes) vem no stdin; imprime `{"quota": true|false}` em JSON UTF-8.
    Falha (conexão inexistente, cadastro ilegível): código 1 e a mensagem no stderr, lida pelo endpoint nativo."""
    body = sys.stdin.buffer.read(ERROR_BODY_LIMIT + 1)
    try:
        text = json.dumps({"quota": quota_check(options.home, options.connection, options.status, body)})
    except LauncherError as error:
        message = str(error)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    sys.stderr.buffer.write(message.encode("utf-8"))
    sys.stderr.buffer.flush()
    return 1


def _main_supervise(options) -> int:
    from agent_launcher import ROOT
    from agent_launcher.endpoint import supervisor
    child = [sys.executable, str(ROOT / "endpoint.py"), "serve", "--home", str(options.home), "--connection", options.connection]
    if options.port:
        child += ["--port", str(options.port)]
    if options.no_idle_exit:
        child.append("--no-idle-exit")
    try:
        return supervisor.supervise(options.home, options.connection, child, options.port if options.wait_port else None)
    except Exception as error:  # processo oculto: sem isto o erro some junto com o stderr
        _log_failure(options.home, f"supervisor caiu: {type(error).__name__}: {error}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Endpoint local de uma conexão do Agent Launcher")
    parser.add_argument("action", choices=("serve", "resolve", "supervise", "usage", "quota"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--connection", help="obrigatório, menos em usage")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-idle-exit", action="store_true")
    parser.add_argument("--wait-port", action="store_true",
                        help="supervise: esperar a --port ficar livre antes de abrir o serve (adoção de endpoint avulso)")
    parser.add_argument("--online", action="store_true", help="resolve: busca o catálogo no host se o cache está vazio")
    parser.add_argument("--month", metavar="AAAA-MM", help="usage: mês do relatório (padrão: o atual)")
    parser.add_argument("--all", action="store_true", help="usage: soma de todos os perfis")
    parser.add_argument("--status", type=int, help="quota: status HTTP da resposta de erro (o corpo vem no stdin)")
    options = parser.parse_args(argv)
    if options.action == "usage":
        return _main_usage(options)
    if not options.connection:
        parser.error("--connection é obrigatório")
    if options.month or options.all:
        parser.error("--month e --all só valem em usage")
    if options.action == "quota":
        if options.status is None:
            parser.error("--status é obrigatório em quota")
        return _main_quota(options)
    if options.status is not None:
        parser.error("--status só vale em quota")
    if options.action == "resolve":
        return _main_resolve(options)
    if options.online:
        parser.error("--online só vale em resolve")
    if options.wait_port and (options.action != "supervise" or not options.port):
        parser.error("--wait-port só vale em supervise, com --port")
    if options.action == "supervise":
        return _main_supervise(options)
    try:
        return serve(options.home, options.connection, options.port, None if options.no_idle_exit else IDLE_SECONDS)
    except LauncherError as error:
        _log_failure(options.home, f"não iniciou: {error}")
        return 1
    except Exception as error:  # processo oculto: sem isto o erro some junto com o stderr
        _log_failure(options.home, f"caiu: {type(error).__name__}: {error}")
        return 1
