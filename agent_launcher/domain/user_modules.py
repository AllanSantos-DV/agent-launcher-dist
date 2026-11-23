"""Carga dos módulos Python do usuário (conectores e adaptadores)."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def load_user_modules(folder: Path | None, prefix: str, accept) -> list[str]:
    """Importa cada `*.py` de `folder` como módulo `<prefix>_<nome>` e o entrega a `accept(módulo, nome do arquivo)`,
    que levanta se ele não serve. Um arquivo com erro não derruba os outros: devolve uma linha
    "arquivo: Tipo: mensagem" por falha."""
    errors = []
    if folder is None or not folder.is_dir():
        return errors
    for path in sorted(folder.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"{prefix}_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            accept(module, path.name)
        except Exception as error:
            errors.append(f"{path.name}: {type(error).__name__}: {error}")
    return errors
