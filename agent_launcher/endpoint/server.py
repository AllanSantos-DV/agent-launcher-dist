"""Endpoint local de UMA conexão: escuta só em 127.0.0.1 e repassa ao host da conexão.

- Rotas: /v1/messages, /v1/responses, /v1/chat/completions (e /v1/messages/count_tokens), cada uma só se a conexão
  serve o protocolo; /v1/models devolve o catálogo do launcher (só modelos com ferramentas); /health; /shutdown.
- Autenticação: a da conexão (chave da DPAPI como Bearer ou x-api-key; nada quando o host já injeta). Qualquer
  credencial que venha do destino (Authorization, x-api-key, api-key) é descartada.
- Streaming (SSE) repassado sem buffer; sem seguir redirecionamento; proxy do sistema só para host público.
- Lê a conexão a cada requisição: trocar host ou chave no launcher vale na próxima chamada, sem mexer no destino.
- Log em ~/.agent-launcher/endpoint.log: método, rota, modelo, status e duração. Nunca corpo nem cabeçalho.
- Uso: o `usage` da resposta (JSON depois de enviado; SSE observado enquanto passa, sem
  segurar nenhum pedaço) vira uma linha em <raiz>/usage.jsonl, só com números. Falha no parse nunca afeta a resposta.
- Encerra sozinho depois de IDLE_SECONDS sem requisição (o autostart no logon desliga o encerramento por ociosidade).
"""
from __future__ import annotations

import argparse
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

from agent_launcher.connectors._http import is_local_host
from agent_launcher.domain.errors import LauncherError

IDLE_SECONDS = 4 * 60 * 60
BODY_LIMIT = 64 * 1024 * 1024
ROUTES = {"/v1/messages": "messages", "/v1/messages/count_tokens": "messages", "/v1/responses": "responses",
          "/v1/chat/completions": "chat_completions"}
CLIENT_CREDENTIALS = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
HOP_BY_HOP = {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
              "content-length", "accept-encoding"}
SAMPLING_FIX = {"messages", "chat_completions"}
RESPONSE_SKIP = {"transfer-encoding", "connection", "keep-alive", "content-length", "content-encoding"}
USAGE_FILE = "usage.jsonl"
JSON_LIMIT = 8 * 1024 * 1024   # corpo JSON maior que isso não é guardado para ler o usage (a resposta passa igual)
LINE_LIMIT = 1024 * 1024       # linha SSE maior que isso é descartada pelo observador


def _count(value) -> int:
    return value if type(value) is int and value > 0 else 0


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
            data = json.loads(raw)
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
                data = json.loads(bytes(self.body))
                if isinstance(data, dict):
                    self.merge(data.get("usage") or (data.get("response") or {}).get("usage"), data.get("model"))
            elif self.kind == "sse" and self.partial.startswith(b"data:") and b"usage" in self.partial:
                self.event(self.partial[5:].strip())
        except Exception:
            return None
        return self.usage


def destination_of(user_agent: str) -> str:
    """Quem chamou o endpoint, pelo User-Agent: VS Code (Copilot Chat) ou Copilot app; senão "endpoint"."""
    agent = (user_agent or "").lower()
    if "vscode" in agent or "copilotchat" in agent or "copilot-chat" in agent:
        return "vscode"
    if "copilot" in agent or "electron" in agent:
        return "copilot_app"
    return "endpoint"


class Upstream:
    """A conexão lida do cadastro, com cache curto: troca de host/chave vale em poucos segundos."""

    def __init__(self, home: Path, connection_id: str, ttl: float = 5):
        self.home, self.connection_id, self.ttl = Path(home), connection_id, ttl
        self._cached, self._at, self._lock = None, 0.0, threading.Lock()
        from agent_launcher.domain import profiles
        self.profile_id = profiles.profile_of_home(self.home)["id"]

    def get(self):
        with self._lock:
            if self._cached is None or time.monotonic() - self._at > self.ttl:
                from agent_launcher.domain.gateway import Gateway
                from agent_launcher.domain.store import Store
                store = Store(self.home)
                self._cached = Gateway(store, store.find_connection(self.connection_id))
                self._at = time.monotonic()
            return self._cached


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
        data = json.loads(body)
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
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or data.get("stream") is not True or "stream_options" in data:
        return body
    data["stream_options"] = {"include_usage": True}
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


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
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/health":
            gateway = self.server.upstream.get()
            return self.reply_json(200, {"agent_launcher_endpoint": True, "connection": gateway.name,
                                         "connection_id": self.server.upstream.connection_id,
                                         "profile": self.server.upstream.profile_id,
                                         "protocols": sorted(gateway.protocols), "requests": self.server.requests,
                                         "started": self.server.started})
        if path in ("/v1/models", "/models"):
            return self.models()
        self.error(404, f"Rota não servida pelo endpoint local do Agent Launcher: {path}")

    def models(self):
        try:
            gateway = self.server.upstream.get()
            catalog = gateway.catalog(offline=True)
            if not catalog.records:
                catalog = gateway.catalog()
        except LauncherError as error:
            return self.error(502, str(error))
        data = [{"id": record.id, "object": "model", "owned_by": record.provider or gateway.name,
                 "max_input_tokens": record.max_input_tokens, "max_output_tokens": record.max_output_tokens}
                for record in catalog.records if record.chat and record.tools is not False]
        self.reply_json(200, {"object": "list", "data": data})

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
            model = str(json.loads(body or b"{}").get("model") or "")[:200]
        except (ValueError, AttributeError):
            pass
        try:
            gateway = self.server.upstream.get()
        except LauncherError as error:
            return self.error(502, str(error))
        if protocol not in gateway.protocols:
            return self.error(400, f"A conexão {gateway.name} não serve {protocol}. Escolha outro modelo ou conexão.")
        url = target_url(gateway, protocol, path, query)
        body = ask_stream_usage(protocol, drop_top_p(protocol, body))
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in HOP_BY_HOP | CLIENT_CREDENTIALS}
        headers.update(auth_headers(gateway, protocol))
        headers["Content-Length"] = str(len(body))
        connection = None
        status = 0
        tap = None
        try:
            connection, target = connect(url, timeout=900)
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if 300 <= status < 400:
                response.read()
                return self.error(502, "O host tentou redirecionar; o endpoint local não segue redirecionamentos "
                                       "(para não vazar a chave). Use o endereço final na conexão.")
            self.send_response(status)
            for key, value in response.getheaders():
                if key.lower() not in RESPONSE_SKIP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if 200 <= status < 300 and not path.endswith("/count_tokens"):
                tap = UsageTap(response.getheader("Content-Type") or "")
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                if tap is not None:
                    tap.feed(chunk)  # depois de enviado: o destino nunca espera pelo observador
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            status = status or 499  # o destino desistiu no meio
        except (OSError, http.client.HTTPException) as error:
            if not status:
                status = 502
                self.error(502, f"Sem resposta do host da conexão {gateway.name}: {error}")
        finally:
            if connection is not None:
                connection.close()
            self.server.record(protocol, path, model, status, time.monotonic() - started)
            if tap is not None:
                self.server.record_usage(protocol, model or tap.model, tap.result(),
                                         destination_of(self.headers.get("User-Agent") or ""))


class EndpointServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, upstream: Upstream, log: Path | None, idle: float | None):
        super().__init__(("127.0.0.1", port), Handler)
        self.upstream, self.log, self.idle = upstream, log, idle
        self.requests, self.started, self.last = 0, time.time(), time.monotonic()
        self._log_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self.usage_file = None
        try:
            from agent_launcher.domain import profiles
            self.usage_file = profiles.base_of(upstream.home) / USAGE_FILE
        except Exception:
            pass

    def touch(self):
        self.last = time.monotonic()

    def handle_error(self, request, client_address):
        """Exceção de um pedido (o stderr deste processo oculto vai para o vazio): só o tipo e a mensagem no endpoint.log."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return  # cliente que desistiu (cancelou o stream): rotina, não defeito
        self.note(f"erro no pedido: {type(error).__name__}: {error}")

    def note(self, message: str) -> None:
        if not self.log:
            return
        with self._log_lock:
            try:
                with self.log.open("a", encoding="utf-8") as stream:
                    stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
            except OSError:
                pass

    def record(self, protocol: str, path: str, model: str, status: int, seconds: float) -> None:
        self.requests += 1
        if not self.log:
            return
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.upstream.connection_id[:8]} POST {path} {protocol} "
                f"model={model!r} status={status} {seconds:.1f}s\n")
        with self._log_lock:
            try:
                with self.log.open("a", encoding="utf-8") as stream:
                    stream.write(line)
            except OSError:
                pass

    def record_usage(self, protocol: str, model: str, usage: dict | None, destination: str) -> None:
        """Uma linha por resposta com usage em <raiz>/usage.jsonl (sem corpo, sem prompt). Nunca levanta."""
        if not usage or not self.usage_file or not any(usage.values()):
            return
        try:
            line = json.dumps({"at": round(time.time(), 3), "profile": self.upstream.profile_id,
                               "connection": self.upstream.connection_id, "model": model, "protocol": protocol,
                               "destination": destination, **usage}, ensure_ascii=False) + "\n"
            with self._usage_lock:
                with self.usage_file.open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except Exception:
            pass

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Endpoint local de uma conexão do Agent Launcher")
    parser.add_argument("action", choices=("serve",))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--connection", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-idle-exit", action="store_true")
    options = parser.parse_args(argv)
    try:
        return serve(options.home, options.connection, options.port, None if options.no_idle_exit else IDLE_SECONDS)
    except LauncherError as error:
        try:
            with (options.home / "endpoint.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} não iniciou: {error}\n")
        except OSError:
            pass
        return 1
    except Exception as error:  # processo oculto: sem isto o erro some junto com o stderr
        try:
            with (options.home / "endpoint.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} caiu: {type(error).__name__}: {error}\n")
        except OSError:
            pass
        return 1
