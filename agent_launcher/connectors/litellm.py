"""LiteLLM proxy ou host próprio que o expõe (inclusive via túnel/VPN, com a autenticação injetada no host).

/v1/models diz o que está disponível; /v1/model/info (exclusivo do LiteLLM) traz modo, limites e ferramentas por ID.
O LiteLLM serve /v1/messages, /v1/responses e /v1/chat/completions na mesma base: todos os destinos usam o mesmo endereço.
Com "Nome da chave no host" (key_name), a raiz vira {endereço}/k/<nome> em todas as URLs (catálogo,
verificação e destinos), e o /healthz precisa anunciar key_routing: true antes de listar ou verificar.
Conectores de usuário que embrulham este só precisam repassar key_name nas opções.
"""
from agent_launcher.domain.catalog import merge_records
from agent_launcher.connectors import ConnectorError
from agent_launcher.connectors._http import (KEY_NAME, NO_KEY, TOOL_SEARCH, base_url, bearer, get_json, key_name,
                                             listed, require_key_routing, with_key_name)

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
    KEY_NAME,
)


def _url(options):
    return with_key_name(base_url(options.get("base_url"), EXAMPLE).removesuffix("/v1"), options)


def _routed(options, headers):
    """Raiz da conexão; com key_name, só depois de o host confirmar que separa chaves por nome."""
    url = _url(options)
    if key_name(options):
        require_key_routing(url, headers)
    return url


def targets(options):
    url = _url(options)
    return {"messages": url, "responses": url + "/v1", "chat_completions": url + "/v1",
            "any_model": bool(options.get("any_model")), "messages_auth": "bearer"}


def verify(options):
    headers = bearer(options.get("api_key") or NO_KEY)
    url = _routed(options, headers)
    listed(get_json(url + "/v1/models", headers, timeout=10), url + "/v1/models")


def models(options):
    headers = bearer(options.get("api_key") or NO_KEY)
    url = _routed(options, headers)
    available = listed(get_json(url + "/v1/models", headers), url + "/v1/models")
    try:
        metadata = listed(get_json(url + "/v1/model/info", headers, timeout=25), url + "/v1/model/info")
    except ConnectorError:
        return merge_records(available, []), False
    return merge_records(available, metadata), True
