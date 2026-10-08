"""Qualquer host compatível com OpenAI Responses (o Codex só aceita wire_api=responses; /chat/completions não basta).

O endereço termina onde a API começa, como o base_url do SDK da OpenAI (normalmente em /v1).
"""
from connectors._http import EXTRA_MODELS, gateway

NAME = "openai_compatible"
LABEL = "Compatível com OpenAI"
HINT = "Host que fala OpenAI Responses (vLLM, Ollama recente, proxies). Somente Codex."
EXAMPLE = "http://127.0.0.1:11434/v1"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "placeholder": EXAMPLE,
     "hint": "Onde a API começa, normalmente terminando em /v1."},
    {"name": "api_key", "label": "Chave", "secret": True, "hint": "Opcional para hosts locais."},
    EXTRA_MODELS,
)
targets, verify, models = gateway(EXAMPLE, claude=None, codex="", models_path="/models", listing_optional=True)
