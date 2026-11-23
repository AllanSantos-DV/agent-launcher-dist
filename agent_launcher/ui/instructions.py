"""Instruções e comandos dentro do próprio app: o texto de uso do pacote (instrucoes.md) renderizado numa janela,
com os comandos copiáveis, e a mesma janela para as novidades da versão (novidades.md, que o pacote leva). Nada aqui
abre arquivo, editor, navegador ou console."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout

from agent_launcher.domain.errors import LauncherError
from agent_launcher.ui.theme import bind_style, label
from version import VERSION

SOURCE = Path(__file__).with_name("instrucoes.md")
NOTES = Path(__file__).with_name("novidades.md")
FENCE = re.compile(r"^```(?P<language>[^\n]*)\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)
SHELL = {"", "powershell", "pwsh", "cmd", "bash", "sh", "shell", "console"}  # blocos que se colam num terminal


def read_markdown(source: Path, what: str) -> str:
    """O texto de `source`; LauncherError nomeando `what` e o arquivo se não puder ser lido."""
    try:
        return source.read_text(encoding="utf-8")
    except OSError as error:
        raise LauncherError(f"Não foi possível ler {what} ({source}): {error}") from error


def release_notes_available() -> bool:
    """A instalação trouxe a nota da versão? O checkout de desenvolvimento não traz: só o pacote a leva."""
    return NOTES.is_file()


def instructions_markdown() -> str:
    """O texto de uso do pacote (instrucoes.md); LauncherError se não puder ser lido."""
    return read_markdown(SOURCE, "as instruções")


def release_notes_markdown() -> str:
    """A nota da versão instalada (a do RELEASE dela, que o pacote leva); LauncherError se a instalação não a trouxe."""
    return read_markdown(NOTES, "as novidades da versão")


def commands(markdown: str) -> str:
    """Os blocos de comando do texto (shell; exemplos em Python ou JSON ficam de fora), separados por linha em branco."""
    return "\n\n".join(found["body"].strip("\n") for found in FENCE.finditer(markdown)
                       if found["language"].strip().lower() in SHELL)


class InstructionsDialog(QDialog):
    """Janela do texto de uso; com `notes`, a mesma janela mostra a nota da versão instalada ("O que mudou")."""

    def __init__(self, parent=None, notes: bool = False):
        super().__init__(parent)
        title = f"O que mudou na versão {VERSION}" if notes else "Instruções e comandos"
        self.setWindowTitle(title)
        self.resize(820, 640)
        layout = QVBoxLayout(self)
        self.error = label("", "notice", wrap=True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error.hide()
        layout.addWidget(self.error)
        self.view = QTextBrowser()
        self.view.setAccessibleName(title)
        # cores explícitas: sem elas o texto segue a paleta do Windows (fundo escuro com a tinta escura do app)
        bind_style(self.view, "QTextBrowser { background: #ffffff; color: #233e32; border: 1px solid #d5ded1; "
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
            self.markdown = release_notes_markdown() if notes else instructions_markdown()
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
