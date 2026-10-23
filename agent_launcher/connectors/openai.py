"""API da OpenAI direto: só Codex, via Responses em https://api.openai.com/v1. /v1/models não informa contexto;
o Codex usa os metadados nativos dele para os modelos OpenAI.
"""
from agent_launcher.connectors._http import gateway

NAME = "openai"
LABEL = "OpenAI API"
HINT = "Chave da OpenAI (platform). Somente Codex."
EXAMPLE = "https://api.openai.com/v1"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "default": EXAMPLE, "placeholder": EXAMPLE},
    {"name": "api_key", "label": "Chave da OpenAI", "secret": True, "required": True},
)
targets, verify, models = gateway(EXAMPLE, claude=None, codex="", models_path="/models", any_model=False,
                                  key_required=True)
