"""OmniRoute: gateway que agrega centenas de provedores atrás de /v1/messages, /v1/responses e /v1/chat/completions.

Porta padrão 20128 (a mesma do 9router: não rode os dois na mesma porta). A chave é opcional enquanto
REQUIRE_API_KEY=false no OmniRoute; /v1/models traz context_length, max_input_tokens e max_output_tokens.
"""
from agent_launcher.connectors._http import TOOL_SEARCH, gateway

NAME = "omniroute"
LABEL = "OmniRoute"
HINT = "Gateway OmniRoute local ou na rede. Chave opcional, conforme REQUIRE_API_KEY do servidor."
EXAMPLE = "http://127.0.0.1:20128"
FIELDS = (
    {"name": "base_url", "label": "Endereço", "required": True, "default": EXAMPLE, "placeholder": EXAMPLE},
    {"name": "api_key", "label": "Chave do OmniRoute", "secret": True, "hint": "Opcional."},
    TOOL_SEARCH,
)
targets, verify, models = gateway(EXAMPLE)
