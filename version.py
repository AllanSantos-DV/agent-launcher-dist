"""Nome, versão e pasta de dados do Agent Launcher (fonte única; o instalador também importa este módulo)."""
import os
from pathlib import Path

APP_NAME = "Agent Launcher"
APP_ID = "agent-launcher"
VERSION = "1.2.11"
# Chave pública Ed25519 do produto no engine-registry (a privada fica só na máquina de quem publica). Tem de ser igual
# à release.publicKey de docs/registry-app-entry.json: quem confere um pacote usa esta, nunca só a do manifest.
REGISTRY_PUBLIC_KEY = "bbfc8c1a639872d4e5fc44ac4172443886153b4579b4f3e0466220c02d09407a"
# Identidade na barra de tarefas: sem ela o Windows agrupa o app sob o pythonw.exe, com o ícone do Python.
APP_USER_MODEL_ID = "AgentLauncher.App"


def data_home() -> Path:
    """~/.agent-launcher, ou AGENT_LAUNCHER_HOME para isolar testes e perfis."""
    return Path(os.environ.get("AGENT_LAUNCHER_HOME") or Path.home() / ".agent-launcher").expanduser()
