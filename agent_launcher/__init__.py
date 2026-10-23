"""Agent Launcher: o produto em pacotes.

domain/      cadastro (projetos, conexões, preferências, backup), cofre, catálogo, erros
connectors/  como falar com cada host de modelos; declaram protocolos e a conexão pronta (gateway.py)
harnesses/   destinos plugáveis: Claude Code, Codex, Copilot CLI, VS Code, Copilot app
session/     plano de sessão, janela da sessão, sessão controlada, pedido de fechamento
platform/    Windows: foco e janelas, teclas, Windows Terminal, elevação, DPAPI, Job Objects
endpoint/    endpoint local da conexão em 127.0.0.1
ui/          interface gráfica (PySide6) e modo texto
update/      atualização automática pelo npm

Os arquivos da raiz (core.py, desktop.py, launcher.py, focus.py, keys.py...) são fachadas: o orquestrador, o
launcher.cmd, os atalhos e as instalações 1.0.x continuam chamando os mesmos nomes.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # a pasta instalada (onde ficam launcher.py, desktop.py, assets/)
