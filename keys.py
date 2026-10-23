"""Fachada: o auxiliar de teclas roda como `python -I keys.py --inject|--screen`. O código mora em agent_launcher.platform.keys."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I não põe a pasta do script no sys.path

if __name__ == "__main__":
    from agent_launcher.platform.keys import helper_main
    raise SystemExit(helper_main(sys.argv))

from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.platform.keys")
