"""9router (pacote npm 9router): roteador local que traduz qualquer modelo para Anthropic Messages e OpenAI Responses.

Porta padrão 20128; /v1/models responde sem chave e traz capabilities.contextWindow/maxOutput/tools por modelo.
As rotas de inferência exigem a chave criada no painel do 9router; como /v1/models não confere a chave, o teste da
conexão faz um POST vazio em /v1/messages (401 = chave recusada; 400 "Missing model" = chave aceita, nada gerado).
"""
from agent_launcher.connectors._http import TOOL_SEARCH, auth_probe, base_url, bearer, gateway

NAME = "9router"
LABEL = "9router"
HINT = "Roteador local 9router (npm). Use a chave criada no painel dele."
EXAMPLE = "http://127.0.0.1:20128"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "default": EXAMPLE, "placeholder": EXAMPLE},
    {"name": "api_key", "label": "Chave do 9router", "secret": True, "required": True},
    TOOL_SEARCH,
)
targets, _list, models = gateway(EXAMPLE)


def verify(options):
    _list(options)
    auth_probe(base_url(options.get("base_url") or EXAMPLE, EXAMPLE) + "/v1/messages", bearer(options.get("api_key") or ""))
