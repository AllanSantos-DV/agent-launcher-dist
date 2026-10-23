"""Fachada: o endpoint local roda como `pythonw endpoint.py serve --home ... --connection ...` (oculto)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from agent_launcher.endpoint.server import main
    raise SystemExit(main())
