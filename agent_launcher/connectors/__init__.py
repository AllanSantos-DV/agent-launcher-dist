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
           ModelInfo leva entrada (max_input_tokens), saída (max_output_tokens), ferramentas (tools), modo e as
           entradas de mídia vision, audio_input e video_input (True aceita, False não aceita); campo ausente fica None
           (desconhecido), e o conector nunca troca "não informado" por False. Os conectores HTTP embutidos usam _http.router_records, que aceita em
           cada item de /v1/models (vale o primeiro inteiro positivo, nesta ordem):
             entrada      max_input_tokens, context_length, top_provider.context_length, capabilities.contextWindow,
                          context_window, limits.input, max_model_len
             saída        max_output_tokens, max_tokens, max_completion_tokens, top_provider.max_completion_tokens,
                          capabilities.maxOutput, limits.output
             ferramentas  capabilities.tools, capabilities.tool_calling, "tools" em supported_parameters
             modo         architecture.output_modalities sem "text" vira "other"
             visão        capabilities.vision, "image" em architecture.input_modalities
             áudio/vídeo  "audio"/"video" em architecture.input_modalities (lista presente sem o item = False)
           O LiteLLM lê model_info de /v1/model/info, ou de /model/info quando o host só serve essa (mode, max_input_tokens, max_output_tokens,
           supports_function_calling, supports_responses_api, supports_vision, supports_audio_input,
           supports_video_input); host sem nenhuma das duas rotas tem os mesmos campos lidos da raiz
           de cada item de /v1/models. contexto_confirmado é True quando algum modelo traz a entrada.
  SEGREGATES_SOURCES  (opcional, bool; ausente = False) o conector se declara com segregação por fonte: cada modelo
           pertence a UMA fonte, dita em ModelInfo.source, com o nome exato que o conector quer ver na tela. Só então o
           launcher guarda e mostra a fonte (coluna, filtro, `models --source`, campo `source` do /v1/models). Fonte não é
           provider: é o caminho de acesso pelo qual o modelo é alcançado. O launcher não cadastra, junta nem renomeia
           fontes; o mesmo modelo por duas fontes são dois ids distintos, escolhidos pelo conector. models() que traz
           um id com 2+ fontes é recusado com ConnectorError. Sem a declaração a fonte é descartada ao gravar o catálogo.
  costs(options, month) -> dict   (opcional; só vale com SEGREGATES_SOURCES = True) o custo do mês por fonte, dado pelo
           conector: o launcher não sabe de onde vem e não estima nada. `month` é "AAAA-MM". Devolve
             {"currency": "USD", "sources": [{"source": str, "cost": número, "limit": número ou None,
                                              "tokens_in": inteiro ou None, "tokens_out": inteiro ou None}]}
           `currency` é o código da moeda de `cost` e de `limit`; `source` repete o nome dado em ModelInfo.source, uma
           vez só por fonte; `cost` e `limit` não são negativos (`limit` None: a fonte não tem teto); os tokens são
           inteiros não negativos ou None. Formato fora disso levanta ConnectorError no uso (fonte repetida, custo não
           numérico ou negativo...). Falha de rede: ConnectorError, como em models(). Só `usage --by-source` e o diálogo
           "Uso do mês" chamam costs(), e só com a rota presente; sem ela não há custo por fonte em lugar nenhum.

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
from pathlib import Path

from agent_launcher.domain.user_modules import load_user_modules

PROTOCOLS = ("messages", "responses", "chat_completions")
LEGACY = {"claude": "messages", "codex": "responses"}


class ConnectorError(Exception):
    """`transient`: o host não respondeu (rede, tempo esgotado, 502/503/504); repetir pode funcionar."""

    transient = False


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
                 "anthropic_compatible", "openai_compatible", "ollama")
BUILTIN = tuple(importlib.import_module(f"{__name__}.{name}") for name in BUILTIN_NAMES)


def declares_sources(module) -> bool:
    """O conector declara segregação por fonte (`SEGREGATES_SOURCES = True` no módulo)? Ausente vale False."""
    return getattr(module, "SEGREGATES_SOURCES", False) is True


def offers_costs(module) -> bool:
    """O módulo tem a rota opcional `costs(options, month)`?"""
    return callable(getattr(module, "costs", None))


def _amount(value, name: str, where: str, *, integer: bool = False) -> int | float:
    """Número finito e não negativo (inteiro, se `integer`); ConnectorError diz qual campo corrigir."""
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or value != value or value in (float("inf"), float("-inf")):
        raise ConnectorError(f"costs(): {where}: {name} precisa ser um número {'inteiro ' if integer else ''}"
                             f"não negativo, não {value!r}.")
    if value < 0:
        raise ConnectorError(f"costs(): {where}: {name} não pode ser negativo ({value!r}).")
    return value


def normalize_costs(raw) -> dict:
    """Resposta de `costs()` conferida contra o contrato e levada ao formato completo (campos ausentes de `limit`,
    `tokens_in` e `tokens_out` viram None; chaves desconhecidas somem). ConnectorError acionável se malformada."""
    if not isinstance(raw, dict):
        raise ConnectorError(f"costs() precisa devolver um dicionário com currency e sources, não {type(raw).__name__}.")
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise ConnectorError("costs(): currency precisa ser o código da moeda (por exemplo \"USD\").")
    sources = raw.get("sources")
    if not isinstance(sources, list):
        raise ConnectorError("costs(): sources precisa ser uma lista de fontes.")
    found, seen = [], set()
    for index, item in enumerate(sources):
        where = f"sources[{index}]"
        if not isinstance(item, dict):
            raise ConnectorError(f"costs(): {where} precisa ser um dicionário com source e cost.")
        name = item.get("source")
        if not isinstance(name, str) or not name.strip():
            raise ConnectorError(f"costs(): {where}: source precisa ser o nome da fonte.")
        if name in seen:
            raise ConnectorError(f"costs(): a fonte {name!r} aparece mais de uma vez; some o custo dela numa linha só.")
        seen.add(name)
        where = f"fonte {name!r}"
        entry = {"source": name, "cost": _amount(item.get("cost"), "cost", where), "limit": None,
                 "tokens_in": None, "tokens_out": None}
        if item.get("limit") is not None:
            entry["limit"] = _amount(item["limit"], "limit", where)
        for field in ("tokens_in", "tokens_out"):
            if item.get(field) is not None:
                entry[field] = _amount(item[field], field, where, integer=True)
        found.append(entry)
    return {"currency": currency.strip(), "sources": found}


def _check(module, origin: str):
    for attribute in ("NAME", "LABEL", "FIELDS"):
        if not getattr(module, attribute, None):
            raise ValueError(f"{origin}: falta {attribute}")
    if not isinstance(getattr(module, "SEGREGATES_SOURCES", False), bool):
        raise ValueError(f"{origin}: SEGREGATES_SOURCES precisa ser True ou False")
    for function in ("targets", "verify", "models"):
        if not callable(getattr(module, function, None)):
            raise ValueError(f"{origin}: falta a função {function}()")
    if hasattr(module, "costs") and not offers_costs(module):
        raise ValueError(f"{origin}: costs precisa ser a função costs(options, month)")
    for item in module.FIELDS:
        if not isinstance(item, dict) or not item.get("name") or not item.get("label"):
            raise ValueError(f"{origin}: cada item de FIELDS precisa de name e label")
    return module


def available(user_dir: Path | None = None) -> tuple[dict, list[str]]:
    """({NAME: módulo}, [erros de carga]) com os embutidos e os do usuário."""
    found = {module.NAME: module for module in BUILTIN}

    def accept(module, name):
        _check(module, name)
        if module.NAME in found:
            raise ValueError(f"NAME {module.NAME!r} já existe")
        found[module.NAME] = module

    return found, load_user_modules(user_dir, "agent_launcher_connector", accept)

