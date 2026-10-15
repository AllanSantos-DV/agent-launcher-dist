"""Nome, versão e pasta de dados do Agent Launcher (fonte única; o instalador também importa este módulo)."""
import os
from pathlib import Path

APP_NAME = "Agent Launcher"
APP_ID = "agent-launcher"
VERSION = "1.0.17"
# Identidade na barra de tarefas: sem ela o Windows agrupa o app sob o pythonw.exe, com o ícone do Python.
APP_USER_MODEL_ID = "AgentLauncher.App"


def data_home() -> Path:
    """~/.agent-launcher, ou AGENT_LAUNCHER_HOME para isolar testes e perfis."""
    return Path(os.environ.get("AGENT_LAUNCHER_HOME") or Path.home() / ".agent-launcher").expanduser()
