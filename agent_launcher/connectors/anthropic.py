"""API da Anthropic direto: só Claude Code. A chave vai em x-api-key (ANTHROPIC_API_KEY no Claude Code);
/v1/models é paginado (after_id/has_more) e traz max_input_tokens e max_tokens por modelo.
"""
from agent_launcher.connectors._http import gateway

NAME = "anthropic"
LABEL = "Anthropic API"
HINT = "Chave da Anthropic (console). Somente Claude Code."
EXAMPLE = "https://api.anthropic.com"
VERSION = "2023-06-01"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "default": EXAMPLE, "placeholder": EXAMPLE},
    {"name": "api_key", "label": "Chave da Anthropic", "secret": True, "required": True},
)
targets, verify, models = gateway(EXAMPLE, codex=None, chat=None, any_model=False, claude_auth="api_key", key_required=True,
                                  headers=lambda key: {"x-api-key": key, "anthropic-version": VERSION}, page_limit=1000)
