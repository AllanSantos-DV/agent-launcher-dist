"""Fachada: a verificação em segundo plano roda como `updater.py --background`. O código mora em agent_launcher.update.updater."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I não põe a pasta do script no sys.path

if __name__ == "__main__":
    from agent_launcher.update.updater import background_main
    raise SystemExit(background_main())

from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.update.updater")
