"""OpenRouter: Claude Code por https://openrouter.ai/api (Messages, chave como Bearer e ANTHROPIC_API_KEY vazio)
e Codex por https://openrouter.ai/api/v1 (Responses). /api/v1/models traz context_length, top_provider e
supported_parameters (com "tools" quando o modelo aceita ferramentas).
"""
from connectors._http import TOOL_SEARCH, gateway

NAME = "openrouter"
LABEL = "OpenRouter"
HINT = "openrouter.ai com a sua chave. Qualquer modelo com ferramentas nos dois CLIs."
EXAMPLE = "https://openrouter.ai/api"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "default": EXAMPLE, "placeholder": EXAMPLE},
    {"name": "api_key", "label": "Chave do OpenRouter", "secret": True, "required": True},
    TOOL_SEARCH,
)
targets, verify, models = gateway(EXAMPLE, key_required=True)
