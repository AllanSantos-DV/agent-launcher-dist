"""HTTP compartilhado pelos conectores: só biblioteca padrão, sem seguir redirecionamentos e com limite de resposta."""
from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

from catalog import MODEL_PATTERN, ModelInfo, base_model, positive_integer
from connectors import ConnectorError

LIMIT = 16 * 1024 * 1024
NO_KEY = "agent-launcher-no-key"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ConnectorError(f"O host tentou redirecionar para {new_url}. Conexão interrompida para não vazar a chave; "
                             "use o endereço final.")


def base_url(value: object, example: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise ConnectorError(f"Endereço inválido: {url or '(vazio)'}. Exemplo: {example}")
    return url


def is_local_host(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _opener(url: str):
    # Host local/privado (loopback, LAN, VPN) nunca passa pelo proxy HTTP corporativo; hosts públicos usam o do sistema.
    handlers = [_NoRedirect()]
    if is_local_host(urllib.parse.urlparse(url).hostname or ""):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def get_json(url: str, headers: dict | None = None, timeout: float = 15) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ConnectorError(f"Resposta de {url} acima de {LIMIT // (1024 * 1024)} MB.")
        result = json.loads(body)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ConnectorError(f"O host recusou a chave (HTTP {error.code}) em {url}.") from None
        if error.code == 404:
            raise ConnectorError(f"{url} não existe (HTTP 404). Confira o endereço base da conexão.") from None
        raise ConnectorError(f"{url} respondeu HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise ConnectorError(f"Sem resposta de {url}: {getattr(error, 'reason', error)}") from None
    except (ValueError, UnicodeError):
        raise ConnectorError(f"{url} não devolveu JSON válido.") from None
    if not isinstance(result, dict):
        raise ConnectorError(f"{url} devolveu JSON fora do formato esperado (objeto).")
    return result


def auth_probe(url: str, headers: dict, timeout: float = 15) -> None:
    """Confere a chave com um POST de corpo vazio: o host autentica antes de validar o corpo, então 401/403 é chave
    recusada e qualquer outro status (400 "Missing model"...) é chave aceita, sem gerar nenhuma resposta de modelo."""
    request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json", **headers},
                                     method="POST")
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            response.read(1024)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ConnectorError(f"O host recusou a chave (HTTP {error.code}) em {url}.") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise ConnectorError(f"Sem resposta de {url}: {getattr(error, 'reason', error)}") from None


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token} if token else {}


def listed(payload: dict, url: str) -> list:
    data = payload.get("data")
    if not isinstance(data, list):
        raise ConnectorError(f"{url} não devolveu a lista no formato {{\"data\": [...]}}.")
    return data


def ids(entries: list) -> list[str]:
    return sorted({base_model(entry["id"]) for entry in entries
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str) and MODEL_PATTERN.fullmatch(entry["id"])},
                  key=str.casefold)


def plain_records(entries: list, extra: str = "") -> list[ModelInfo]:
    """IDs sem metadados (OpenAI, roteadores sem contexto) + IDs extras digitados na conexão."""
    names = set(ids(entries))
    names.update(item.strip() for item in str(extra or "").split(",") if MODEL_PATTERN.fullmatch(item.strip()))
    return [ModelInfo(name) for name in sorted(names, key=str.casefold)]


def _first(*values) -> int | None:
    return next((value for value in map(positive_integer, values) if value is not None), None)


def router_records(entries: list, extra: str = "") -> tuple[list[ModelInfo], bool]:
    """Lista /v1/models com os metadados que cada roteador publica: max_input_tokens/max_output_tokens (Anthropic,
    OmniRoute), context_length/top_provider/supported_parameters (OpenRouter) e capabilities (9router, OmniRoute).
    Campo ausente continua desconhecido: nunca vira capacidade positiva."""
    records = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not MODEL_PATTERN.fullmatch(entry["id"]):
            continue
        capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
        top = entry.get("top_provider") if isinstance(entry.get("top_provider"), dict) else {}
        architecture = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
        tools = next((value for value in (capabilities.get("tools"), capabilities.get("tool_calling"))
                      if type(value) is bool), None)
        if tools is None and isinstance(entry.get("supported_parameters"), list):
            tools = "tools" in entry["supported_parameters"]
        outputs = architecture.get("output_modalities")
        mode = "other" if isinstance(outputs, list) and "text" not in outputs else None
        identifier = base_model(entry["id"])
        records[identifier] = ModelInfo(
            identifier, mode,
            _first(entry.get("max_input_tokens"), entry.get("context_length"), top.get("context_length"),
                   capabilities.get("contextWindow")),
            _first(entry.get("max_output_tokens"), entry.get("max_tokens"), entry.get("max_completion_tokens"),
                   top.get("max_completion_tokens"), capabilities.get("maxOutput")),
            tools,
        )
    for record in plain_records([], extra):
        records.setdefault(record.id, record)
    result = [records[key] for key in sorted(records, key=str.casefold)]
    return result, any(record.max_input_tokens for record in result)


TOOL_SEARCH = {"name": "tool_search", "label": "Tool search de MCP no Claude Code", "type": "bool", "default": False,
               "hint": "Carrega as ferramentas MCP sob demanda (bem menos contexto). Só ligue se o host suportar "
                       "tool_reference: sem esse suporte as requisições do Claude Code falham."}

EXTRA_MODELS = {"name": "extra_models", "label": "Modelos extras",
                "placeholder": "id-1, id-2", "hint": "IDs separados por vírgula para hosts que não listam todos os modelos."}


def gateway(example: str, *, claude: str | None = "", codex: str | None = "/v1", models_path: str = "/v1/models",
            any_model: bool = True, claude_auth: str = "bearer", key_required: bool = False, headers=None,
            page_limit: int | None = None, listing_optional: bool = False):
    """targets/verify/models de um host HTTP. `claude`/`codex` são sufixos sobre o endereço (None = CLI não atendido).
    `headers(options)` substitui o Bearer padrão; `page_limit` ativa a paginação after_id/has_more da Anthropic.
    `listing_optional`: host sem /models funciona só com os "Modelos extras" digitados (a falha vira erro sem eles)."""

    def url(options):
        return base_url(options.get("base_url") or example, example)

    def auth(options):
        key = options.get("api_key") or ""
        if key_required and not key:
            raise ConnectorError("Esta conexão exige uma chave de API.")
        if headers:
            return headers(key)
        if options.get("x_api_key"):
            return {"x-api-key": key or NO_KEY, "anthropic-version": "2023-06-01"}
        return bearer(key or NO_KEY)

    def targets(options):
        root = url(options)
        return {"claude": None if claude is None else root + claude, "codex": None if codex is None else root + codex,
                "any_model": any_model, "claude_auth": "api_key" if options.get("x_api_key") else claude_auth}

    def fetch(options, timeout=15):
        address = url(options) + models_path
        if page_limit is None:
            return listed(get_json(address, auth(options), timeout), address)
        entries, after = [], ""
        for _page in range(50):
            query = f"?limit={page_limit}" + (f"&after_id={urllib.parse.quote(after)}" if after else "")
            payload = get_json(address + query, auth(options), timeout)
            entries += listed(payload, address)
            after = payload.get("last_id") or ""
            if payload.get("has_more") is not True or not after:
                return entries
        raise ConnectorError(f"{address} paginou além de 50 páginas; lista interrompida.")

    def listing(options, timeout=15):
        try:
            return fetch(options, timeout)
        except ConnectorError:
            if listing_optional and str(options.get("extra_models") or "").strip():
                return []
            raise

    def verify(options):
        listing(options, timeout=10)

    def models(options):
        return router_records(listing(options), options.get("extra_models", ""))

    return targets, verify, models
