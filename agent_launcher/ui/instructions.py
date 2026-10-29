"""Instruções e comandos dentro do próprio app: o README.md do pacote (fonte única do conteúdo) renderizado numa janela,
com os comandos copiáveis. Nada aqui abre arquivo, editor, navegador ou console."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from agent_launcher import ROOT
from agent_launcher.domain.errors import LauncherError

SOURCE = ROOT / "README.md"
FENCE = re.compile(r"^```(?P<language>[^\n]*)\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)
SHELL = {"", "powershell", "pwsh", "cmd", "bash", "sh", "shell", "console"}  # blocos que se colam num terminal


def instructions_markdown() -> str:
    try:
        return SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        raise LauncherError(f"Não foi possível ler as instruções ({SOURCE}): {error}") from error


def commands(markdown: str) -> str:
    """Os blocos de comando do texto (shell; exemplos em Python ou JSON ficam de fora), separados por linha em branco."""
    return "\n\n".join(found["body"].strip("\n") for found in FENCE.finditer(markdown)
                       if found["language"].strip().lower() in SHELL)


class InstructionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Instruções e comandos")
        self.resize(820, 640)
        layout = QVBoxLayout(self)
        self.error = QLabel("")
        self.error.setObjectName("notice")
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error.hide()
        layout.addWidget(self.error)
        self.view = QTextBrowser()
        self.view.setAccessibleName("Instruções e comandos")
        # cores explícitas: sem elas o texto segue a paleta do Windows (fundo escuro com a tinta escura do app)
        self.view.setStyleSheet("QTextBrowser { background: #ffffff; color: #233e32; border: 1px solid #d5ded1; "
                                "border-radius: 8px; padding: 6px; selection-background-color: #d5e6cf; }")
        self.view.setOpenLinks(False)  # só âncoras internas; endereço externo não abre navegador daqui
        self.view.anchorClicked.connect(self.follow)
        layout.addWidget(self.view, 1)
        actions = QHBoxLayout()
        self.copy_commands = QPushButton("Copiar comandos")
        self.copy_commands.clicked.connect(self.copy_all_commands)
        actions.addWidget(self.copy_commands)
        self.copy_selection = QPushButton("Copiar seleção")
        self.copy_selection.clicked.connect(self.view.copy)
        actions.addWidget(self.copy_selection)
        actions.addStretch()
        close = QPushButton("Fechar")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.markdown = ""
        try:
            self.markdown = instructions_markdown()
        except LauncherError as error:
            self.error.setText(str(error))
            self.error.show()
            self.copy_commands.setEnabled(False)
        else:
            self.view.setMarkdown(self.markdown)

    def follow(self, url: QUrl) -> None:
        if url.scheme() == "" and url.fragment():
            self.view.scrollToAnchor(url.fragment())

    def copy_all_commands(self) -> None:
        QApplication.clipboard().setText(commands(self.markdown))
