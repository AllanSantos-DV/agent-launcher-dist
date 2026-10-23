"""Fachadas da raiz: `import desktop` devolve o próprio módulo do pacote (mesmo objeto), então quem troca um atributo
(testes, orquestrador) troca o que o pacote usa."""
import importlib
import sys


def alias(name: str, target: str):
    module = importlib.import_module(target)
    sys.modules[name] = module
    return module
