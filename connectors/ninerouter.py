"""9router (pacote npm 9router): roteador local que traduz qualquer modelo para Anthropic Messages e OpenAI Responses.

Porta padrão 20128; /v1/models responde sem chave e traz capabilities.contextWindow/maxOutput/tools por modelo.
As rotas de inferência exigem a chave criada no painel do 9router.
"""
from connectors._http import gateway

NAME = "9router"
LABEL = "9router"
HINT = "Roteador local 9router (npm). Use a chave criada no painel dele."
EXAMPLE = "http://127.0.0.1:20128"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "default": EXAMPLE, "placeholder": EXAMPLE},
    {"name": "api_key", "label": "Chave do 9router", "secret": True, "required": True},
)
targets, verify, models = gateway(EXAMPLE)
