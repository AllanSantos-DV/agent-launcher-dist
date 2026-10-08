"""Conectores: como o Agent Launcher fala com cada tipo de host de modelos (padrão do model gateway do Smart Tool).

Um conector é um módulo Python com:
  NAME     id único (str)
  LABEL    nome exibido
  HINT     uma frase explicando quando usar
  FIELDS   sequência de {"name", "label", opcionais "secret", "required", "placeholder", "default", "hint",
           "type": "text" | "bool"}; campos secretos ficam cifrados (DPAPI), o resto no config.json.
           O campo secreto "api_key" é a chave entregue aos CLIs (sem ele, vai um marcador que não é segredo).
  targets(options) -> dict
           {"claude": URL base Anthropic Messages (o Claude Code acrescenta /v1/messages) ou None,
            "codex": URL base OpenAI Responses (termina em /v1; o Codex acrescenta /responses) ou None,
            "any_model": True quando o host traduz qualquer modelo para os dois protocolos,
            "claude_auth": "bearer" (ANTHROPIC_AUTH_TOKEN) ou "api_key" (ANTHROPIC_API_KEY, header x-api-key)}
  verify(options) -> None, levantando ConnectorError com um motivo acionável
  models(options) -> (list[ModelInfo], contexto_confirmado: bool)

Os embutidos ficam neste pacote; qualquer .py em ~/.agent-launcher/connectors é carregado como conector do usuário.
Um arquivo que falha ao carregar aparece com o erro e não afeta os outros.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path


class ConnectorError(Exception):
    pass


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
