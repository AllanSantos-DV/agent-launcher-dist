"""LiteLLM proxy ou host próprio que o expõe (inclusive via túnel/VPN, com a autenticação injetada no host).

/v1/models diz o que está disponível; /v1/model/info (exclusivo do LiteLLM) traz modo, limites e ferramentas por ID.
O LiteLLM serve /v1/messages e /v1/responses na mesma base, então os dois CLIs usam o mesmo endereço.
"""
from catalog import merge_records
from connectors import ConnectorError
from connectors._http import NO_KEY, TOOL_SEARCH, base_url, bearer, get_json, listed

NAME = "litellm"
LABEL = "LiteLLM / host próprio"
HINT = "Proxy LiteLLM local, na rede ou por túnel. Sem chave quando o host já injeta a autenticação."
EXAMPLE = "http://127.0.0.1:4000"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "placeholder": EXAMPLE,
     "hint": "Raiz do proxy, sem /v1."},
    {"name": "api_key", "label": "Chave", "secret": True, "hint": "Opcional: virtual key do LiteLLM."},
    {"name": "any_model", "label": "Qualquer modelo nos dois CLIs", "type": "bool", "default": False,
     "hint": "Desmarcado: Claude Code só lista claude-*, Codex só gpt-*/o*. Marque se o proxy traduz os demais."},
    TOOL_SEARCH,
)


def _url(options):
    return base_url(options.get("base_url"), EXAMPLE).removesuffix("/v1")


def targets(options):
    url = _url(options)
    return {"claude": url, "codex": url + "/v1", "any_model": bool(options.get("any_model")), "claude_auth": "bearer"}


def verify(options):
    listed(get_json(_url(options) + "/v1/models", bearer(options.get("api_key") or NO_KEY), timeout=10),
           _url(options) + "/v1/models")


def models(options):
    url, headers = _url(options), bearer(options.get("api_key") or NO_KEY)
    available = listed(get_json(url + "/v1/models", headers), url + "/v1/models")
    try:
        metadata = listed(get_json(url + "/v1/model/info", headers, timeout=25), url + "/v1/model/info")
    except ConnectorError:
        return merge_records(available, []), False
    return merge_records(available, metadata), True
