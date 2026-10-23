"""Fachada: install.py instala os CLIs por aqui. O código mora em agent_launcher.harnesses.installers."""
from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.harnesses.installers")
