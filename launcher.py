"""Fachada: modo texto, chamado pelo launcher.cmd, pelo orquestrador (keys, topmost, front) e pelos atalhos. O código mora em agent_launcher.ui.text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I não põe a pasta do script no sys.path

if __name__ == "__main__":
    from agent_launcher.ui.text import script_main
    raise SystemExit(script_main())

from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.ui.text")
