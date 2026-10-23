"""Pedido de fechamento da janela de sessão pelo orquestrador (contrato: ~/.cli-launcher/close-requests/<pid>)."""
from __future__ import annotations

import time
from pathlib import Path

# O arquivo <pid> é o pid do desktop.py da janela. Com ele presente quando o CLI termina, a janela sai com 0 e o
# Terminal fecha a aba.
CLOSE_REQUESTS_DIR = Path.home() / ".cli-launcher" / "close-requests"
CLOSE_REQUEST_MAX_AGE_S = 120


def consume_close_request(pid: int, folder: Path | None = None) -> bool:
    """True (e apaga o pedido) se o orquestrador pediu agora para fechar a janela deste processo.

    Pedido antigo é apagado e ignorado: sobrou de uma janela que morreu sem consumi-lo, e o Windows reaproveita PIDs,
    então valeria para uma janela nova que nada tem a ver com ele.
    """
    request = (folder or CLOSE_REQUESTS_DIR) / str(pid)
    try:
        age = time.time() - request.stat().st_mtime
        request.unlink()
    except FileNotFoundError:
        return False
    return age <= CLOSE_REQUEST_MAX_AGE_S
