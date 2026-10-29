"""Conectores: como o Agent Launcher fala com cada tipo de host de modelos (padrão do model gateway do Smart Tool).

Um conector é um módulo Python com:
  NAME     id único (str)
  LABEL    nome exibido
  HINT     uma frase explicando quando usar
  FIELDS   sequência de {"name", "label", opcionais "secret", "required", "placeholder", "default", "hint",
           "type": "text" | "bool"}; campos secretos ficam cifrados (DPAPI), o resto no config.json.
           O campo secreto "api_key" é a chave entregue aos destinos (sem ele, vai um marcador que não é segredo).
  targets(options) -> dict com os PROTOCOLOS que o host serve:
           {"messages": URL base Anthropic Messages (o cliente acrescenta /v1/messages) ou None,
            "responses": URL base OpenAI Responses (termina em /v1; o cliente acrescenta /responses) ou None,
            "chat_completions": URL base OpenAI Chat Completions (termina em /v1) ou None,
            "any_model": True quando o host traduz qualquer modelo para os protocolos que serve,
            "messages_auth": "bearer" (Authorization: Bearer) ou "api_key" (header x-api-key)}
           O formato antigo ({"claude", "codex", "claude_auth"}) continua aceito: normalize_targets() o traduz.
  verify(options) -> None, levantando ConnectorError com um motivo acionável; login por código (device flow):
           levante LoginPending(mensagem, uri, código, expires_at, interval) enquanto aguarda a autorização;
           levante QuotaExhausted(mensagem) só quando login e assinatura estão válidos e a cota acabou (a conexão é
           gravada com o aviso e o estado "cota esgotada")
  models(options) -> (list[ModelInfo], contexto_confirmado: bool)

Várias conexões e perfis:
  - O mesmo conector serve quantas conexões o usuário quiser; cada conexão guarda as suas opções e a sua chave.
  - Perfis de trabalho (um por cliente, por exemplo) têm conexões e projetos próprios. Os conectores do usuário ficam
    em ~/.agent-launcher/connectors e valem para todos os perfis; o conector não sabe em qual perfil está.
  - Host que guarda várias chaves e escolhe pela URL: ponha `_http.KEY_NAME` em FIELDS e repasse `key_name` ao
    conector `litellm` (ou use `_http.with_key_name(url, options)` e `_http.require_key_routing(raiz, headers)`).
    O nome entra como /k/<nome> em todas as URLs da conexão, e o host precisa anunciar "key_routing": true no
    /healthz; sem isso a conexão é recusada. Conexão sem `key_name` não muda nada.

Os embutidos ficam neste pacote; qualquer .py em ~/.agent-launcher/connectors é carregado como conector do usuário.
Um arquivo que falha ao carregar aparece com o erro e não afeta os outros.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

PROTOCOLS = ("messages", "responses", "chat_completions")
LEGACY = {"claude": "messages", "codex": "responses"}


class ConnectorError(Exception):
    pass


class LoginPending(ConnectorError):
    """Login interativo em andamento (fluxo de dispositivo): verify() levanta isto enquanto o usuário não autorizou.
    A interface mostra o endereço, o código e o tempo restante (expires_at, epoch em segundos) e chama verify() de novo a
    cada `interval` segundos até autorizar; a mensagem serve ao modo texto."""

    def __init__(self, message: str, uri: str, code: str, expires_at: float, interval: float = 5.0):
        super().__init__(message)
        self.uri, self.code, self.expires_at, self.interval = uri, code, float(expires_at), float(interval)


class QuotaExhausted(ConnectorError):
    """Login e assinatura válidos; a única falha é a cota esgotada. A conexão é gravada com este aviso (estado "cota
    esgotada", nunca "funcionando") e passa a servir quando a cota voltar ou com outra conta. Qualquer outra falha
    (login, conta errada, sem assinatura, rede) continua ConnectorError e impede gravar."""


def normalize_targets(raw: dict) -> dict:
    """targets() de qualquer conector no formato de protocolos, com as chaves antigas mantidas (claude, codex,
    claude_auth) para quem ainda as lê. Conector do usuário no formato antigo ganha os protocolos equivalentes."""
    if not isinstance(raw, dict):
        raise ConnectorError("targets() precisa devolver um dicionário.")
    result = {protocol: raw.get(protocol) or None for protocol in PROTOCOLS}
    for old, protocol in LEGACY.items():
        if result[protocol] is None and raw.get(old):
            result[protocol] = raw[old]
    for protocol in PROTOCOLS:
        if result[protocol] is not None and not isinstance(result[protocol], str):
            raise ConnectorError(f"targets()[{protocol!r}] precisa ser uma URL ou None.")
    result["any_model"] = bool(raw.get("any_model"))
    auth = raw.get("messages_auth") or raw.get("claude_auth") or "bearer"
    if auth not in ("bearer", "api_key"):
        raise ConnectorError(f"messages_auth inválido: {auth!r} (use bearer ou api_key).")
    result["messages_auth"] = auth
    result["claude"], result["codex"], result["claude_auth"] = result["messages"], result["responses"], auth
    return result


BUILTIN_NAMES = ("litellm", "ninerouter", "omniroute", "openrouter", "anthropic", "openai",
                 "anthropic_compatible", "openai_compatible")
BUILTIN = tuple(importlib.import_module(f"{__name__}.{name}") for name in BUILTIN_NAMES)


def _check(module, origin: str):
    for attribute in ("NAME", "LABEL", "FIELDS"):
        if not getattr(module, attribute, None):
            raise ValueError(f"{origin}: falta {attribute}")
    for function in ("targets", "verify", "models"):
        if not callable(getattr(module, function, None)):
            raise ValueError(f"{origin}: falta a função {function}()")
    for item in module.FIELDS:
        if not isinstance(item, dict) or not item.get("name") or not item.get("label"):
            raise ValueError(f"{origin}: cada item de FIELDS precisa de name e label")
    return module


def available(user_dir: Path | None = None) -> tuple[dict, list[str]]:
    """({NAME: módulo}, [erros de carga]) com os embutidos e os do usuário."""
    found = {module.NAME: module for module in BUILTIN}
    errors = []
    if user_dir is None or not user_dir.is_dir():
        return found, errors
    for path in sorted(user_dir.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"agent_launcher_connector_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _check(module, path.name)
            if module.NAME in found:
                raise ValueError(f"NAME {module.NAME!r} já existe")
            found[module.NAME] = module
        except Exception as error:
            errors.append(f"{path.name}: {type(error).__name__}: {error}")
    return found, errors


def defaults(module) -> dict:
    return {item["name"]: item.get("default", False if item.get("type") == "bool" else "")
            for item in module.FIELDS if not item.get("secret")}
