"""Fachada: a etapa elevada roda como `python -I elevated.py apply ...` (um clique de UAC). O código mora em agent_launcher.platform.elevated."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I não põe a pasta do script no sys.path

if __name__ == "__main__":
    from agent_launcher.platform.elevated import main
    raise SystemExit(main())

from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.platform.elevated")
