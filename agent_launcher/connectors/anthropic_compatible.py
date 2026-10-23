"""Qualquer host compatível com Anthropic Messages (o Claude Code acrescenta /v1/messages ao endereço).

A lista vem de {endereço}/v1/models no formato {"data": [{"id": ...}]}; hosts que não listam usam "Modelos extras".
"""
from agent_launcher.connectors._http import EXTRA_MODELS, TOOL_SEARCH, gateway

NAME = "anthropic_compatible"
LABEL = "Compatível com Anthropic"
HINT = "Host próprio ou de terceiros que fala Anthropic Messages. Somente Claude Code."
EXAMPLE = "https://meu-host.exemplo/anthropic"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "placeholder": EXAMPLE,
     "hint": "Base onde o Claude Code acrescenta /v1/messages."},
    {"name": "api_key", "label": "Chave", "secret": True},
    {"name": "x_api_key", "label": "Enviar a chave como x-api-key", "type": "bool", "default": False,
     "hint": "Desmarcado: Authorization: Bearer (ANTHROPIC_AUTH_TOKEN)."},
    TOOL_SEARCH,
    EXTRA_MODELS,
)
targets, verify, models = gateway(EXAMPLE, codex=None, chat=None, listing_optional=True)
