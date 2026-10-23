"""Fachada: a janela da sessão continua sendo `desktop.py` (o orquestrador a acha pela linha de comando) e `from desktop import start_session` continua valendo. O código mora em agent_launcher.session.window."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I não põe a pasta do script no sys.path

if __name__ == "__main__":
    from agent_launcher.session.window import main
    raise SystemExit(main())

from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.session.window")
