from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget,
    QSystemTrayIcon, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from agent_launcher import ROOT
from agent_launcher.domain import cache, model_probe, profiles, usage
from agent_launcher.domain.usage import USAGE_HEADER, usage_rows, usage_text
from agent_launcher.domain.conversation_usage import resume_window_warning, window_warning
from agent_launcher.domain.catalog import base_model, media_states, price_label, token_label
from agent_launcher.domain.errors import LauncherBusy, LauncherError
from agent_launcher.domain.files import append_capped, atomic_text, project_path
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import cli_update
from agent_launcher.harnesses import installers as clis
from agent_launcher.harnesses import registry, vscode
from agent_launcher.platform import keys, terminal
from agent_launcher.session import reopen
from agent_launcher.session.conversations import conversations_for, has_conversation_for
from agent_launcher.session.preview import (REMINDER, new_session_command, plan_report, plan_text, planned,
                                            resume_command)
from agent_launcher.session.window import session_title, start_session
from agent_launcher.ui import placement
from agent_launcher.ui import theme as theme_module
from agent_launcher.ui.background import BackgroundTask, ProgressSignals, deliver, run_task
from agent_launcher.ui.connections import ConnectionsDialog, quota_warning, split_cleanup
from agent_launcher.ui.instructions import InstructionsDialog, release_notes_available
from agent_launcher.ui.text import conversation_usages, label_of
from agent_launcher.ui.theme import (
    ACCENT_NAMES, DANGER, GREEN, INFO, MENU_SELECTED, MUTED, THEME_NAMES, WARN, CardGrid, CliButton, DangerMenu, ElidedLabel,
    FlowRow, InfoStrip, NoticeStrip, ReadableCombo, RowsTable, StatusLine, bind_icon, bind_style, icon, label, plural, tone, tone_item,
)
from agent_launcher.update import updater
from version import APP_NAME, APP_USER_MODEL_ID, VERSION


STARTUP_LOG = "abertura.log"
RELEASE_SEEN = "novidades-vistas.txt"
STARTUP_LOG_BYTES = 64 * 1024
_located: dict[str, tuple[tuple, str | None]] = {}


def process_started() -> float:
    """Momento (epoch) em que o Windows criou este processo: a abertura conta do clique no atalho, não do import."""
    import ctypes
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), *(ctypes.byref(item) for item in times)):
        raise OSError(f"GetProcessTimes falhou ({ctypes.GetLastError()})")
    return ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 1e7 - 11644473600


PROCESS_STARTED = process_started()
GUI_IMPORTED = time.time()


def installed_path(cli: str) -> str | None:
    """Executável do destino (CLIs de terminal pelo módulo de instalação, que os testes substituem), resolvido uma vez
    por PATH: cada busca confere todas as pastas do PATH. O caminho guardado que sumiu do disco é procurado de novo."""
    key = (os.environ.get("PATH", ""), clis.locate)
    cached = _located.get(cli)
    if cached and cached[0] == key and (cached[1] is None or os.path.isfile(cached[1])):
        return cached[1]
    found = clis.locate(cli)
    _located[cli] = (key, found)
    return found


def forget_installed() -> None:
    """Esquece os caminhos guardados: depois de instalar ou atualizar, e ao voltar ao launcher (pode ter mudado por fora)."""
    _located.clear()


def model_name(identifier: str) -> str:
    match = re.search(r"(?:^|[./])(claude-|gpt-|o[134](?:-|$)|codex)", identifier)
    tail = identifier[match.start():].lstrip("./") if match else identifier.rsplit("/", 1)[-1]
    if tail.startswith("claude-"):
        tail = re.sub(r"(?<=\d)-(?=\d)", ".", tail[7:])
        return "Claude " + tail.replace("-", " ").title()
    if tail.startswith("gpt-"):
        return "GPT-" + tail[4:].replace("-", " ").title()
    return tail


def provider_name(provider: str) -> str:
    return {"bedrock.anthropic": "Bedrock", "vertex_ai.anthropic": "Vertex"}.get(provider, provider or "Direto")


MEDIA_COLUMNS = (("vision", "Visão", "vision"), ("audio_input", "Áudio", "audio"), ("video_input", "Vídeo", "video"))
UNKNOWN_MEDIA = "o conector não informou"
SOURCE_COLUMN_WIDTH = 136
ALL_SOURCES = "Todas as fontes"
MEDIA_FILTERS = (("Todas as entradas", None),) + tuple((f"Com {title.lower()}", field) for field, title, _symbol in MEDIA_COLUMNS)


def media_cell(title: str, state: bool | None) -> tuple[str, str]:
    """Texto e dica da célula de uma entrada de mídia na lista de modelos: sem texto (só o ícone) quando o conector
    informou, traço quando não informou."""
    if state is None:
        return "—", f"{title}: {UNKNOWN_MEDIA}"
    return "", f"{title}: {'aceita' if state else 'não aceita'} (informado pelo conector)"


def media_icon(symbol: str, state: bool) -> QIcon:
    """Ícone de uma entrada de mídia da lista de modelos: cheio quando o conector diz que aceita, riscado quando diz que não."""
    return icon(symbol, GREEN, fill=MENU_SELECTED) if state else icon(symbol, MUTED, struck=True)


def project_icon(item: QListWidgetItem, selected: bool) -> QIcon:
    """Ícone do projeto na lista (pasta, ou alfinete no fixado): verde no escolhido, claro sobre o trilho nos outros.
    O fixado é marcado pelo ícone e não por texto, para a marca de sessão aberta continuar cabendo na lista."""
    return icon("pin" if item.data(PROJECT_PINNED) else "folder", GREEN if selected else "#c8d9c7")


def breakable_path(path: str) -> str:
    """Texto que quebra linha depois das barras de um caminho (espaço de largura zero), sem mudar o que se lê."""
    return re.sub(r"([\\/])", "\\1\u200b", path)


def catalog_tag(status: str) -> tuple[str, bool]:
    """Rótulo curto do estado do catálogo, ao lado do título dos modelos (o texto inteiro vai na dica); True = atenção."""
    if status.startswith("AVISO:"):
        return ("Cache desatualizado" if "desatualizado" in status else "Catálogo sem confirmação"), True
    parts = status.split(";")[0].rstrip(".").split(" • ")
    text = " · ".join(parts[:2] if status.startswith("Cache local") else parts[:1])
    return text, "indisponível" in text


def button(text: str, symbol: str = "", role: str = "", parent=None) -> QPushButton:
    widget = QPushButton(text, parent)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if symbol:
        bind_icon(widget, symbol, "#ffffff" if role == "primary" else GREEN)
    if role:
        widget.setObjectName(role)
    return widget


class ProjectDialog(QDialog):
    """Edita nome e pasta de um projeto cadastrado (a pasta em si nunca é movida ou alterada)."""

    def __init__(self, project: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Editar projeto")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name = QLineEdit(project["name"])
        self.name.setAccessibleName("Nome do projeto")
        form.addRow("Nome", self.name)
        folder_row = QHBoxLayout()
        self.path = QLineEdit(project["path"])
        self.path.setAccessibleName("Pasta do projeto")
        folder_row.addWidget(self.path, 1)
        choose = QPushButton("Escolher…")
        choose.clicked.connect(self.choose_folder)
        folder_row.addWidget(choose)
        form.addRow("Pasta", folder_row)
        layout.addLayout(form)
        layout.addWidget(label("Só o cadastro muda: a pasta, os arquivos e as sessões não são alterados.", "muted"))
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("Salvar")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        actions.addWidget(save)
        layout.addLayout(actions)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Escolha a pasta do projeto", self.path.text())
        if folder:
            self.path.setText(str(Path(folder)))


class ThemeDialog(QDialog):
    """Tema e cor de destaque da interface; quem abre grava a escolha ao aceitar."""

    def __init__(self, theme: str, accent: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tema da interface")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.theme = self.picker("Tema", THEME_NAMES, theme)
        form.addRow("Tema", self.theme)
        self.accent = self.picker("Cor de destaque", ACCENT_NAMES, accent)
        form.addRow("Cor de destaque", self.accent)
        layout.addLayout(form)
        layout.addWidget(label("A cor do perfil, das abas do Terminal e das ferramentas não muda com o tema.", "muted"))
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("Salvar")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        actions.addWidget(save)
        layout.addLayout(actions)

    @staticmethod
    def picker(name: str, options: dict, chosen: str) -> ReadableCombo:
        combo = ReadableCombo()
        combo.setAccessibleName(name)
        for value, title in options.items():
            combo.addItem(title, value)
        combo.setCurrentIndex(max(0, combo.findData(chosen)))
        return combo

    def chosen(self) -> tuple[str, str]:
        """(tema, cor de destaque) escolhidos, nos valores que o Store grava."""
        return self.theme.currentData(), self.accent.currentData()


class TextDialog(QDialog):
    """Texto selecionável só para leitura, com o botão que o copia inteiro (como o Copiar erro da faixa de avisos)."""

    def __init__(self, title: str, text: str, copy_label: str, copied_message: str, parent=None, on_copy=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 520)
        self.copied_message = copied_message
        layout = QVBoxLayout(self)
        self.box = QPlainTextEdit(text)
        self.box.setReadOnly(True)
        self.box.setAccessibleName(title)
        # cores explícitas: sem elas o texto segue a paleta do Windows (fundo escuro com a tinta escura do app)
        bind_style(self.box, "QPlainTextEdit { background: #ffffff; color: #233e32; border: 1px solid #d5ded1; "
                             "border-radius: 8px; padding: 6px; selection-background-color: #d5e6cf; }")
        layout.addWidget(self.box, 1)
        self.actions = QHBoxLayout()
        self.copy_button = QPushButton(copy_label)
        self.copy_button.clicked.connect(lambda: self.copy(on_copy))
        self.actions.addWidget(self.copy_button)
        self.copied = QLabel("")
        self.copied.setObjectName("muted")
        self.actions.addWidget(self.copied)
        self.actions.addStretch()
        close = QPushButton("Fechar")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        self.actions.addWidget(close)
        layout.addLayout(self.actions)

    def copy(self, on_copy=None) -> None:
        QApplication.clipboard().setText(self.box.toPlainText())
        self.copied.setText(self.copied_message)
        if on_copy:
            on_copy()


class DiagnosticsDialog(TextDialog):
    """O diagnóstico como texto selecionável, com o botão que o copia inteiro."""

    def __init__(self, text: str, parent=None, on_copy=None):
        super().__init__("Diagnóstico", text, "Copiar diagnóstico", "Diagnóstico copiado.", parent, on_copy)


class PlanDialog(TextDialog):
    """O que a próxima abertura vai iniciar (comando, conexão, modelo, ambiente e avisos), só para leitura. Com
    `command_for(shell)`, também copia o comando que abre a sessão fora do launcher, para cmd ou PowerShell."""

    def __init__(self, text: str, parent=None, command_for=None):
        super().__init__("O que vai abrir", text, "Copiar resumo", "Resumo copiado.", parent)
        self.command_for = command_for
        self.copied.setWordWrap(True)
        self.command_button = QPushButton("Copiar comando")
        self.command_button.setToolTip("Copia o comando que abre esta sessão num terminal seu, com a pasta, o ambiente e "
                                       "o modelo desta conexão. A chave não vai junto.")
        self.command_button.clicked.connect(lambda: self.copy_command("cmd"))
        self.powershell_button = QPushButton("Copiar para PowerShell")
        self.powershell_button.setToolTip("O mesmo comando, escrito para colar no PowerShell (5.1 ou 7). A chave não vai "
                                          "junto.")
        self.powershell_button.clicked.connect(lambda: self.copy_command("powershell"))
        copies = QHBoxLayout()
        for button in (self.command_button, self.powershell_button):
            button.setEnabled(command_for is not None)
            copies.addWidget(button)
        copies.addStretch()
        self.layout().insertLayout(1, copies)

    def copy_command(self, shell: str) -> None:
        """Copia o comando da nova sessão escrito para o `shell`; sem montá-lo, mostra o motivo e não toca na área de
        transferência."""
        copy_command_text(lambda: self.command_for(shell), shell, self.copied, "Comando")


def copy_command_text(build, shell: str, status: QLabel, noun: str) -> None:
    """Copia o comando que `build()` monta para o `shell` e o diz em `status` ("<noun> copiado"); sem montá-lo, mostra o
    motivo e não toca na área de transferência."""
    try:
        text = build()
    except (LauncherError, OSError, ValueError) as error:
        status.setText(f"Não foi possível montar o comando: {error}")
        return
    QApplication.clipboard().setText(text)
    form = " (PowerShell)" if shell == "powershell" else ""
    swap = f"; troque {REMINDER} pela sua" if REMINDER in text else ""
    status.setText(f"{noun} copiado{form}{swap}.")


class ScreenTask(BackgroundTask):
    """Lê a tela do console de uma sessão aberta fora da thread da interface (o auxiliar pode levar alguns segundos)."""

    def __init__(self, pid: int):
        super().__init__()
        self.pid = pid

    def outcome(self):
        return keys.read_screen(self.pid), ""


class SendKeysTask(BackgroundTask):
    """Envia teclas a uma sessão aberta fora da thread da interface (um texto grande leva segundos). Antes confere que a
    sessão do pid segue aberta."""

    def __init__(self, pid: int, tokens: list[str]):
        super().__init__()
        self.pid = pid
        self.tokens = tokens

    def outcome(self):
        keys.resolve(set(), pid=self.pid)
        keys.send(self.pid, self.tokens)
        return None, ""


def split_keys(text: str) -> list[str]:
    """As fichas do `launcher keys` digitadas num campo: separadas por espaço, aspas agrupam (\"text:olá mundo\") e a barra
    invertida é literal. Aspas sem fechar levantam ValueError."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split, lexer.escape, lexer.commenters = True, "", ""
    return list(lexer)


def session_pid(session: dict) -> int | None:
    """O pid do registro da sessão; None quando falta ou não é número."""
    try:
        return int(session["pid"])
    except (KeyError, TypeError, ValueError):
        return None


class SessionScreenDialog(TextDialog):
    """O que uma sessão aberta exibe agora. A tela é só leitura e nada vai à sessão até o clique em Enviar, que manda as
    fichas do `launcher keys` digitadas no campo à sessão que está sendo lida (pelo caminho de colar, sem foco e sem a
    área de transferência); a janela dela não vem para a frente."""

    READ_ONLY_HINT = "A tela é só leitura: nada é enviado à sessão até você clicar em Enviar."

    def __init__(self, sessions: list[dict], parent=None):
        super().__init__("Tela da sessão", "", "Copiar tela", "Tela copiada.", parent)
        self.sessions = list(sessions)
        self.sending = False
        self.box.setToolTip(self.READ_ONLY_HINT)
        self.hint = label(self.READ_ONLY_HINT, "muted")
        self.hint.setWordWrap(True)
        self.layout().insertWidget(0, self.hint)
        self.choice = ReadableCombo()
        self.choice.setAccessibleName("Sessão aberta")
        for session in self.sessions:
            self.choice.addItem(f"{label_of(session.get('cli', ''))} — {session.get('title') or session.get('project', '')}",
                                session_pid(session))
        self.layout().insertWidget(1, self.choice)
        self.error = label("", "notice")
        self.error.setWordWrap(True)
        self.error.hide()
        self.layout().insertWidget(2, self.error)
        self.target_label = label("", "muted")
        self.target_label.setWordWrap(True)
        self.keys_input = QLineEdit()
        self.keys_input.setPlaceholderText('Ex.: enter · esc · 1 · down · "text:resposta"')
        self.keys_input.setAccessibleName("Teclas a enviar")
        self.send_button = QPushButton("Enviar")
        self.send_button.setAccessibleName("Enviar à sessão")
        self.send_message = label("", "muted")
        self.send_message.setWordWrap(True)
        self.send_error = label("", "notice")
        self.send_error.setWordWrap(True)
        self.send_error.hide()
        keys_row = QHBoxLayout()
        keys_row.addWidget(label("Teclas a enviar"))
        keys_row.addWidget(self.keys_input, 1)
        keys_row.addWidget(self.send_button)
        self.layout().insertWidget(4, self.target_label)
        self.layout().insertLayout(5, keys_row)
        self.layout().insertWidget(6, self.send_message)
        self.layout().insertWidget(7, self.send_error)
        self.refresh_button = QPushButton("Atualizar")
        self.refresh_button.clicked.connect(self.refresh)
        self.actions.insertWidget(1, self.refresh_button)
        for each in self.findChildren(QPushButton):
            each.setAutoDefault(False)  # Enter no campo não aciona botão nenhum: só o clique envia
        self.choice.currentIndexChanged.connect(self.refresh)
        self.choice.currentIndexChanged.connect(self.update_send)
        self.keys_input.textChanged.connect(self.update_send)
        self.send_button.clicked.connect(self.send_typed_keys)
        self.task = None
        self.update_send()
        self.refresh()

    def refresh(self) -> None:
        """Lê a tela da sessão escolhida; a leitura de uma escolha anterior que chega depois é descartada."""
        pid = self.choice.currentData()
        if pid is None:
            return
        self.error.hide()
        self.copied.setText("Lendo a tela…")
        task = self.task = ScreenTask(pid)
        run_task(task, lambda result, text="": task is self.task and self.show_screen(result))

    def show_screen(self, result) -> None:
        self.copied.setText("")
        if isinstance(result, Exception):
            self.error.setText(f"Não foi possível ler a tela da sessão: {result}")
            self.error.show()
            return
        self.box.setPlainText(result)
        self.box.verticalScrollBar().setValue(self.box.verticalScrollBar().maximum())

    def chosen_session(self) -> dict | None:
        index = self.choice.currentIndex()
        return self.sessions[index] if 0 <= index < len(self.sessions) else None

    def target_text(self) -> str:
        session = self.chosen_session() or {}
        pid = self.choice.currentData()
        return (f"{label_of(session.get('cli', ''))} · {session.get('project', '')} · {session.get('title', '')}"
                + (f" · pid {pid}" if pid is not None else ""))

    def typed_keys(self) -> tuple[list[str], str]:
        """(fichas, problema): as fichas do campo já validadas por keys.parse; problema vazio quando valem."""
        text = self.keys_input.text().strip()
        if not text:
            return [], ""
        try:
            tokens = split_keys(text)
            keys.parse(tokens)
        except ValueError:
            return [], "Aspas sem fechar no campo de teclas."
        except keys.KeysError as error:
            return [], str(error)
        return tokens, ""

    def update_send(self) -> None:
        """Mostra a sessão que receberia, o que seria enviado ou o motivo de não poder, e habilita o Enviar só quando há
        sessão com pid, fichas válidas e nenhum envio em andamento."""
        session = self.chosen_session()
        pid = self.choice.currentData()
        reason = ("Sem sessão para enviar." if session is None else
                  "Esta sessão não tem pid registrado: não há como enviar a ela." if pid is None else
                  "")
        tokens, problem = ([], "") if reason else self.typed_keys()
        self.target_label.setText(f"Sessão que receberá as teclas: {self.target_text()}")
        self.send_error.setVisible(bool(problem))
        self.send_error.setText(problem)
        self.send_message.setText(reason or ("" if problem or not tokens else f"Será enviado: {self.keys_input.text().strip()}"))
        self.send_button.setEnabled(bool(tokens) and not self.sending)

    def send_typed_keys(self) -> None:
        """Clique em Enviar: as fichas do campo vão, uma vez, à sessão que está sendo lida."""
        tokens, problem = self.typed_keys()
        pid = self.choice.currentData()
        if self.sending or not tokens or problem or not self.send_button.isEnabled():
            return
        self.sending = True
        typed = self.keys_input.text().strip()
        for widget in (self.choice, self.keys_input):
            widget.setEnabled(False)
        self.send_button.setEnabled(False)
        self.send_message.setText("Enviando…")
        task = SendKeysTask(pid, tokens)
        self.send_task = task
        run_task(task, lambda result, text="": self.finish_send(result, typed))

    def finish_send(self, result, typed: str) -> None:
        """Depois do envio, relê a tela; se falhou, mostra o erro como veio e mantém as fichas para tentar de novo."""
        self.sending = False
        for widget in (self.choice, self.keys_input):
            widget.setEnabled(True)
        if isinstance(result, Exception):
            self.update_send()
            self.send_error.setText(str(result) if isinstance(result, keys.KeysError) else f"{type(result).__name__}: {result}")
            self.send_error.show()
            return
        self.keys_input.clear()
        self.send_message.setText(f"Enviado: {typed}")
        self.refresh()


def conversation_usage_cells(result, record, verified: bool) -> tuple[str, str]:
    """Contexto (com a parte da janela do modelo escolhido, quando confirmada) e custo estimado; traço = desconhecido."""
    if result is None:
        return "—", "—"
    window = record.max_input_tokens if record and verified else None
    context = "—" if result.context is None else (f"{round(result.context / 1000)}k" if result.context >= 1000 else str(result.context))
    if result.context and window:
        context += f" ({round(result.context * 100 / window)}%)"
    cost = "—" if result.cost is None else f"US$ {result.cost:.2f}".replace(".", ",")
    return context, cost


CONVERSATIONS_HEADER = ("Data", "CLI", "Título", "Mensagens", "Contexto", "Custo (est.)")


def conversations_text(rows: list[list[str]]) -> str:
    """A lista de conversas separada por tabulação: colada numa planilha, cada valor cai na sua coluna."""
    return "\n".join("\t".join(row) for row in [list(CONVERSATIONS_HEADER), *rows])


class ConversationsDialog(placement.GrowingDialog):
    """Conversas do CLI nesta pasta (as deste perfil, se a pasta é compartilhada): filtro por texto, cópia da lista
    como está filtrada, cópia do comando que retoma a conversa selecionada fora do launcher, para cmd ou para PowerShell
    (command_for: ID da conversa e shell -> texto), duplo clique ou Retomar. A coluna Título abre com largura para
    TITLE_CHARS caracteres e a mensagem de cópia tem altura reservada: ela não encolhe a tabela, a janela cresce."""

    TITLE_COLUMN = 2
    TITLE_CHARS = 40

    def __init__(self, conversations: list, parent=None, cli_label: str = "", usages: dict | None = None,
                 record=None, verified: bool = False, command_for=None):
        super().__init__(parent, QSize(720, 420))
        from datetime import datetime
        self.setWindowTitle(f"Conversas do {cli_label}" if cli_label else "Escolher conversa")
        layout = self.body_layout
        layout.addWidget(label("Escolha a conversa a retomar (mais recente primeiro).", "muted"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrar por título, data ou CLI")
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filtrar conversas")
        self.filter.textChanged.connect(self.apply_filter)
        layout.addWidget(self.filter)
        usages = usages or {}
        self.table = RowsTable(len(conversations), len(CONVERSATIONS_HEADER))
        self.table.setHorizontalHeaderLabels(list(CONVERSATIONS_HEADER))
        self.table.setAccessibleName("Conversas")
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, conversation in enumerate(conversations):
            values = (datetime.fromtimestamp(conversation.updated).strftime("%d/%m/%Y %H:%M"),
                      registry.get(conversation.cli).LABEL if registry.find(conversation.cli) else conversation.cli,
                      conversation.title or "(sem título)",
                      "" if conversation.messages is None else str(conversation.messages),
                      *conversation_usage_cells(usages.get(conversation.id), record, verified))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, conversation.id)
                if column == 2:
                    item.setToolTip(f"{conversation.title}\nID: {conversation.id}")
                elif column == 4:
                    warning = window_warning(getattr(usages.get(conversation.id), "context", None), record, verified)
                    item.setToolTip(warning or "Tokens do último pedido da conversa: o que a retomada carrega.")
                    if warning:
                        tone_item(item, WARN)
                elif column == 5:
                    item.setToolTip("Estimativa pelo preço que o host informa para cada modelo usado; não é fatura. "
                                    "Traço: o host não informou o preço de algum modelo, ou o CLI não guarda o uso.")
                if column >= 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        if conversations:
            self.table.selectRow(0)
        self.table.cellDoubleClicked.connect(lambda *_args: self.accept())
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copiar lista")
        self.copy_button.setToolTip("Copia a lista como está filtrada, separada por tabulação, para colar numa planilha")
        self.copy_button.setEnabled(bool(conversations))
        self.copy_button.clicked.connect(self.copy)
        actions.addWidget(self.copy_button)
        actions.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.resume = QPushButton("Retomar")
        self.resume.setObjectName("primary")
        self.resume.setDefault(True)
        self.resume.setEnabled(bool(conversations))
        self.resume.clicked.connect(self.accept)
        actions.addWidget(self.resume)
        self.command_for = command_for
        self.command_button = QPushButton("Copiar comando de retomada")
        self.command_button.setToolTip("Copia o comando que continua a conversa selecionada num terminal seu, com a pasta, "
                                       "o ambiente e o modelo desta conexão. A chave não vai junto.")
        self.command_button.setEnabled(bool(conversations) and command_for is not None)
        self.command_button.clicked.connect(lambda: self.copy_command("cmd"))
        self.powershell_button = QPushButton("Copiar para PowerShell")
        self.powershell_button.setToolTip("O mesmo comando de retomada, escrito para colar no PowerShell (5.1 ou 7). "
                                          "A chave não vai junto.")
        self.powershell_button.setEnabled(bool(conversations) and command_for is not None)
        self.powershell_button.clicked.connect(lambda: self.copy_command("powershell"))
        self.copied = StatusLine(lines=2)
        copies = QHBoxLayout()
        copies.addWidget(self.command_button)
        copies.addWidget(self.powershell_button)
        copies.addStretch()
        self.footer_layout.addLayout(copies)
        self.footer_layout.addWidget(self.copied)
        self.footer_layout.addLayout(actions)

    def content_floor(self) -> QSize:
        """O piso de largura mais o que a tabela precisa para a coluna Título ter TITLE_CHARS caracteres ao lado das outras
        colunas (que se ajustam ao conteúdo), da barra de rolagem e das margens do diálogo."""
        table, header = self.table, self.table.horizontalHeader()
        others = sum(max(table.sizeHintForColumn(column), header.sectionSizeHint(column))
                     for column in range(table.columnCount()) if column != self.TITLE_COLUMN)
        title = self.TITLE_CHARS * table.fontMetrics().averageCharWidth()
        margins = self.MARGINS
        width = others + title + table.verticalScrollBar().sizeHint().width() + 2 * table.frameWidth()
        return self.floor.expandedTo(QSize(width + margins.left() + margins.right(), 0))

    def visible_rows(self) -> list[int]:
        return [row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]

    def apply_filter(self, text: str) -> None:
        """Esconde as conversas sem o texto na data, no CLI ou no título; a seleção vai para uma que continua à vista."""
        query = text.strip().casefold()
        for row in range(self.table.rowCount()):
            haystack = " ".join(self.table.item(row, column).text() for column in range(3)).casefold()
            self.table.setRowHidden(row, query not in haystack)
        shown = self.visible_rows()
        selected = self.table.selectionModel().selectedRows()
        if not shown:
            self.table.clearSelection()
        elif not selected or self.table.isRowHidden(selected[0].row()):
            self.table.selectRow(shown[0])
        self.resume.setEnabled(bool(shown))
        self.copy_button.setEnabled(bool(shown))
        self.command_button.setEnabled(bool(shown) and self.command_for is not None)
        self.powershell_button.setEnabled(bool(shown) and self.command_for is not None)
        self.copied.setText("")

    def copy_command(self, shell: str = "cmd") -> None:
        """Copia o comando de retomada da conversa selecionada, escrito para o `shell` (cmd ou powershell); sem
        montá-lo, mostra o motivo e não toca na área de transferência."""
        conversation = self.selected()
        if not conversation or self.command_for is None:
            return
        copy_command_text(lambda: self.command_for(conversation, shell), shell, self.copied, "Comando de retomada")

    def copy(self) -> None:
        rows = [[self.table.item(row, column).text() for column in range(self.table.columnCount())]
                for row in self.visible_rows()]
        QApplication.clipboard().setText(conversations_text(rows))
        self.copied.setText(f"{plural(len(rows), 'conversa copiada', 'conversas copiadas')}: cole numa planilha, "
                            "cada valor vai para a sua coluna.")

    def selected(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row(), 0) if rows else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class ReopenDialog(placement.GrowingDialog):
    """Sessões que estavam abertas (session/reopen.py), marcáveis. A linha sem conversa identificada vem desmarcada e
    desabilitada, com o motivo; `selected()` devolve só as marcadas e habilitadas."""

    COLUMNS = ("Projeto", "CLI", "Última abertura", "Conversa")
    REASON_COLUMN = 3

    def __init__(self, rows: list, parent=None):
        super().__init__(parent, QSize(640, 340))
        self.setWindowTitle("Reabrir sessões recentes")
        self.rows = rows
        self.body_layout.addWidget(label(
            f"Sessões abertas nas últimas {reopen.RECENT_HOURS} horas e que não estão abertas agora. Marque as que quer "
            "reabrir: cada uma retoma a própria conversa, como aba da mesma janela do Terminal, sem tirar o foco.", "muted"))
        self.table = RowsTable(len(rows), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setAccessibleName("Sessões recentes")
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.REASON_COLUMN, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for number, row in enumerate(rows):
            values = (row.name, reopen.cli_label(row.cli), datetime.fromtimestamp(row.at).strftime("%d/%m/%Y %H:%M"),
                      row.reason or f"retoma a conversa {row.conversation[:8]}")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(f"{row.folder}\nID da conversa: {row.conversation}" if row.conversation else row.folder)
                if row.selectable:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable if column == 0
                                  else Qt.ItemFlag.ItemIsEnabled)
                else:
                    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable if column == 0 else Qt.ItemFlag.NoItemFlags)
                if column == 0:
                    item.setCheckState(Qt.CheckState.Checked if row.selectable else Qt.CheckState.Unchecked)
                self.table.setItem(number, column, item)
        self.table.itemChanged.connect(lambda _item: self.update_button())
        self.body_layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.reopen_button = QPushButton("Reabrir marcadas")
        self.reopen_button.setObjectName("primary")
        self.reopen_button.setDefault(True)
        self.reopen_button.clicked.connect(self.accept)
        actions.addWidget(self.reopen_button)
        self.footer_layout.addLayout(actions)
        self.update_button()

    def selected(self) -> list:
        return [row for number, row in enumerate(self.rows)
                if self.table.item(number, 0).checkState() == Qt.CheckState.Checked
                and self.table.item(number, 0).flags() & Qt.ItemFlag.ItemIsEnabled]

    def update_button(self) -> None:
        marked = len(self.selected())
        self.reopen_button.setEnabled(bool(marked))
        self.reopen_button.setText(f"Reabrir {marked} marcada{'s' if marked != 1 else ''}" if marked else "Reabrir marcadas")


class ReopenTask(BackgroundTask):
    """Reabre as sessões marcadas, uma aba por vez (session/reopen.py), fora da thread da interface: cada aba espera a
    janela aparecer para devolver o foco a quem estava com ele."""

    def __init__(self, store: Store, rows: list):
        super().__init__()
        self.store = store
        self.rows = rows

    def outcome(self):
        return reopen.open_all(self.store, self.rows), ""


class InstallCliTask(BackgroundTask):
    """Instala a ferramenta fora da thread da interface (download de centenas de MB), relatando o progresso."""

    signals_class = ProgressSignals

    def __init__(self, cli: str):
        super().__init__()
        self.cli = cli
        self.lines = []

    def outcome(self):
        found = clis.ensure(self.cli, log=self.lines.append, progress=lambda fraction, text: deliver(self.signals, "progress", fraction, text))
        return found, "\n".join(self.lines)

    def failure(self, error):
        return None, "\n".join(self.lines + [f"{type(error).__name__}: {error}"])


class TerminalTask(BackgroundTask):
    """Instala o Windows Terminal sem admin (ZIP portátil oficial, depois winget) fora da thread da interface."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.lines = []

    def outcome(self):
        found = terminal.ensure(self.root, log=self.lines.append)
        return found, "\n".join(self.lines)

    def failure(self, error):
        return None, "\n".join(self.lines + [str(error)])


class CliCheckTask(BackgroundTask):
    """Lê as versões instaladas (`--version`) e as publicadas no npm das CLIs, fora da thread da interface."""

    def outcome(self):
        return cli_update.check_all(), ""


class CliUpdateTask(BackgroundTask):
    """Atualiza uma CLI (o `update` dela, npm ou winget) fora da thread da interface; o resultado volta com o id da CLI."""

    def __init__(self, cli: str):
        super().__init__()
        self.cli = self.tag = cli

    def outcome(self):
        return cli_update.update(self.cli), self.cli


class TerminalUpdateTask(BackgroundTask):
    """Atualiza o Windows Terminal portátil (a cópia que o launcher instalou) fora da thread da interface."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def outcome(self):
        return terminal.update_portable(self.root) or "", ""


class UpdateTask(BackgroundTask):
    """Confere o engine-registry e, havendo versão nova, instala por cima (fora da thread da interface)."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def outcome(self):
        return updater.update(self.root), ""


class RefreshTask(BackgroundTask):
    def __init__(self, home: Path, connection: str, force: bool):
        super().__init__()
        self.home = home
        self.connection = self.tag = connection
        self.force = force

    def outcome(self):
        store = Store(self.home)
        gateway = Gateway(store, store.find_connection(self.connection))
        return gateway.catalog(refresh=self.force), self.connection


class CleanupTask(BackgroundTask):
    """Limpeza nos destinos das conexões de um perfil (harnesses/cleanup.py) fora da thread da interface. A
    confirmação já foi feita pela interface: aqui o confirm sempre aceita (nada de QMessageBox em outra thread)."""

    def __init__(self, home: Path, cleanup=None):
        super().__init__()
        self.home = home
        self.cleanup = cleanup

    def outcome(self):
        from agent_launcher.harnesses.cleanup import cleanup_profile
        return (self.cleanup or cleanup_profile)(self.home, lambda *_: True), ""

    def failure(self, error):
        from agent_launcher.harnesses.cleanup import failure
        return [failure("Limpeza nos destinos", error)], ""


class UsageTask(BackgroundTask):
    """Uso do mês do perfil (domain/usage.py) fora da thread da interface: pode ler muitos arquivos de conversa."""

    def __init__(self, home: Path, month: str | None = None, everyone: bool = False):
        super().__init__()
        self.home, self.month, self.everyone = home, month, everyone

    def outcome(self):
        report = usage.usage_report_all if self.everyone else usage.usage_report
        return report(self.home, self.month), ""


class SourceCostsTask(BackgroundTask):
    """Custo do mês por fonte (a rota `costs` do conector da conexão) fora da thread da interface: é uma chamada de rede."""

    def __init__(self, gateway: Gateway, month: str):
        super().__init__()
        self.gateway, self.month = gateway, month

    def outcome(self):
        return self.gateway.costs(self.month), ""


class ModelTestTask(BackgroundTask):
    """Chamada real de 1 token (domain/model_probe.py) fora da thread da interface: o host pode demorar a responder."""

    def __init__(self, home: Path, connection_id: str, model: str, protocol: str):
        super().__init__()
        self.home, self.connection_id, self.model, self.protocol = home, connection_id, model, protocol
        self.tag = model

    def outcome(self):
        return model_probe.probe(self.home, self.connection_id, self.model, self.protocol), self.model


MODEL_ORDERS = (("Ordenar: Nome", "name"), ("Ordenar: Contexto", "context"), ("Ordenar: Preço", "price"))
MODEL_ORDER_TIPS = {"name": "Família e versão mais nova primeiro",
                    "context": "Maior contexto de entrada primeiro; o que o conector não informou vai para o fim",
                    "price": "Menor preço de entrada primeiro; o que o conector não informou vai para o fim"}


def order_models(records: list, order: str) -> list:
    """Os modelos na ordem pedida: "name" (família e versão mais nova primeiro, a ordem de sempre), "context" (maior
    entrada primeiro) ou "price" (menor preço de entrada primeiro). Quem não tem o dado vai para o fim, na ordem do nome."""
    families = {"opus": 0, "sonnet": 1, "haiku": 2}
    ordered = sorted(records, key=lambda record: record.version, reverse=True)
    ordered.sort(key=lambda record: families.get(record.family, 3))
    values = {"context": lambda record: record.max_input_tokens, "price": lambda record: record.input_cost_per_token}
    if order not in values:
        return ordered
    known = [record for record in ordered if values[order](record) is not None]
    known.sort(key=values[order], reverse=order == "context")
    return known + [record for record in ordered if values[order](record) is None]


def models_text(records: list, catalog) -> str:
    """A lista de modelos separada por tabulação: colada numa planilha, cada valor cai na sua coluna. A coluna Fonte só
    entra quando o conector do catálogo declara fontes."""
    with_source = catalog.segregates_sources
    lines = ["\t".join(["Modelo", "Provider", *(["Fonte"] if with_source else []), "Entrada", "Saída",
                        "Preço (US$ por 1M, entrada/saída)", *(title for _, title, _ in MEDIA_COLUMNS)])]
    for record in records:
        states = ["—" if state is None else "sim" if state else "não" for _title, state in media_states(record)]
        output = "—" if record.max_output_tokens is None else token_label(record.max_output_tokens)
        lines.append("\t".join([record.id, provider_name(record.provider), *([record.source or ""] if with_source else []),
                                 token_label(record.max_input_tokens), output, price_label(record) or "—", *states]))
    return "\n".join(lines)


class UsageTable(RowsTable):
    """Tabela do uso do mês: o diálogo nunca a deixa com menos de quatro linhas à vista."""

    ROWS = 4


class UsageDialog(QDialog):
    from agent_launcher.domain.usage import NOTE

    SOURCE_COST_ROWS = 6

    limit_saved = Signal()

    def __init__(self, parent, home: Path, profile_name: str, gateway: Gateway | None = None):
        """`gateway`: a conexão selecionada; só com ela declarando fontes e oferecendo a rota de custo o diálogo ganha a
        seção "Custo por fonte" (`source_costs_title`, `source_costs_status`, `source_costs_table`); sem isso os três
        valem None e nada de custo por fonte aparece."""
        super().__init__(parent)
        self.setWindowTitle(f"Uso do mês — {profile_name}")
        self.home = home
        self.gateway = gateway if gateway is not None and gateway.offers_costs else None
        self.resize(760, 420 if self.gateway is None else 600)
        layout = QVBoxLayout(self)
        choice = QHBoxLayout()
        self.month_label = QLabel("Mês")
        choice.addWidget(self.month_label)
        self.month_choice = ReadableCombo()
        self.month_choice.setAccessibleName("Mês do uso")
        for month in usage.recent_months():
            self.month_choice.addItem(month, month)
        self.month_label.setBuddy(self.month_choice)
        choice.addWidget(self.month_choice)
        self.everyone_check = QCheckBox("Somar todos os perfis")
        self.everyone_check.setToolTip("Soma o uso de todos os perfis numa tabela só; a tabela de um perfil mostra só o dele")
        self.everyone_check.setVisible(len(profiles.list_profiles(profiles.base_of(home))) > 1)
        self.everyone_check.toggled.connect(self.calculate)
        choice.addWidget(self.everyone_check)
        self.status = QLabel("Calculando…")
        choice.addWidget(self.status, 1)
        layout.addLayout(choice)
        self.error = label("", "notice")  # erro no estilo de erro da janela principal, nunca na linha de status
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.table = UsageTable(0, len(USAGE_HEADER))
        self.table.setHorizontalHeaderLabels(list(USAGE_HEADER))
        for column in range(2, len(USAGE_HEADER)):
            self.table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for column in range(2):
            self.table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.source_costs_title = self.source_costs_status = self.source_costs_table = self.source_costs_task = None
        if self.gateway is not None:
            self.build_source_costs(layout)
        self.note = QLabel(self.NOTE)
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        limit_row = QHBoxLayout()
        self.limit_label = QLabel("Limite mensal (US$)")
        limit_row.addWidget(self.limit_label)
        self.limit_edit = QLineEdit()
        self.limit_edit.setAccessibleName("Limite mensal")
        self.limit_edit.setPlaceholderText("Sem limite")
        self.limit_edit.setToolTip("Teto de gasto do mês deste perfil. A janela avisa a 80% e a 100%; nada é bloqueado. "
                                   "Vazio desliga.")
        self.limit_edit.setFixedWidth(130)
        self.limit, self.limit_unavailable = None, ""
        try:
            self.limit = Store(home).monthly_limit()
        except (LauncherError, OSError, ValueError) as error:
            self.limit_unavailable = f"Limite mensal indisponível: {error}"
            self.limit_edit.setEnabled(False)
        self.limit_edit.setText("" if self.limit is None else usage.format_limit(self.limit))
        self.limit_edit.returnPressed.connect(self.save_limit)
        self.limit_label.setBuddy(self.limit_edit)
        limit_row.addWidget(self.limit_edit)
        self.limit_save = QPushButton("Salvar")
        self.limit_save.setEnabled(not self.limit_unavailable)
        self.limit_save.clicked.connect(self.save_limit)
        limit_row.addWidget(self.limit_save)
        self.limit_note = QLabel("")
        self.limit_note.setObjectName("muted")
        self.limit_note.setWordWrap(True)
        limit_row.addWidget(self.limit_note, 1)
        layout.addLayout(limit_row)
        self.limit_basis = None  # o último relatório calculado, a base do percentual do limite
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copiar relatório")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy)
        actions.addWidget(self.copy_button)
        self.copied = QLabel("")
        self.copied.setObjectName("muted")
        actions.addWidget(self.copied)
        actions.addStretch()
        close = QPushButton("Fechar")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.report = None
        self.month_choice.currentIndexChanged.connect(self.calculate)
        self.calculate()

    def build_source_costs(self, layout: QVBoxLayout) -> None:
        """A seção "Custo por fonte" da conexão: título, estado e a tabela de `usage.SOURCE_COST_HEADER`."""
        self.source_costs_title = label(f"Custo por fonte • {self.gateway.name}", "section")
        layout.addWidget(self.source_costs_title)
        self.source_costs_status = label("", "muted")
        self.source_costs_status.setWordWrap(True)
        self.source_costs_status.hide()
        layout.addWidget(self.source_costs_status)
        table = self.source_costs_table = QTableWidget(0, len(usage.SOURCE_COST_HEADER))
        table.setAccessibleName("Custo do mês por fonte")
        table.setHorizontalHeaderLabels(list(usage.SOURCE_COST_HEADER))
        for column in range(1, len(usage.SOURCE_COST_HEADER)):
            table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(table)
        self.fit_source_costs_height()

    def fit_source_costs_height(self) -> None:
        """A tabela de custo tem a altura do cabeçalho e das suas linhas (até `SOURCE_COST_ROWS`, depois rola): sem sobra
        abaixo do Total."""
        table = self.source_costs_table
        shown = min(table.rowCount(), self.SOURCE_COST_ROWS)
        table.setFixedHeight(table.horizontalHeader().sizeHint().height() + shown * table.verticalHeader().defaultSectionSize()
                             + 2 * table.frameWidth())

    def set_source_costs_status(self, text: str) -> None:
        self.source_costs_status.setText(text)
        self.source_costs_status.setVisible(bool(text))

    def load_source_costs(self) -> None:
        """Pede ao conector o custo por fonte do mês escolhido; a resposta de um mês anterior que chega depois é descartada."""
        month = self.month_choice.currentData()
        self.set_source_costs_status("Calculando…")
        self.source_costs_table.setRowCount(0)
        self.fit_source_costs_height()
        task = self.source_costs_task = SourceCostsTask(self.gateway, month)
        run_task(task, lambda result, text="": task is self.source_costs_task
                 and self.show_source_costs(result, month))

    def show_source_costs(self, result, month: str) -> None:
        if isinstance(result, Exception):
            self.set_source_costs_status(f"Não foi possível ler o custo por fonte: {result}")
            return
        rows = usage.source_costs_rows(result)
        self.set_source_costs_status("" if rows else f"O conector não informou custo por fonte em {month}.")
        table = self.source_costs_table
        table.setRowCount(len(rows))
        for number, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QTableWidgetItem(value)
                if column >= 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if number == len(rows) - 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                table.setItem(number, column, item)
        self.fit_source_costs_height()

    def calculate(self) -> None:
        """Calcula o mês escolhido; o resultado de uma escolha anterior que chega depois é descartado."""
        if self.gateway is not None:
            self.load_source_costs()
        self.status.setText("Calculando…")
        self.error.hide()
        self.report = self.limit_basis = None
        self.show_limit_note()
        self.copy_button.setEnabled(False)
        self.table.setRowCount(0)
        task = self.task = UsageTask(self.home, self.month_choice.currentData(), self.everyone_check.isChecked())
        run_task(task, lambda report, text="": task is self.task and self.show_report(report, text))

    def show_report(self, report, _text: str = ""):
        if isinstance(report, Exception):
            self.status.setText("")
            self.error.setText(f"Não foi possível calcular o uso: {report}")
            self.error.show()
            return
        self.error.hide()
        self.limit_basis = report
        self.show_limit_note()
        rows = usage_rows(report)
        self.note.setText(self.NOTE.replace("deste perfil", "de cada perfil") if report.get("profile") == "todos" else self.NOTE)
        self.report = report if rows else None
        self.copy_button.setEnabled(self.report is not None)
        self.status.setText("" if rows else f"Nenhum uso encontrado em {report['month']}.")
        self.table.setRowCount(len(rows))
        for number, row in enumerate(rows):
            total = number == len(rows) - 1
            for column, value in enumerate(row):
                item = QTableWidgetItem(value)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if total:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if column == 4 and (value == "sem preço" or value.endswith("(parcial)")):
                    tone_item(item, MUTED)
                self.table.setItem(number, column, item)

    def copy(self) -> None:
        QApplication.clipboard().setText(usage_text(self.report))
        self.copied.setText("Relatório copiado: cole numa planilha, cada valor vai para a sua coluna.")

    def show_limit_note(self, saved: bool = False) -> None:
        """O gasto do mês frente ao limite do perfil (ou o estado do limite, sem relatório ou na soma de perfis)."""
        report = self.limit_basis
        if self.limit_unavailable:
            text = self.limit_unavailable
        elif self.limit is None:
            text = "Limite desligado: nenhum aviso." if saved else "Sem limite mensal: a janela não avisa."
        elif report is not None and report.get("profile") == "todos":
            text = "O limite vale para cada perfil: a soma de todos não é comparada com ele."
        elif report is not None:
            text = f"Limite mensal: {usage.limit_text(usage.limit_status(report, self.limit))}."
        else:
            text = "Limite mensal salvo." if saved else ""
        self.limit_note.setText(text)

    def save_limit(self) -> None:
        """Grava o limite do campo (vazio desliga) no perfil e avisa a janela para refazer o aviso da faixa."""
        try:
            value = usage.parse_limit(self.limit_edit.text())
            Store(self.home).set_monthly_limit(value)
        except (LauncherError, OSError, ValueError) as error:
            self.error.setText(f"Não foi possível salvar o limite: {error}")
            self.error.show()
            return
        self.error.hide()
        self.limit = value
        self.limit_edit.setText("" if value is None else usage.format_limit(value))
        self.show_limit_note(saved=True)
        self.limit_saved.emit()


PROJECT_TEXT = Qt.ItemDataRole.UserRole + 1  # texto e dica do projeto sem a marca de sessão aberta
PROJECT_PINNED = Qt.ItemDataRole.UserRole + 2
SESSION_POLL_MS = 1500
TRAY_NOTICE_MS = 10_000 # o Windows mostra o balão por tempo próprio; o ícone fica este tempo para o balão não sumir com ele
GENERAL_ERROR = "geral"  # origem do erro avulso de uma ação; os erros persistentes têm origem própria
MODEL_TEST = "teste-modelo"  # origem do rodapé e do erro do teste de modelo com uma chamada real


class ModelTable(QTableWidget):
    """Lista de modelos, sem rolagem horizontal. Com largura para o conforto (colunas depois do nome na largura de
    sempre, nome inteiro) a coluna do nome ocupa o que sobra; na janela estreita as margens das células caem para a
    metade, os cabeçalhos de mídia viram só o ícone (o título fica na dica) e as colunas de texto dividem o que resta,
    com reticências e a dica com o texto inteiro."""

    NAME_MIN = 200
    NAME_PADDING = 24
    TIGHT = 16
    TIGHT_STYLE = "QHeaderView::section { padding: 10px 4px; } QTableWidget::item { padding: 0 4px; }"
    ICON = 16

    def __init__(self, rows: int, columns: int, parent=None):
        super().__init__(rows, columns, parent)
        self.roomy: dict[int, int] = {}
        self.text_columns: tuple[int, ...] = ()
        self.media: tuple[tuple[int, str, str], ...] = ()
        self.title_hints: dict[int, int] = {}
        self.compact = False
        self.fitting = False

    def configure(self, roomy: dict[int, int], text_columns: tuple[int, ...], media: tuple[tuple[int, str, str], ...]) -> None:
        """Recomeça no conforto, com os cabeçalhos em texto: `roomy` é a largura de sempre de cada coluna depois do nome,
        `text_columns` as que dividem o que sobra na janela estreita e `media` as colunas (índice, título, símbolo) cujo
        cabeçalho vira ícone."""
        self.roomy, self.text_columns, self.media = roomy, text_columns, media
        self.title_hints = {}
        self.compact = False
        self.setStyleSheet("")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_columns()

    def showEvent(self, event):
        super().showEvent(event)
        self.fit_columns()

    def set_compact(self, compact: bool) -> None:
        if compact == self.compact:
            return
        self.compact = compact
        self.setStyleSheet(self.TIGHT_STYLE if compact else "")
        for column, title, symbol in self.media:
            item = self.horizontalHeaderItem(column)
            item.setText("" if compact else title)
            item.setIcon(icon(symbol, MUTED, self.ICON) if compact else QIcon())
            item.setToolTip(title if compact else "")
            item.setData(Qt.ItemDataRole.AccessibleTextRole, title)

    def fit_columns(self) -> None:
        count = self.columnCount()
        if self.fitting or not count or not self.isVisible():
            return
        self.fitting = True
        try:
            if not self.roomy:
                self.roomy = {column: self.columnWidth(column) for column in range(1, count)}
            header = self.horizontalHeader()
            if not self.compact:
                self.title_hints = {column: header.sectionSizeHint(column) for column in range(count)}
            width = self.viewport().width()
            extra = self.TIGHT if self.compact else 0
            wants = {column: max(self.roomy.get(column, 0), self.title_hints.get(column, 0),
                                 self.sizeHintForColumn(column) + extra) for column in range(1, count)}
            whole_name = self.sizeHintForColumn(0) + extra + self.NAME_PADDING
            compact = width < sum(wants.values()) + max(whole_name, self.NAME_MIN)
            self.set_compact(compact)
            if compact:
                self.share_width(width)
            else:
                for column, wanted in wants.items():
                    self.setColumnWidth(column, wanted)
                self.setColumnWidth(0, width - sum(wants.values()))
        finally:
            self.fitting = False

    def share_width(self, width: int) -> None:
        """Janela estreita: as colunas de números e de mídia ficam do tamanho do cabeçalho ou do valor; as de texto
        repartem o resto, cada uma com pelo menos o cabeçalho e o excesso do que pede dividido na proporção do que pede."""
        header = self.horizontalHeader()
        elastic = (0, *self.text_columns)
        used = 0
        for column in range(1, self.columnCount()):
            if column not in elastic:
                size = max(header.sectionSizeHint(column), self.sizeHintForColumn(column))
                self.setColumnWidth(column, size)
                used += size
        floor = {column: header.sectionSizeHint(column) for column in elastic}
        asked = {column: max(floor[column], self.sizeHintForColumn(column)) for column in elastic}
        room = width - used
        spare = max(room - sum(floor.values()), 0)
        extra = sum(asked[column] - floor[column] for column in elastic)
        shares = {column: floor[column] + (spare * (asked[column] - floor[column]) // extra if extra else 0)
                  for column in elastic}
        if spare > extra:
            shares[0] += spare - extra
        shares[0] += room - sum(shares.values())
        for column, size in shares.items():
            self.setColumnWidth(column, size)


class LauncherWindow(QMainWindow):
    NARROW_WINDOW = 1000
    NARROW_SIDE = 20
    WIDE_SIDE = 32

    def __init__(self, store: Store | None = None, auto_refresh: bool = True):
        super().__init__()
        self.store = store or active_store()
        # Raiz dos perfis: a pasta-mãe quando o Store é de um perfil extra; a própria pasta senão (testes).
        self.profile_base = profiles.base_of(self.store.home)
        self.auto_refresh = auto_refresh
        self.startup_phases = {"import": GUI_IMPORTED - PROCESS_STARTED}
        self.startup_pending = {"clis"} if auto_refresh else set()
        self.startup_done = False
        self.gateway = None
        self.catalog = None
        self.project = None
        self.cli = "claude"
        self.records = []
        self.selected_model = None
        self.online = False
        self.refreshing = False
        self.sessions = []
        self.reopening = False
        self.terminal_installing = False
        self.terminal_updating = False
        self.cli_versions = {}
        self.cli_queue = []
        self.cli_checking = False
        self.cli_updating = None
        self.limit_checking = False
        self.source_limit_checking = False
        self.source_limit_task = None
        self.model_testing = False
        self.errors: dict[str, str] = {}  # origem -> erro ativo; cada um some só quando a sua origem se resolve
        self.warnings: dict[str, str] = {}  # origem -> aviso (algo não aconteceu, nada parou de funcionar); some como o erro
        self.infos: dict[str, str] = {}  # origem -> informativo; a mesma origem substitui o seu, origens diferentes convivem
        self.cli_failed = set()
        self.cli_announced = ()
        self.catalog_size = 0
        self.footer_lines: dict[str, str] = {}  # origem -> rodapé; o de uma operação em curso tem prioridade
        self.footer_busy: set[str] = set()
        self.update_status = "não verificada nesta execução"
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(ROOT / "assets" / "launcher.ico")))
        available = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(placement.initial_geometry(available, QSize(1220, 850)))
        self.setMinimumSize(placement.minimum_for(available))
        self._build()
        self.keeper = placement.WindowKeeper(self)
        self.populate_profiles()
        self.populate_projects(self.store.data["last_project"])
        self.populate_connections()
        self.tray = None
        self.tray_notified = False
        self.tray_timer = QTimer(self)
        self.tray_timer.setSingleShot(True)
        self.tray_timer.setInterval(TRAY_NOTICE_MS)
        self.tray_timer.timeout.connect(self.release_tray)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.check_sessions)
        self.watching_sessions = False
        self.session_watcher = QFileSystemWatcher(self)
        self.session_watcher.directoryChanged.connect(self.sessions_changed)
        self.watch_sessions()
        self.check_sessions()
        QApplication.instance().applicationStateChanged.connect(self.recheck_tools)
        self.shortcuts = []
        for sequence, callback in (("Ctrl+K", self.project_search.setFocus), ("Ctrl+F", self.model_search.setFocus),
                                   ("Ctrl+N", self.add_project), ("F5", lambda: self.refresh_catalog(True)),
                                   ("Ctrl+R", self.resume_by_shortcut), ("Ctrl+H", lambda: self.choose_conversation()),
                                   ("Ctrl+Shift+H", self.reopen_recent_sessions),
                                   ("Ctrl+U", lambda: self.show_usage()), ("Ctrl+Return", self.launch_by_shortcut),
                                   ("Ctrl+Enter", self.launch_by_shortcut)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self.startup_mark("janela")
        if self.auto_refresh:
            QTimer.singleShot(0, self.start_auto_update)
            # Janela aberta por dias também se atualiza (09/10: uma janela antiga seguia com o bug já corrigido).
            self.update_timer = QTimer(self)
            self.update_timer.timeout.connect(self.start_auto_update)
            self.update_timer.start(UPDATE_INTERVAL_MS)
            QTimer.singleShot(0, self.check_limit)
            self.limit_timer = QTimer(self)
            self.limit_timer.timeout.connect(self.check_limit)
            self.limit_timer.start(LIMIT_INTERVAL_MS)
            QTimer.singleShot(800, self.ensure_terminal)
            QTimer.singleShot(1500, self.show_release_notes_once)

    def resizeEvent(self, event):
        """Na janela estreita as margens laterais do conteúdo caem de 32 para 20 px: a tabela de modelos ganha 24 px."""
        super().resizeEvent(event)
        side = self.NARROW_SIDE if event.size().width() < self.NARROW_WINDOW else self.WIDE_SIDE
        for layout, top, bottom in self.side_margins:
            if layout.contentsMargins().left() != side:
                layout.setContentsMargins(side, top, side, bottom)

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(270)
        outer = QVBoxLayout(sidebar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.profile_strip = QFrame()  # faixa com a cor do perfil (some no padrão)
        self.profile_strip.setObjectName("profileStrip")
        self.profile_strip.setFixedHeight(5)
        outer.addWidget(self.profile_strip)
        rail_page = QWidget()
        rail_page.setObjectName("railPage")
        rail = QVBoxLayout(rail_page)
        rail.setContentsMargins(20, 21, 20, 20)
        rail.setSpacing(13)
        rail_scroll = QScrollArea()
        rail_scroll.setObjectName("rail")
        rail_scroll.setWidgetResizable(True)
        rail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail_scroll.setWidget(rail_page)
        outer.addWidget(rail_scroll, 1)
        brand = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(QIcon(str(ROOT / "assets/launcher.svg")).pixmap(38, 38))
        brand.addWidget(mark)
        brand.addWidget(label(APP_NAME, "brand"))
        brand.addStretch()
        brand.addWidget(label(VERSION, "railMuted"))
        rail.addLayout(brand)
        rail.addSpacing(6)
        rail.addWidget(label("PERFIL", "railMuted"))
        profile_row = QHBoxLayout()
        profile_row.setSpacing(6)
        self.profile_picker = ReadableCombo()
        self.profile_picker.setAccessibleName("Perfil de trabalho")
        self.profile_picker.setToolTip("Cada perfil tem os seus projetos e as suas conexões")
        self.profile_picker.activated.connect(self.pick_profile)
        profile_row.addWidget(self.profile_picker, 1)
        self.profile_options = QToolButton()
        self.profile_options.setObjectName("ghost")
        bind_icon(self.profile_options, "more", "#c8d9c7")
        self.profile_options.setToolTip("Novo perfil, renomear, remover, importar ou exportar")
        self.profile_options.setAccessibleName("Opções do perfil")
        self.profile_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        profile_menu = DangerMenu(self)
        profile_menu.addAction("Novo perfil…", self.new_profile)
        self.rename_profile_action = profile_menu.addAction("Renomear perfil…", self.rename_profile)
        self.color_profile_action = profile_menu.addAction("Cor do perfil…", self.color_profile)
        self.remove_profile_action = profile_menu.add_danger_action("Remover perfil…", self.remove_profile)
        profile_menu.addSeparator()
        self.import_profile_action = profile_menu.addAction("Importar perfil…", self.import_profile)
        self.export_profile_action = profile_menu.addAction("Exportar perfil…", self.export_profile)
        profile_menu.addSeparator()
        self.usage_action = profile_menu.addAction("Uso do mês…", self.show_usage)
        self.profile_options.setMenu(profile_menu)
        profile_row.addWidget(self.profile_options)
        rail.addLayout(profile_row)
        rail.addSpacing(8)
        project_heading = QHBoxLayout()
        project_heading.addWidget(label("Seus projetos", "section"))
        project_heading.addStretch()
        self.project_count = label("", "railMuted")
        project_heading.addWidget(self.project_count)
        rail.addLayout(project_heading)
        self.project_search = QLineEdit()
        self.project_search.setPlaceholderText("Buscar projetos")
        self.project_search.setAccessibleName("Buscar projetos")
        self.paint_search_placeholder()
        bind_icon(self.project_search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition), "search", "#c8d9c7")
        self.project_search.textChanged.connect(self.filter_projects)
        rail.addWidget(self.project_search)
        self.project_list = QListWidget()
        self.project_list.setAccessibleName("Projetos cadastrados")
        self.project_list.setIconSize(QSize(18, 18))
        self.project_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.project_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.project_list.currentItemChanged.connect(self.select_project)
        self.project_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(self.project_menu)
        rail.addWidget(self.project_list, 1)
        list_hint = label("Fixados e mais recentes primeiro. Botão direito ou Shift+F10 para fixar, editar ou remover.", "railMuted")
        list_hint.setWordWrap(True)
        rail.addWidget(list_hint)
        add = button("Adicionar projeto", "plus")
        bind_icon(add, "plus", "#edf5e6")
        add.clicked.connect(self.add_project)
        rail.addWidget(add)
        import_button = button("Importar projetos", role="ghost")
        import_button.clicked.connect(self.import_projects)
        rail.addWidget(import_button)
        rail.addSpacing(10)
        rail.addWidget(label("CONEXÃO", "railMuted"))
        self.connection_picker = ReadableCombo()
        self.connection_picker.setAccessibleName("Conexão com o provedor")
        self.connection_picker.currentIndexChanged.connect(self.select_connection)
        rail.addWidget(self.connection_picker)
        self.address_label = ElidedLabel("")
        self.address_label.setObjectName("railMuted")
        rail.addWidget(self.address_label)
        manage = button("Gerenciar conexões", role="ghost")
        manage.clicked.connect(self.manage_connections)
        rail.addWidget(manage)
        layout.addWidget(sidebar)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.restart_banner = QFrame()
        self.restart_banner.setObjectName("restartBanner")
        banner = QHBoxLayout(self.restart_banner)
        banner.setContentsMargins(16, 12, 14, 12)
        banner.setSpacing(12)
        restart_mark = QLabel()
        bind_icon(restart_mark, "refresh", GREEN, 22)
        banner.addWidget(restart_mark)
        restart_flow = FlowRow(12)
        restart_box = QWidget()
        restart_texts = QVBoxLayout(restart_box)
        restart_texts.setContentsMargins(0, 0, 0, 0)
        restart_texts.setSpacing(2)
        self.restart_title = label("", "restartTitle")
        self.restart_title.setWordWrap(True)
        self.restart_detail = label("")
        self.restart_detail.setWordWrap(True)
        restart_texts.addWidget(self.restart_title)
        restart_texts.addWidget(self.restart_detail)
        restart_flow.add(restart_box, 1, width=lambda: self.restart_title.fontMetrics().horizontalAdvance(
            self.restart_title.text()) + 8)
        self.restart_button = button("&Reiniciar agora", "refresh", "primary")
        self.restart_button.clicked.connect(self.restart)
        restart_flow.add(self.restart_button, 0, Qt.AlignmentFlag.AlignRight)
        banner.addWidget(restart_flow, 1)
        self.restart_banner.hide()
        self.restart_left = 0
        self.restart_timer = QTimer(self)
        self.restart_timer.setInterval(1000)
        self.restart_timer.timeout.connect(self.tick_restart)
        self.copied_timer = QTimer(self)
        self.copied_timer.setSingleShot(True)
        self.copied_timer.setInterval(InfoStrip.HIDE_MS)
        self.copied_timer.timeout.connect(lambda: self.end_footer("copia"))
        banner_row = QHBoxLayout()
        banner_row.setContentsMargins(32, 14, 32, 0)
        self.side_margins = [(banner_row, 14, 0)]
        banner_row.addWidget(self.restart_banner)
        column.addLayout(banner_row)
        scrolled = QWidget()
        scrolled.setObjectName("page")
        body = QVBoxLayout(scrolled)
        body.setContentsMargins(32, 26, 32, 12)
        self.side_margins.append((body, 26, 12))
        body.setSpacing(12)
        self.page = QScrollArea()
        self.page.setObjectName("page")
        self.page.setWidgetResizable(True)
        self.page.setFrameShape(QFrame.Shape.NoFrame)
        self.page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.page.setWidget(scrolled)
        column.addWidget(self.page, 1)
        dock_frame = QFrame()
        dock_frame.setObjectName("actionsBar")
        dock = QVBoxLayout(dock_frame)
        dock.setContentsMargins(32, 12, 32, 18)
        self.side_margins.append((dock, 12, 18))
        dock.setSpacing(8)
        column.addWidget(dock_frame)
        top = FlowRow(12)
        self.project_title = ElidedLabel("Escolha um projeto")
        self.project_title.setObjectName("title")
        self.project_title.setMinimumHeight(43)
        top.add(self.project_title, 1,
                width=lambda: self.project_title.fontMetrics().horizontalAdvance(self.project_title.full_text) + 8)
        status_box = QWidget()
        status_row = QHBoxLayout(status_box)
        status_row.setContentsMargins(0, 0, 0, 0)
        self.connection = label("Verificando conexão…", "status")
        status_row.addWidget(self.connection)
        self.options = QToolButton()
        self.options.setObjectName("ghost")
        bind_icon(self.options, "more")
        self.options.setToolTip("Opções do projeto e diagnóstico")
        self.options.setAccessibleName("Opções do projeto e diagnóstico")
        self.options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = DangerMenu(self)
        menu.setToolTipsVisible(True)
        self.add_project_actions(menu, context=False)
        menu.addSeparator()
        menu.addAction("Conexões…", self.manage_connections)
        menu.addAction("Diagnóstico da conexão", self.show_diagnostics)
        self.bypass_preference = QAction("Bypass marcado por padrão", self)
        self.bypass_preference.setCheckable(True)
        self.bypass_preference.setToolTip("A caixa do Bypass já vem marcada em toda abertura: a sessão abre sem pedir permissão "
                                          "(no Codex, também sem sandbox).")
        self.bypass_preference.setChecked(self.store.preference("bypass_default"))
        self.bypass_preference.toggled.connect(self.set_bypass_default)
        menu.addAction(self.bypass_preference)
        self.update_preference = QAction("Atualizar automaticamente", self)
        self.update_preference.setCheckable(True)
        self.update_preference.setChecked(self.store.preference("auto_update"))
        self.update_preference.toggled.connect(lambda checked: self.save_preference("auto_update", checked))
        menu.addAction(self.update_preference)
        self.cli_update_preference = QAction("Atualizar as CLIs automaticamente", self)
        self.cli_update_preference.setCheckable(True)
        self.cli_update_preference.setChecked(self.store.preference("cli_auto_update"))
        self.cli_update_preference.toggled.connect(lambda checked: self.save_preference("cli_auto_update", checked))
        menu.addAction(self.cli_update_preference)
        menu.addAction("Tema da interface…", self.choose_theme)
        menu.addAction("Instruções e comandos…", self.show_instructions)
        self.options.setMenu(menu)
        status_row.addWidget(self.options)
        top.add(status_box, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(top)
        project_path_row = QHBoxLayout()
        self.project_path = label("Seus projetos e suas sessões, em um só lugar.", "muted")
        self.project_path.setWordWrap(True)
        project_path_row.addWidget(self.project_path, 1)
        self.open_folder = button("Abrir &pasta", "folder", "ghost")
        self.open_folder.clicked.connect(self.open_project)
        project_path_row.addWidget(self.open_folder, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(project_path_row)
        body.addSpacing(2)
        self.cli_group = QButtonGroup(self)
        self.cli_buttons = {}
        for harness in registry.all():
            widget = CliButton(harness.LABEL + (" (prévia)" if harness.PREVIEW else ""), harness.SUBTITLE, harness.SYMBOL)
            self.cli_group.addButton(widget)
            self.cli_buttons[harness.ID] = widget
            widget.clicked.connect(lambda checked=False, chosen=harness.ID: self.choose_card(chosen))
            widget.action_requested.connect(lambda chosen=harness.ID: self.update_cli_now(chosen))
        self.cli_buttons["claude"].setChecked(True)
        self.cli_grid = CardGrid(list(self.cli_buttons.values()))
        body.addWidget(self.cli_grid)
        model_heading = QHBoxLayout()
        model_heading.addWidget(label("Modelo da sessão", "section"))
        self.catalog_tag = label("", "tag")
        self.catalog_tag.hide()
        model_heading.addWidget(self.catalog_tag)
        model_heading.addStretch()
        self.model_count = label("", "muted")
        model_heading.addWidget(self.model_count)
        body.addLayout(model_heading)
        filters = FlowRow(8)
        search_box = QWidget()
        search_row = QHBoxLayout(search_box)
        search_row.setContentsMargins(0, 0, 0, 0)
        tools = FlowRow(8)
        choices_box = QWidget()
        choices_row = QHBoxLayout(choices_box)
        choices_row.setContentsMargins(0, 0, 0, 0)
        actions_box = QWidget()
        actions_row = QHBoxLayout(actions_box)
        actions_row.setContentsMargins(0, 0, 0, 0)
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("Buscar modelo, família ou ID")
        self.model_search.setAccessibleName("Buscar modelos")
        bind_icon(self.model_search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition), "search", MUTED)
        self.model_search.textChanged.connect(self.populate_models)
        search_row.addWidget(self.model_search, 1)
        self.provider_filter = ReadableCombo()
        self.provider_filter.tighten()
        self.provider_filter.setAccessibleName("Filtrar provider")
        self.provider_filter.currentIndexChanged.connect(self.populate_models)
        search_row.addWidget(self.provider_filter)
        self.source_filter = ReadableCombo()
        self.source_filter.tighten()
        self.source_filter.setAccessibleName("Filtrar fonte")
        self.source_filter.currentIndexChanged.connect(self.populate_models)
        search_row.addWidget(self.source_filter)
        self.source_filter.hide()
        self.media_filter = ReadableCombo()
        self.media_filter.tighten()
        self.media_filter.setAccessibleName("Filtrar por entrada")
        for text, field in MEDIA_FILTERS:
            self.media_filter.addItem(text, field)
        self.media_filter.fit_to_items()
        self.media_filter.currentIndexChanged.connect(self.populate_models)
        choices_row.addWidget(self.media_filter)
        self.model_order = ReadableCombo()
        self.model_order.tighten()
        self.model_order.setAccessibleName("Ordenar modelos")
        for text, order in MODEL_ORDERS:
            self.model_order.addItem(text, order)
            self.model_order.setItemData(self.model_order.count() - 1, MODEL_ORDER_TIPS[order], Qt.ItemDataRole.ToolTipRole)
        self.model_order.fit_to_items()
        self.model_order.currentIndexChanged.connect(self.populate_models)
        choices_row.addWidget(self.model_order)
        self.copy_models_button = button("Copiar lista", "copy", "ghost")
        self.copy_models_button.setAccessibleName("Copiar lista")
        self.copy_models_button.setToolTip("Copia a lista como está filtrada, separada por tabulação, para colar numa planilha")
        self.copy_models_button.clicked.connect(self.copy_models)
        actions_row.addWidget(self.copy_models_button)
        self.refresh_button = button("Atualizar", "refresh")
        self.refresh_button.setAccessibleName("Atualizar")
        self.refresh_button.setToolTip("Atualizar catálogo e verificar proxy (F5)")
        self.refresh_button.clicked.connect(lambda: self.refresh_catalog(True))
        actions_row.addWidget(self.refresh_button)
        tools.add(choices_box, width=lambda: choices_box.minimumSizeHint().width())
        tools.add(actions_box, width=lambda: actions_box.minimumSizeHint().width())
        # Numa linha só enquanto tudo cabe nas larguras mínimas (o filtro de provider pede o item mais longo inteiro);
        # na janela estreita as partes descem uma para cada linha em vez de saírem da janela.
        filters.add(search_box, 1, width=lambda: search_box.minimumSizeHint().width())
        filters.add(tools, width=lambda: choices_box.minimumSizeHint().width() + tools.box.spacing()
                    + actions_box.minimumSizeHint().width())
        body.addWidget(filters)
        self.model_table = ModelTable(0, 0)
        self.model_table.verticalHeader().hide()
        self.model_table.verticalHeader().setDefaultSectionSize(40)
        self.model_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.source_column = None
        self.layout_model_columns(False)
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.model_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.model_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.model_table.setShowGrid(False)
        self.model_table.setMinimumHeight(160)
        self.model_table.itemSelectionChanged.connect(self.select_model)
        self.model_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.model_table.customContextMenuRequested.connect(self.model_menu)
        body.addWidget(self.model_table, 1)
        self.empty_models = label("", "muted")
        self.empty_models.setWordWrap(True)
        self.empty_models.hide()
        body.addWidget(self.empty_models)
        context_card = QFrame()
        context_card.setObjectName("contextCard")
        context = QVBoxLayout(context_card)
        context.setContentsMargins(16, 12, 16, 12)
        context.setSpacing(4)
        self.model_id = ElidedLabel()
        self.model_id.setObjectName("mono")
        context.addWidget(self.model_id)
        self.context_info = label("Escolha um modelo para ver o contexto.")
        self.context_info.setWordWrap(True)
        context.addWidget(self.context_info)
        self.family_info = ElidedLabel()
        self.family_info.setObjectName("muted")
        context.addWidget(self.family_info)
        self.media_notice = NoticeStrip("mediaNotice", "alert", WARN)
        self.media_notice.row.setContentsMargins(8, 4, 10, 4)
        context.addWidget(self.media_notice)
        body.addWidget(context_card)
        self.info_strip = InfoStrip()
        self.info_notice = self.info_strip.info
        self.footer = self.info_strip.status
        dock.addWidget(self.info_strip)
        self.problems = QWidget()
        problems = QVBoxLayout(self.problems)
        problems.setContentsMargins(0, 0, 0, 0)
        problems.setSpacing(6)
        self.error_strip = NoticeStrip("errorStrip", "error", DANGER)
        self.notice = self.error_strip.text
        self.copy_error = button("Copiar erro", "copy", "ghost")
        self.copy_error.setToolTip("Copia os erros com o contexto (versões, Windows, Terminal) para colar num chat de IA")
        self.copy_error.clicked.connect(self.copy_notice)
        self.error_strip.row.addWidget(self.copy_error, 0, Qt.AlignmentFlag.AlignTop)
        problems.addWidget(self.error_strip)
        self.warning_strip = NoticeStrip("warningStrip", "alert", WARN)
        self.warning_notice = self.warning_strip.text
        problems.addWidget(self.warning_strip)
        self.problems.hide()
        dock.addWidget(self.problems)
        if self.store.recovered:
            self.show_warning(self.store.recovered, origin="recuperacao")
        actions = FlowRow(12)
        session_box = QWidget()
        session_options = QVBoxLayout(session_box)
        session_options.setContentsMargins(0, 0, 0, 0)
        session_options.setSpacing(4)
        self.bypass_checkbox = QCheckBox("&Bypass de permissões")
        self.bypass_checkbox.setChecked(self.bypass_default)
        self.bypass_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bypass_checkbox.toggled.connect(self.update_actions)
        session_options.addWidget(self.bypass_checkbox)
        self.bypass_risk = NoticeStrip("bypassRisk", "alert", WARN)
        self.bypass_risk.row.setContentsMargins(8, 4, 10, 4)
        self.bypass_risk.row.setSpacing(6)
        session_options.addWidget(self.bypass_risk)
        self.session_hint = label("A sessão abre em outra janela.", "muted")
        self.session_hint.setWordWrap(True)
        session_options.addWidget(self.session_hint)
        actions.add(session_box, 1, width=lambda: self.bypass_checkbox.sizeHint().width())
        buttons_flow = FlowRow(8)
        self.install_cli_button = button("Instalar", "plus")
        self.install_cli_button.setMinimumHeight(44)
        self.install_cli_button.clicked.connect(self.install_cli)
        self.install_cli_button.hide()
        buttons_box = QWidget()
        buttons = QHBoxLayout(buttons_box)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.installing_cli = False
        self.plan_button = QToolButton()
        bind_icon(self.plan_button, "vision")
        self.plan_button.setMinimumHeight(44)
        self.plan_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plan_button.setAccessibleName("Ver o que vai abrir")
        self.plan_button.setToolTip("Ver o que a nova sessão vai iniciar: comando, conexão, modelo, ambiente e avisos")
        self.plan_button.clicked.connect(self.show_session_plan)
        buttons.addWidget(self.plan_button)
        self.resume_button = button("Retomar última", "resume")
        self.resume_button.setMinimumHeight(44)
        self.resume_button.clicked.connect(lambda: self.launch_session(True))
        buttons.addWidget(self.resume_button)
        self.build_conversation_menu(buttons)
        self.launch_button = button("Nova sessão", "arrow", "primary")
        self.launch_button.setMinimumHeight(44)
        self.launch_button.clicked.connect(lambda: self.launch_session(False))
        buttons.addWidget(self.launch_button)
        # Na janela estreita, Instalar sobe para uma linha própria em vez de espremer os botões da sessão.
        buttons_flow.add(self.install_cli_button, 0, Qt.AlignmentFlag.AlignRight)
        buttons_flow.add(buttons_box, 0, Qt.AlignmentFlag.AlignRight)
        actions.add(buttons_flow, 0, Qt.AlignmentFlag.AlignRight,
                    width=lambda: sum(part.sizeHint().width() for part in (self.install_cli_button, buttons_box)
                                      if part.isVisibleTo(buttons_flow)) + buttons_flow.box.spacing())
        dock.addWidget(actions)
        layout.addWidget(content, 1)

    def populate_projects(self, preferred=None):
        self.project_list.blockSignals(True)
        self.project_list.clear()
        selected = None
        pinned = self.store.pinned_ids()
        for project in self.store.projects:
            item = QListWidgetItem(project["name"])
            item.setData(Qt.ItemDataRole.UserRole, project["id"])
            item.setData(PROJECT_PINNED, project["id"] in pinned)
            item.setIcon(project_icon(item, False))
            text, tip = project["name"], project["path"]
            if not Path(project["path"]).is_dir():
                text += "  (pasta não encontrada)"
                tip = (f"Pasta não encontrada: {project['path']}\n"
                       "Botão direito → Editar para apontar a nova pasta.")
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            if project["id"] in pinned:
                tip += "\nFixado no topo da lista."
            item.setData(PROJECT_TEXT, (text, tip))
            self.project_list.addItem(item)
            if project["id"] == preferred:
                selected = item
        self.mark_open_sessions()
        self.project_list.blockSignals(False)
        self.project_count.setText(str(len(self.store.projects)))
        self.filter_projects()
        if selected or self.project_list.count():
            self.project_list.setCurrentItem(selected or self.project_list.item(0))
        else:
            self.select_project(None)

    def mark_open_sessions(self) -> int:
        """Marca na lista os projetos com sessão aberta agora, aberta por esta ou por outra instância do launcher.
        Devolve quantas sessões estão registradas."""
        open_clis: dict[str, list[str]] = {}
        registered = keys.sessions()
        for session in registered:
            open_clis.setdefault(session.get("project_id"), []).append(label_of(session.get("cli", "")))
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            text, tip = item.data(PROJECT_TEXT)
            clis_open = open_clis.get(item.data(Qt.ItemDataRole.UserRole), [])
            if clis_open:
                count = len(clis_open)
                text += "  · aberta" if count == 1 else f"  · {count} abertas"
                tip += f"\n{plural(count, 'sessão aberta', 'sessões abertas')}: {', '.join(sorted(set(clis_open)))}"
            if item.text() != text:
                item.setText(text)
            item.setToolTip(tip)
        return len(registered)

    def filter_projects(self):
        query = self.project_search.text().strip().casefold()
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            item.setHidden(query not in item.text().casefold() and query not in item.toolTip().casefold())

    def select_project(self, item, previous=None):
        if previous:
            previous.setIcon(project_icon(previous, False))
        self.project = self.store.find(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if item:
            item.setIcon(project_icon(item, True))
        self.project_title.setText(self.project["name"] if self.project else "Seu próximo projeto começa aqui")
        self.project_path.setText(breakable_path(self.project["path"]) if self.project else
                                  "Adicione uma pasta para começar. Nenhum arquivo será alterado.")
        self.project_path.setToolTip(self.project["path"] if self.project else "")
        self.project_path.setAccessibleName(self.project["path"] if self.project else "Pasta do projeto")
        self.project_path.setObjectName("pathMono" if self.project else "muted")
        self.project_path.style().unpolish(self.project_path)
        self.project_path.style().polish(self.project_path)
        self.open_folder.setEnabled(self.project is not None)
        connection = self.project.get("last_connection") if self.project else None
        index = self.connection_picker.findData(connection) if connection else -1
        if index >= 0 and index != self.connection_picker.currentIndex():
            self.connection_picker.setCurrentIndex(index)  # select_connection recarrega CLI e modelos
            return
        self.select_cli(self.project["last_cli"] if self.project else self.cli)

    @property
    def bypass_default(self) -> bool:
        return self.store.preference("bypass_default")

    def save_preference(self, key: str, checked: bool) -> None:
        """Grava a preferência `key` do perfil; a falha vira erro na faixa em vez de derrubar a janela."""
        try:
            fresh = Store(self.store.home)
            fresh.set_preference(key, checked)
            self.store = fresh
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível salvar a preferência: {error}")

    def start_cli_check(self):
        if self.cli_checking or self.cli_updating:
            return
        self.cli_checking = True
        self.cli_check_task = CliCheckTask()
        run_task(self.cli_check_task, self.finish_cli_check)

    @Slot(object, str)
    def finish_cli_check(self, result, _unused=""):
        self.cli_checking = False
        self.startup_mark("clis")
        if isinstance(result, Exception):
            clis.record(f"verificação das CLIs falhou: {result}")
            self.show_warning(f"Não foi possível verificar as versões das CLIs: {result}", origin="cli:verificacao")
            return
        self.cli_versions = result
        self.refresh_cli_versions()
        problems, self.cli_queue = [], []
        for cli, found in result.items():
            if not found["newer"]:
                self.clear_error(f"cli:{cli}")  # em dia (atualizada por fora, inclusive): a falha de atualizar não vale mais
            if found["error"] and not found["installed"]:
                problems.append(f"{clis.NAMES[cli]}: não foi possível verificar a versão ({found['error']}).")
                continue
            if not found["newer"]:
                continue
            if self.store.preference("cli_auto_update") and not cli_update.disabled_by_env() \
                    and (cli, found["latest"]) not in self.cli_failed:
                self.cli_queue.append(cli)
        if problems:
            self.show_warning("\n".join(problems), origin="cli:verificacao")
        else:
            self.clear_error("cli:verificacao")
        self.run_next_cli_update()
        self.update_actions()

    @staticmethod
    def sessions_note(count: int) -> str:
        return (f" {plural(count, 'sessão aberta segue', 'sessões abertas seguem')} na versão antiga; a nova vale na "
                "próxima abertura." if count else "")

    def refresh_cli_versions(self):
        """Versão instalada na linha do cartão; na faixa dele, a versão nova com Atualizar ou a falha com Tentar de novo."""
        for cli, widget in self.cli_buttons.items():
            found = self.cli_versions.get(cli)
            widget.set_version(f"versão {found['installed']}" if found and found["installed"] else "")
            if not found or not found["newer"]:
                widget.set_offer("")
            elif (cli, found["latest"]) in self.cli_failed:
                widget.set_offer(f"Não atualizou para {found['latest']}", "Tentar de novo", WARN, "alert")
            else:
                widget.set_offer(f"{found['latest']} disponível", "Atualizar", GREEN, "refresh")
            widget.action.setEnabled(self.cli_updating is None and not self.cli_checking)

    def run_next_cli_update(self):
        if self.cli_updating or not self.cli_queue:
            return
        self.cli_updating = self.cli_queue.pop(0)
        latest = (self.cli_versions.get(self.cli_updating) or {}).get("latest")
        self.cli_buttons[self.cli_updating].set_progress(None, f"Atualizando para {latest}…" if latest else "Atualizando…", INFO)
        self.refresh_cli_versions()
        self.update_actions()
        self.cli_update_task = CliUpdateTask(self.cli_updating)
        run_task(self.cli_update_task, self.finish_cli_update)

    @Slot(object, str)
    def finish_cli_update(self, result, cli):
        self.cli_updating = None
        forget_installed()
        name = clis.NAMES[cli]
        found = self.cli_versions.get(cli) or {}
        if isinstance(result, cli_update.UpdateDeferred):
            self.clear_error(f"cli:{cli}")
            self.cli_buttons[cli].finish_update("Adiada · sessões abertas")
            self.show_info(str(result), origin=f"cli:{cli}")
        elif isinstance(result, Exception):
            self.cli_failed.add((cli, found.get("latest")))
            self.cli_buttons[cli].finish_update()
            target = f" para {found['latest']}" if found.get("latest") else ""
            current = f"na versão {found['installed']}" if found.get("installed") else "na versão de antes"
            self.show_warning(f"O {name} não atualizou{target} e segue {current}: {result}", origin=f"cli:{cli}")
        elif result["updated"]:
            self.clear_error(f"cli:{cli}")
            found.update(installed=result["after"], newer=False, running=0)
            self.cli_buttons[cli].finish_update(f"✓ Atualizado para {result['after']}")
            if result["sessions"]:
                self.show_info(f"{name} atualizado: {result['before']} → {result['after']}." + self.sessions_note(result["sessions"]),
                               origin=f"cli:{cli}")
        else:
            self.clear_error(f"cli:{cli}")
            found.update(installed=result["after"], newer=False, running=0)
            self.cli_buttons[cli].finish_update("✓ Em dia")
        self.refresh_cli_versions()
        self.run_next_cli_update()
        self.update_actions()

    def update_cli_now(self, cli=None):
        """Atualizar (ou Tentar de novo) da faixa do cartão: entra na frente da fila, inclusive a versão que já falhou."""
        cli = cli or self.cli
        found = self.cli_versions.get(cli)
        if found and found["newer"] and not self.cli_updating and not self.cli_checking:
            self.cli_failed.discard((cli, found["latest"]))
            self.cli_queue = [cli] + [queued for queued in self.cli_queue if queued != cli]
            self.run_next_cli_update()

    def update_portable_terminal(self):
        if self.terminal_installing or self.terminal_updating or not self.store.preference("auto_update") \
                or updater.disabled_by_env():
            return
        self.terminal_updating = True
        self.terminal_update_task = TerminalUpdateTask(ROOT)
        run_task(self.terminal_update_task, self.finish_terminal_update)

    @Slot(object, str)
    def finish_terminal_update(self, result, _unused=""):
        self.terminal_updating = False
        if isinstance(result, Exception):
            # Sem rede ou sem acesso ao GitHub: não vira faixa a cada abertura; fica no registro do Terminal.
            record_terminal_problem(self.problem_report(f"Atualização do Windows Terminal portátil falhou: {result}"))
        elif result and not result.startswith("adiada"):
            self.clear_error("terminal")
            self.show_info(result, origin="terminal:atualizacao")

    def start_auto_update(self):
        self.update_portable_terminal()
        self.start_cli_check()
        if self.update_status == "verificando…":
            return  # o timer periódico não sobrepõe uma verificação em andamento
        if not self.store.preference("auto_update"):
            self.update_status = "desligada nas opções"
        elif updater.disabled_by_env():
            self.update_status = f"desligada por {updater.DISABLE_ENV}"
        elif not updater.installed(ROOT):
            self.update_status = "não se aplica (cópia de desenvolvimento, sem manifesto do instalador)"
        else:
            self.update_status = "verificando…"
            self.update_task = UpdateTask(ROOT)
            run_task(self.update_task, self.finish_auto_update)

    @Slot(object, str)
    def finish_auto_update(self, result, _unused=""):
        if isinstance(result, updater.CheckError):
            # Sem rede/proxy: não vira faixa a cada abertura; fica no Diagnóstico e no update.log.
            self.update_status = f"falhou: {result}"
        elif isinstance(result, Exception):
            self.update_status = f"falhou: {result}"
            self.show_warning(f"{APP_NAME} não se atualizou e segue na versão {VERSION}: {result}", origin="launcher:atualizacao")
        elif result:
            self.update_status = f"atualizado para {result}; reiniciando a interface"
            self.clear_error("launcher:atualizacao")
            self.announce_restart(result)
        else:
            self.update_status = f"em dia ({VERSION})"
            self.clear_error("launcher:atualizacao")
        updater.record(self.update_status)

    def announce_restart(self, version):
        """Faixa de reinício fixa no topo, com contagem de verdade: a janela aberta segue com o código antigo na memória
        e, sem reiniciar, as correções não chegam a ela."""
        self.restart_title.setText(f"{APP_NAME} {version} pronto")
        self.restart_button.setEnabled(True)
        self.restart_banner.show()
        self.restart_left = RESTART_DELAY_MS // 1000
        if self.restart_left > 0:
            self.render_countdown()
            self.restart_timer.start()
        else:
            self.restart_timer.stop()
            QTimer.singleShot(0, self.finish_countdown)

    def tick_restart(self):
        self.restart_left -= 1
        if self.restart_left > 0:
            self.render_countdown()
        else:
            self.restart_timer.stop()
            self.finish_countdown()

    def render_countdown(self):
        self.restart_detail.setText(f"Reinicia a interface em {self.restart_left} s. As sessões abertas continuam.")

    def finish_countdown(self):
        self.restart_detail.setText("Reiniciando…")
        self.restart_button.setEnabled(False)
        self.restart_if_idle()

    def restart_if_idle(self):
        """Reinicia sozinho, a menos que haja diálogo aberto ou instalação em curso (aí a faixa pede o reinício)."""
        busy = (QApplication.activeModalWidget() is not None or self.installing_cli or self.terminal_installing
                or self.cli_updating is not None)
        if busy:
            self.update_status = self.update_status.replace("reiniciando a interface", "reabra para usar")
            self.restart_detail.setText("Termine o que está fazendo e reinicie para usar a versão nova.")
            self.restart_button.setEnabled(True)
            return
        self.restart()

    def set_footer(self, origin, text, busy=False):
        """Rodapé: operação em curso (busy) fica na frente até terminar; o que chegar enquanto isso espera a sua vez, e
        sem operação em curso vale a mensagem mais recente."""
        self.footer_lines.pop(origin, None)
        self.footer_lines[origin] = text
        self.footer_busy.discard(origin)
        if busy:
            self.footer_busy.add(origin)
        self.render_footer()

    def end_footer(self, origin):
        self.footer_busy.discard(origin)
        self.footer_lines.pop(origin, None)
        self.render_footer()

    def render_footer(self):
        busy = [origin for origin in self.footer_lines if origin in self.footer_busy]
        shown = busy[-1] if busy else next(reversed(self.footer_lines), None)
        self.refresh_info_strip(None, self.footer_lines.get(shown, ""))

    def refresh_info_strip(self, info=None, status=None):
        """Atualiza a faixa de informativo; o que não vier fica como estava."""
        info = self.info_notice.text() if info is None else info
        status = self.footer.full_text if status is None else status
        self.info_strip.refresh(info, status, bool(self.footer_busy))

    def restart(self):
        """Reabre o launcher pela versão instalada e fecha esta janela (as sessões são processos à parte)."""
        import subprocess
        pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        executable = pythonw if pythonw.is_file() else Path(sys.executable)
        subprocess.Popen([str(executable), str(ROOT / "launcher.pyw")], cwd=ROOT, close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        QApplication.instance().quit()

    def set_bypass_default(self, checked):
        try:
            fresh = Store(self.store.home)
            fresh.set_preference("bypass_default", checked)
            self.store = fresh
            self.bypass_checkbox.setChecked(checked)
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))

    def interface_store(self) -> Store:
        """Onde o tema fica: a pasta de dados raiz, a mesma para todos os perfis (o tema é do launcher, não do perfil)."""
        return self.store if self.store.home == self.profile_base else Store(self.profile_base)

    def choose_theme(self):
        store = self.interface_store()
        self.theme_dialog = ThemeDialog(store.choice("theme"), store.choice("accent"), self)
        self.theme_dialog.accepted.connect(self.save_theme)
        self.theme_dialog.open()

    def save_theme(self):
        theme, accent = self.theme_dialog.chosen()
        try:
            fresh = Store(self.profile_base)
            fresh.set_choice("theme", theme)
            fresh.set_choice("accent", accent)
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível salvar o tema: {error}")
            return
        if self.store.home == fresh.home:
            self.store = fresh
        theme_module.apply(QApplication.instance(), theme, accent)

    def paint_search_placeholder(self):
        palette = self.project_search.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tone("#c4d8c6", True)))
        self.project_search.setPalette(palette)

    def retone(self):
        """Refaz o que o tema pinta fora da folha de estilo e dos ícones ligados: o texto de dica da busca e os ícones
        das listas de projetos e de modelos."""
        self.paint_search_placeholder()
        current = self.project_list.currentItem()
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            item.setIcon(project_icon(item, item is current))
        for row, record in enumerate(self.records):
            for column, (field, _title, symbol) in enumerate(MEDIA_COLUMNS, start=self.media_column):
                item, state = self.model_table.item(row, column), getattr(record, field)
                if item is not None and state is not None:
                    item.setIcon(media_icon(symbol, state))

    def populate_connections(self, preferred=None):
        self.connection_picker.blockSignals(True)
        self.connection_picker.clear()
        for connection in self.store.connections:
            self.connection_picker.addItem(connection["name"], connection["id"])
        if preferred is None:
            try:
                preferred = self.store.connection_for(self.project)["id"]
            except LauncherError:
                preferred = None
        self.connection_picker.setCurrentIndex(max(0, self.connection_picker.findData(preferred)))
        self.connection_picker.blockSignals(False)
        self.select_connection()

    def select_connection(self, *_args):
        """Adota a conexão escolhida no seletor: refaz o catálogo e, se ela oferece custo por fonte, relê o custo dela."""
        identifier = self.connection_picker.currentData()
        self.gateway, self.catalog, self.online = None, None, False
        self.source_limit_checking = False
        self.clear_transient("connection", "teto-fontes")
        try:
            if identifier:
                self.gateway = Gateway(self.store, self.store.find_connection(identifier))
                self.catalog = self.gateway.catalog(offline=True)
        except LauncherError as error:
            self.gateway = None
            self.show_error(str(error))
        self.address_label.setText(self.gateway.url if self.gateway else "Nenhuma conexão")
        self.address_label.setToolTip(self.gateway.url if self.gateway else "")
        self.refresh_card_states()
        if not self.gateway:
            self.connection.setText("✕ Sem conexão")
            self.connection.setProperty("offline", True)
            if not self.store.connections:
                self.show_error("Adicione uma conexão com um provedor (Gerenciar conexões) para listar modelos e abrir sessões.")
        else:
            self.connection.setText("Verificando conexão…")
        preferred = self.store.last_cli(self.project) if self.project else self.cli
        served = [cli for cli in registry.ids() if self.gateway and cli in self.gateway.clis]
        available = [cli for cli in served if self.cli_buttons[cli].installed] or served or ["claude"]
        self.select_cli(preferred if preferred in available else available[0])
        if self.gateway and self.auto_refresh:
            if not self.startup_done:
                self.startup_pending.add("catalogo")
            QTimer.singleShot(0, self.refresh_catalog)
            QTimer.singleShot(0, self.check_source_limits)

    def manage_connections(self):
        ConnectionsDialog(self.store.home, self).exec()
        current = self.connection_picker.currentData()
        try:
            self.store = Store(self.store.home)
        except LauncherError as error:
            self.show_error(str(error))
            return
        known = {connection["id"] for connection in self.store.connections}
        self.populate_connections(current if current in known else None)

    def refresh_card_states(self):
        """Cartão desligado pode instalar; o instalado só fica habilitado se a conexão serve aquela ferramenta."""
        for cli, widget in self.cli_buttons.items():
            if not widget.installing:
                widget.set_installed(installed_path(cli) is not None)
            widget.setEnabled(not widget.installed or bool(self.gateway and cli in self.gateway.clis))

    def showEvent(self, event):
        super().showEvent(event)
        if "pintada" not in self.startup_phases:
            QTimer.singleShot(0, lambda: self.startup_mark("pintada"))

    def startup_mark(self, phase: str) -> None:
        """Marca uma fase da abertura (segundos desde a criação do processo); com a janela pintada e nada pendente
        (catálogo, versões das CLIs), grava a linha da abertura no log que o Diagnóstico mostra."""
        if self.startup_done or phase in self.startup_phases:
            return
        self.startup_phases[phase] = time.time() - PROCESS_STARTED
        self.startup_pending.discard(phase)
        if "pintada" not in self.startup_phases or self.startup_pending:
            return
        self.startup_done = True
        phases = self.startup_phases
        parts = [f"{name} {phases[key]:.2f} s" for key, name in (
            ("import", "import"), ("janela", "janela"), ("pintada", "pintada"), ("catalogo", "catálogo"),
            ("clis", "CLIs")) if key in phases]
        line = f"{VERSION} · " + " · ".join(parts) + f" · pronta {max(phases.values()):.2f} s"
        try:
            append_capped(self.profile_base / STARTUP_LOG, f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n",
                          STARTUP_LOG_BYTES)
        except OSError as error:
            self.show_warning(f"Não foi possível gravar o tempo da abertura em {STARTUP_LOG}: {error}", origin="abertura")

    def last_startup(self) -> str:
        """A linha da última abertura gravada, sem a data e a hora; vazio se nenhuma foi gravada."""
        try:
            lines = (self.profile_base / STARTUP_LOG).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ""
        return lines[-1].split(" ", 2)[-1] if lines else ""

    @Slot(Qt.ApplicationState)
    def recheck_tools(self, state):
        """Ao voltar para o launcher, ferramenta instalada ou removida por fora aparece no cartão."""
        if state == Qt.ApplicationState.ApplicationActive:
            forget_installed()
            self.refresh_card_states()
            self.update_actions()

    def choose_card(self, cli):
        """Cartão de ferramenta pronta seleciona; o de ferramenta que falta (e que o launcher instala) instala."""
        widget = self.cli_buttons[cli]
        if widget.installed or not registry.get(cli).install_steps():
            self.select_cli(cli)
        elif self.installing_cli and self.installing_cli != cli:
            self.show_info(f"Aguarde o fim da instalação do {registry.get(self.installing_cli).LABEL}: uma ferramenta "
                           "por vez.", origin="cli:instalacao")
        elif not self.installing_cli:
            self.select_cli(cli)
            self.install_cli(cli)

    def select_cli(self, cli):
        self.cli = cli
        self.bypass_checkbox.setChecked(self.bypass_default)
        if self.cli_buttons[cli].isCheckable():
            self.cli_buttons[cli].setChecked(True)
        elif self.cli_group.checkedButton():
            self.cli_group.setExclusive(False)  # grupo exclusivo não desmarca o último marcado
            self.cli_group.checkedButton().setChecked(False)
            self.cli_group.setExclusive(True)
        self.selected_model = None
        self.model_search.blockSignals(True)
        self.model_search.clear()
        self.model_search.blockSignals(False)
        self.populate_providers()
        self.populate_sources()
        self.populate_models()

    def layout_model_columns(self, with_source: bool) -> None:
        """Colunas da tabela de modelos: com a Fonte (oito) quando o conector declara fontes; sem ela (sete) nada sobra no
        lugar dela. Guarda onde ficam a Fonte (`source_column`, None sem ela), a Entrada e a mídia; as células são
        preenchidas por `populate_models`."""
        table = self.model_table
        self.source_column = 2 if with_source else None
        self.input_column = 3 if with_source else 2
        self.media_column = self.input_column + 2
        headers = ["Modelo", "Provider", *(["Fonte"] if with_source else []), "Entrada", "Saída",
                   *(title for _, title, _ in MEDIA_COLUMNS)]
        table.clear()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAccessibleName("Modelos disponíveis: nome, provider, " + ("fonte, " if with_source else "")
                                + "entrada, saída, visão, áudio e vídeo")
        header = table.horizontalHeader()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        roomy = {1: 115, **({self.source_column: SOURCE_COLUMN_WIDTH} if with_source else {}),
                 self.input_column: 90, self.input_column + 1: 80}
        for offset in (0, 1):
            table.horizontalHeaderItem(self.input_column + offset).setTextAlignment(right)
        media = tuple((column, title, symbol) for column, (_, title, symbol) in enumerate(MEDIA_COLUMNS, self.media_column))
        for column, _title, _symbol in media:
            table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            roomy[column] = 64
        table.configure(roomy, (1, *([self.source_column] if with_source else [])), media)
        for column, width in roomy.items():
            table.setColumnWidth(column, width)

    def populate_sources(self):
        """Mostra ou tira o filtro e a coluna Fonte conforme o conector da conexão declara fontes. Com fonte, lista as do
        catálogo do CLI atual (ordem alfabética) e mantém a escolha se ela ainda existe; sem fonte, o filtro fica vazio
        e escondido e a escolha some."""
        declared = bool(self.catalog and self.catalog.segregates_sources)
        if declared != (self.source_column is not None):
            self.layout_model_columns(declared)
        current = self.source_filter.currentData()
        records = self.catalog.for_cli(self.cli) if declared else []
        self.source_filter.blockSignals(True)
        self.source_filter.clear()
        if declared:
            self.source_filter.addItem(ALL_SOURCES, None)
            for label in sorted({record.source for record in records if record.source}, key=str.casefold):
                self.source_filter.addItem(label, label)
            self.source_filter.setCurrentIndex(max(0, self.source_filter.findData(current)))
            self.source_filter.fit_to_items()
        self.source_filter.setVisible(declared)
        self.source_filter.blockSignals(False)
        self.copy_models_button.setText("" if declared else "Copiar lista")
        self.refresh_button.setText("" if declared else "Atualizar")

    def populate_providers(self):
        current = self.provider_filter.currentData()
        self.provider_filter.blockSignals(True)
        self.provider_filter.clear()
        self.provider_filter.addItem("Todos os providers", None)
        for provider in sorted({record.provider for record in self.catalog.for_cli(self.cli)} if self.catalog else ()):
            self.provider_filter.addItem(provider_name(provider), provider)
        match = self.provider_filter.findData(current)
        self.provider_filter.setCurrentIndex(max(0, match))
        self.provider_filter.fit_to_items()
        self.media_filter.fit_to_items()
        self.model_order.fit_to_items()
        self.provider_filter.blockSignals(False)

    def populate_models(self):
        preferred = self.selected_model.id if self.selected_model else None
        if not preferred and self.project and self.gateway:
            preferred = self.store.last_model(self.project, self.gateway.connection, self.cli)
        query = self.model_search.text().strip().casefold()
        provider = self.provider_filter.currentData()
        source = self.source_filter.currentData()
        media = self.media_filter.currentData()
        all_records = self.catalog.for_cli(self.cli) if self.catalog else []
        self.records = [record for record in all_records if (provider is None or record.provider == provider)
                        and (source is None or source == record.source)
                        and (media is None or getattr(record, media) is True)
                        and (query in record.id.casefold() or query in model_name(record.id).casefold())]
        self.records = order_models(self.records, self.model_order.currentData())
        self.model_table.blockSignals(True)
        self.model_table.clearContents()
        self.model_table.setRowCount(len(self.records))
        selected_row = 0
        for row, record in enumerate(self.records):
            values = [model_name(record.id), provider_name(record.provider)]
            if self.source_column is not None:
                values.append(record.source or "")
            values += [token_label(record.max_input_tokens).replace(".", ","),
                       "—" if record.max_output_tokens is None else token_label(record.max_output_tokens).replace(".", ",")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == self.source_column:
                    item.setToolTip(f"Fonte: {value}")
                else:
                    item.setToolTip(record.id if column < self.input_column else self.catalog.describe(record))
                if column >= self.input_column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.model_table.setItem(row, column, item)
            for column, (field, title, symbol) in enumerate(MEDIA_COLUMNS, start=self.media_column):
                self.model_table.setItem(row, column, self.media_item(title, symbol, getattr(record, field)))
            if record.id == preferred:
                selected_row = row
        self.model_table.fit_columns()
        self.model_table.blockSignals(False)
        if self.records:
            self.model_table.selectRow(selected_row)
        self.catalog_size = len(all_records)
        self.render_empty_models()
        self.model_count.setText(f"{len(self.records)} de {len(all_records)} disponíve{'l' if len(all_records) == 1 else 'is'}")
        self.render_catalog_tag()
        self.select_model()

    @staticmethod
    def media_item(title: str, symbol: str, state: bool | None) -> QTableWidgetItem:
        text, tip = media_cell(title, state)
        item = QTableWidgetItem(text)
        item.setToolTip(tip)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, tip)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if state is not None:
            item.setIcon(media_icon(symbol, state))
        return item

    def render_empty_models(self):
        """Lista vazia diz por quê: carregando, catálogo que não veio ou busca sem resultado."""
        if self.records:
            self.empty_models.hide()
            return
        if self.catalog_size:
            text = "Nenhum modelo encontrado para esta busca. Limpe a busca ou os filtros."
        elif self.refreshing:
            text = "Carregando modelos…"
        elif self.gateway and not self.online and "connection" in self.errors:
            text = "Não foi possível carregar o catálogo de modelos. Verifique a conexão e clique em Atualizar."
        elif not self.gateway:
            text = "Escolha uma conexão para listar os modelos."
        else:
            text = "Nenhum modelo neste catálogo para esta ferramenta. Clique em Atualizar para buscar de novo."
        self.empty_models.setText(text)
        self.empty_models.show()

    def render_catalog_tag(self):
        if not self.catalog or not self.catalog.status:
            self.catalog_tag.hide()
            return
        text, stale = catalog_tag(self.catalog.status)
        self.catalog_tag.setText(text)
        self.catalog_tag.setToolTip(self.catalog.status.replace("atualizar: r", "atualizar: F5"))
        self.catalog_tag.setProperty("stale", stale or not self.online)
        self.catalog_tag.style().unpolish(self.catalog_tag)
        self.catalog_tag.style().polish(self.catalog_tag)
        self.catalog_tag.show()

    def select_model(self):
        row = self.model_table.currentRow()
        self.selected_model = self.records[row] if 0 <= row < len(self.records) else None
        record = self.selected_model
        self.model_id.setText(record.id if record else "")
        if record and self.catalog and self.catalog.verified:
            context, detail = registry.get(self.cli).describe_model(record, self.catalog)
            self.context_info.setText(context or f"Entrada declarada: "
                                                 f"{token_label(record.max_input_tokens).replace('.', ',')} tokens")
            self.family_info.setText(detail)
        else:
            self.context_info.setText("Contexto não confirmado. Nenhum aumento será forçado." if record else "Escolha um modelo da lista.")
            self.family_info.setText("Este host não informa limites (ou o cache venceu); os CLIs usam os seus próprios." if record else "")
        refuses_images = bool(record) and record.vision is False
        self.media_notice.text.setText(f"{model_name(record.id)} não aceita imagem (o conector informou)." if refuses_images else "")
        self.media_notice.setVisible(refuses_images)
        self.update_actions()

    def update_actions(self):
        harness = registry.get(self.cli)
        card = self.cli_buttons.get(self.cli)
        # O cartão já guarda o que refresh_card_states achou: procurar de novo varre o PATH inteiro a cada clique.
        missing = not card.installed if card is not None else installed_path(self.cli) is None
        installable = bool(harness.install_steps())
        self.install_cli_button.setVisible(bool((missing and installable) or self.installing_cli))
        self.install_cli_button.setEnabled(not self.installing_cli)
        self.install_cli_button.setText(f"Instalando {registry.get(self.installing_cli).LABEL}…" if self.installing_cli
                                        else f"Instalar {harness.LABEL}")
        enabled = bool(self.project and self.gateway and self.selected_model and self.online and not self.refreshing
                       and not missing)
        self.launch_button.setEnabled(enabled)
        self.plan_button.setEnabled(enabled)
        self.launch_button.setText("&Nova sessão" if harness.KIND == "terminal" else f"Abrir no {harness.LABEL}")
        resumable = enabled and "resume" in harness.CAPABILITIES and self.has_previous_conversation()
        self.resume_button.setVisible("resume" in harness.CAPABILITIES)
        self.resume_button.setEnabled(resumable)
        self.conversation_button.setVisible("resume" in harness.CAPABILITIES)
        self.conversation_button.setEnabled(resumable)
        self.bypass_checkbox.setVisible("bypass" in harness.CAPABILITIES)
        self.bypass_checkbox.setEnabled(enabled)
        bypass = self.bypass_checkbox.isChecked() and "bypass" in harness.CAPABILITIES and not missing
        description = harness.BYPASS_RISK + " Vale só para a próxima abertura; o padrão fica em Opções → Bypass marcado por padrão."
        self.bypass_checkbox.setToolTip(description)
        self.bypass_checkbox.setAccessibleDescription(description)
        risk = f"Bypass ligado: {harness.BYPASS_HINT[0].lower()}{harness.BYPASS_HINT[1:]}"
        self.bypass_risk.text.setText(risk)
        self.bypass_risk.setToolTip(description)
        self.bypass_risk.setAccessibleName(risk)
        self.bypass_risk.setAccessibleDescription(description)
        self.bypass_risk.setVisible(bypass)
        self.session_hint.setVisible(not bypass)
        absent = self.missing_reason(harness, installable) if missing else ""
        self.session_hint.setText(absent or harness.SESSION_HINT)
        self.session_hint.setToolTip(session_title(self.project, self.cli, self.store.home) if self.project else "Selecione um projeto")
        reason = ("Retoma a última sessão nativa deste CLI na pasta do projeto." if enabled else
                  absent or "Escolha projeto, conexão e modelo; a conexão precisa responder.")
        self.resume_button.setToolTip(reason if resumable or not enabled else
                                      f"Ainda não há conversa do {harness.LABEL} nesta pasta para retomar: use Nova sessão.")
        self.launch_button.setToolTip(session_title(self.project, self.cli, self.store.home) if enabled else reason)

    def missing_reason(self, harness, installable: bool) -> str:
        """Por que a sessão não abre com o CLI ausente, dito no botão desligado e no aviso ao lado dele."""
        if self.installing_cli == harness.ID:
            return f"{harness.LABEL} não instalado: instalando agora, a sessão abre quando terminar."
        if installable:
            return f"{harness.LABEL} não instalado: clique em Instalar {harness.LABEL} (ou no cartão dele) antes da sessão."
        return harness.MISSING_HINT

    def has_previous_conversation(self) -> bool:
        """Pasta compartilhada com outro perfil: só conta conversa aberta por este perfil."""
        try:
            return has_conversation_for(self.store.home, self.cli, project_path(self.project["path"]))
        except (LauncherError, OSError, ValueError):
            return False

    def session_report(self) -> dict:
        """O que a nova sessão vai iniciar, do plano que a própria abertura usa; fora do terminal, a prévia do destino."""
        if not (self.project and self.gateway and self.selected_model and self.catalog):
            raise LauncherError("Escolha projeto, conexão e modelo para ver o que vai abrir.")
        harness = registry.get(self.cli)
        if harness.KIND != "terminal":
            return harness.preview(self.store, self.project, self.gateway, self.selected_model.id, self.catalog)
        plan = planned(self.store.home, self.gateway, self.project["path"], self.cli, self.selected_model.id, False,
                       self.catalog)
        return plan_report(plan, self.gateway, False, self.catalog)

    def show_session_plan(self) -> None:
        """Mostra, só para leitura, o que a nova sessão vai iniciar; nada abre e nada é gravado."""
        try:
            report = self.session_report()
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))
            return
        terminal = "command" in report
        PlanDialog(plan_text(report) if terminal else json.dumps(report, ensure_ascii=False, indent=2), self,
                   self.new_command_for if terminal else None).exec()

    def new_command_for(self, shell: str = "cmd") -> str:
        """O comando que abre uma sessão nova fora do launcher, com a conexão e o modelo escolhidos na tela, escrito para
        o `shell` (cmd ou powershell)."""
        if not (self.project and self.gateway and self.selected_model and self.catalog):
            raise LauncherError("Escolha projeto, conexão e modelo para montar o comando.")
        return new_session_command(self.store.home, self.gateway, self.project["path"], self.cli,
                                   self.selected_model.id, self.catalog, shell)

    def resume_by_shortcut(self) -> None:
        """Ctrl+R faz o que o botão Retomar última faz, só quando o botão está à vista e habilitado."""
        if self.resume_button.isEnabled() and not self.resume_button.isHidden():
            self.launch_session(True)

    def launch_by_shortcut(self) -> None:
        """Ctrl+Enter faz o que o botão Nova sessão faz, só quando o botão está à vista e habilitado (com o CLI ausente
        ele fica desligado)."""
        if self.launch_button.isEnabled() and not self.launch_button.isHidden():
            self.launch_session(False)

    def build_conversation_menu(self, actions):
        """Seta ao lado de 'Retomar última' com 'Escolher conversa…' (lista as conversas do CLI nesta pasta)."""
        self.conversation_button = QToolButton()
        self.conversation_button.setText("▾")
        self.conversation_button.setMinimumHeight(44)
        self.conversation_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.conversation_button.setAccessibleName("Conversas anteriores")
        self.conversation_button.setToolTip("Escolher qual conversa retomar")
        self.conversation_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.conversation_button)
        menu.addAction("Escolher conversa…\tCtrl+H", self.choose_conversation)
        menu.addAction("Reabrir sessões recentes…\tCtrl+Shift+H", self.reopen_recent_sessions)
        self.conversation_button.setMenu(menu)
        actions.addWidget(self.conversation_button)

    def conversation_list(self) -> list:
        try:
            return conversations_for(self.store.home, registry.get(self.cli), project_path(self.project["path"]))
        except (LauncherError, OSError, ValueError):
            return []

    def choose_conversation(self):
        if not self.project:
            return
        found = self.conversation_list()
        if not found:
            self.show_info(f"Ainda não há conversa do {registry.get(self.cli).LABEL} nesta pasta.", origin="conversa")
            return
        dialog = ConversationsDialog(found, self, registry.get(self.cli).LABEL, conversation_usages(self.store.home, found),
                                     self.selected_model, bool(self.catalog and self.catalog.verified),
                                     command_for=self.resume_command_for)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected():
            self.launch_session(True, conversation=dialog.selected())

    def reopen_candidates(self) -> list:
        return reopen.candidates(self.store)

    def reopen_recent_sessions(self) -> None:
        """Item do menu ao lado de Escolher conversa e Ctrl+Shift+H: lista as sessões recentes que não estão abertas e
        reabre as marcadas. Não depende do projeto selecionado."""
        try:
            rows = self.reopen_candidates()
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível listar as sessões recentes: {error}", origin="reabrir")
            return
        if not rows:
            self.show_info(f"Nenhuma sessão aberta nas últimas {reopen.RECENT_HOURS} horas para reabrir "
                           "(as que estão abertas agora ficam de fora).", origin="reabrir")
            return
        dialog = ReopenDialog(rows, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected():
            self.start_reopen(dialog.selected())

    def start_reopen(self, rows: list) -> None:
        if self.reopening:
            return
        self.reopening = True
        self.infos.pop("reabrir", None)
        self.clear_error("reabrir")
        self.set_footer("reabrir", f"Reabrindo {plural(len(rows), 'sessão', 'sessões')}…", busy=True)
        self.reopen_task = ReopenTask(self.store, rows)
        run_task(self.reopen_task, self.finish_reopen)

    @Slot(object, str)
    def finish_reopen(self, results, _origin: str) -> None:
        self.reopening = False
        self.end_footer("reabrir")
        self.infos.pop("reabrir", None)
        self.clear_error("reabrir")
        if isinstance(results, Exception):
            self.show_error(f"Não foi possível reabrir as sessões: {results}", origin="reabrir")
        elif any(not result["ok"] for result in results):
            self.show_warning(reopen.describe(results), origin="reabrir")
        else:
            self.show_info(reopen.describe(results), origin="reabrir")

    def resume_command_for(self, conversation: str, shell: str = "cmd") -> str:
        """O comando que retoma a conversa fora do launcher, com a conexão e o modelo escolhidos na tela, escrito para
        o `shell` (cmd ou powershell)."""
        if not (self.project and self.gateway and self.selected_model and self.catalog):
            raise LauncherError("Escolha projeto, conexão e modelo para montar o comando.")
        return resume_command(self.store.home, self.gateway, self.project["path"], self.cli, self.selected_model.id,
                              self.catalog, conversation, shell)

    def warn_resume_window(self, conversation: str | None):
        """Avisa quando a conversa que será retomada já enche a janela do modelo escolhido."""
        warning = resume_window_warning(self.store.home, self.conversation_list(), conversation, self.selected_model,
                                        bool(self.catalog and self.catalog.verified))
        if warning:
            self.show_warning(warning, origin="janela")
        else:
            self.clear_error("janela")

    @Slot()
    def refresh_catalog(self, force=False):
        if self.refreshing or not self.gateway:
            return
        self.refreshing = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Verificando…")
        self.connection.setText("Verificando conexão…")
        self.render_empty_models()
        self.update_actions()
        self.refresh_task = RefreshTask(self.store.home, self.gateway.connection["id"], bool(force))
        run_task(self.refresh_task, self.finish_refresh)

    @Slot(object, str)
    def finish_refresh(self, result, connection):
        self.refreshing = False
        self.startup_mark("catalogo")
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Atualizar")
        if not self.gateway or connection != self.gateway.connection["id"]:
            self.update_actions()
            return  # resposta de uma conexão que já foi trocada
        catalog, error = (None, str(result) or type(result).__name__) if isinstance(result, Exception) else (result, "")
        self.online = not error
        self.connection.setText(f"✓ Conectado · {self.gateway.name}" if self.online else f"✕ Indisponível · {self.gateway.name}")
        self.connection.setProperty("offline", not self.online)
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)
        if catalog is not None:
            self.catalog = catalog
            if catalog.news():
                self.show_info(catalog.news(), origin="catalogo-novidades")
                self.gateway.mark_news_seen(catalog)
        elif self.catalog is not None:
            self.catalog.verified = False
        if error:
            self.show_error(f"Não foi possível acessar a conexão. Verifique o host e clique em Atualizar. {error}",
                            origin="connection")
        else:
            self.clear_error("connection")
        self.populate_providers()
        self.populate_sources()
        self.populate_models()

    def launch_session(self, resume=False, conversation=None):
        if not self.project or not self.gateway or not self.selected_model or not self.online or self.refreshing:
            return
        self.clear_transient()
        if resume:
            self.warn_resume_window(conversation)
        else:
            self.clear_error("janela")
        harness = registry.get(self.cli)
        if installed_path(self.cli) is None:
            self.show_error(self.missing_reason(harness, bool(harness.install_steps())), origin="cli:instalacao")
            self.update_actions()
            return
        if harness.KIND != "terminal":
            self.open_outside_terminal(harness)
            return
        bypass_permissions = self.bypass_checkbox.isChecked()
        try:
            process = start_session(self.store, self.project, self.cli, self.selected_model.id, resume,
                                    bypass_permissions=bypass_permissions, connection=self.gateway.connection["id"],
                                    **({"conversation": conversation} if conversation else {}))
        except LauncherBusy as error:
            self.show_info(str(error), origin="sessao")
            return
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))
            return
        title = session_title(self.project, self.cli, self.store.home)
        if process is not None:
            self.sessions.append((process, title))
            self.sync_session_timer()
        self.bypass_checkbox.setChecked(self.bypass_default)
        try:
            fresh = Store(self.store.home)
            project = fresh.find(self.project["id"])
            fresh.remember(project, fresh.find_connection(self.gateway.connection["id"]), self.cli,
                           base_model(self.selected_model.id))
            self.store = fresh
            self.project = project
            self.populate_projects(project["id"])  # o projeto aberto sobe para o topo da lista
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"A janela abriu, mas não foi possível salvar a preferência: {error}")
        mode = " · bypass ativo" if bypass_permissions else ""
        self.set_footer("sessao", f"{self.quota_note()}Janela aberta: {title}{mode}. A janela verifica a conexão antes de iniciar.")
        note = self.shared_resume_note() if resume else None
        if note:
            self.show_info(note, origin="pasta")
        if terminal.find(ROOT) is None:
            self.ensure_terminal(opened_in_console=True)

    def install_cli(self, cli=None):
        if self.installing_cli:
            return
        cli = cli or self.cli
        self.installing_cli = cli
        self.clear_info("cli:instalacao")
        self.cli_buttons[cli].set_progress(None, "Preparando")
        self.set_footer("instalacao", f"Instalando {registry.get(cli).LABEL} sem admin…", busy=True)
        self.update_actions()
        self.install_task = InstallCliTask(cli)
        self.install_task.signals.progress.connect(self.show_install_progress)
        run_task(self.install_task, self.finish_install_cli)

    @Slot(object, str)
    def show_install_progress(self, fraction, stage):
        if self.installing_cli:
            self.cli_buttons[self.installing_cli].set_progress(fraction, stage)

    @Slot(object, str)
    def finish_install_cli(self, found, log):
        cli = self.installing_cli or self.cli
        self.installing_cli = False
        forget_installed()
        self.cli_buttons[cli].finish_install(bool(found))
        if found:
            self.clear_error("cli:instalacao")
            self.set_footer("instalacao", f"Instalado: {found}")
            self.refresh_card_states()
            if cli == self.cli:
                self.select_cli(cli)
            if cli in clis.NAMES:
                self.start_cli_check()
        else:
            self.set_footer("instalacao", "A instalação não terminou; o motivo está no erro logo abaixo.")
            self.show_error(log or f"Não foi possível instalar o {registry.get(cli).LABEL}.", origin="cli:instalacao")
        self.update_actions()

    def watch_sessions(self) -> None:
        """Vigia a pasta de sessões (criada se faltar). Sem como vigiá-la, o laço de sessões fica sempre ligado."""
        try:
            keys.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not self.session_watcher.directories():
            self.watching_sessions = self.session_watcher.addPath(str(keys.SESSIONS_DIR))

    def sessions_changed(self, _folder: str = "") -> None:
        """Sessão registrada ou removida por qualquer processo: marca a lista e liga ou desliga o laço."""
        self.watch_sessions()
        self.check_sessions()

    def sync_session_timer(self, registered: int = 0) -> None:
        """O laço de 1,5 s só roda com sessão registrada ou aberta por esta janela e ainda sem registro; a pasta vigiada o
        religa quando a primeira sessão aparece."""
        wanted = bool(registered or self.sessions) or not self.watching_sessions
        if wanted and not self.refresh_timer.isActive():
            self.refresh_timer.start(SESSION_POLL_MS)
        elif not wanted and self.refresh_timer.isActive():
            self.refresh_timer.stop()

    def check_sessions(self):
        registered = self.mark_open_sessions()
        finished = [(process, title) for process, title in self.sessions if process.poll() is not None]
        self.sessions = [(process, title) for process, title in self.sessions if process.poll() is None]
        if finished:
            message = (f"Sessão encerrada: {finished[-1][1]} · "
                       f"{plural(len(self.sessions), 'janela ainda aberta', 'janelas ainda abertas')}")
            self.set_footer("sessao", message)
            self.notify_session_ended(message)
        self.sync_tray()
        self.sync_session_timer(registered)

    def notify_session_ended(self, message: str) -> None:
        """Balão do Windows com a mensagem do rodapé, só com o launcher minimizado ou sem foco: à vista, o rodapé basta."""
        if self.isActiveWindow() and not self.isMinimized():
            return
        self.show_tray_notice(message)

    def show_tray_notice(self, message: str) -> bool:
        """Balão do Windows sem tocar na janela (nem foco, nem restauração). False sem bandeja no Windows: nada aparece."""
        self.tray_notified = True
        self.sync_tray()
        if self.tray is None:
            self.tray_notified = False
            return False
        self.tray.showMessage(APP_NAME, message, self.windowIcon(), TRAY_NOTICE_MS)
        self.tray_timer.start()
        return True

    def sync_tray(self) -> None:
        """O ícone da bandeja só existe com sessão aberta ou aviso ainda a mostrar. Sem bandeja no Windows não cria nada."""
        wanted = bool(self.sessions) or self.tray_notified
        if wanted and self.tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            self.tray.show()
        elif not wanted and self.tray is not None:
            self.tray.hide()
            self.tray.deleteLater()
            self.tray = None

    def release_tray(self) -> None:
        """Fim do prazo do aviso: o ícone sai se não há mais sessão aberta."""
        self.tray_notified = False
        self.sync_tray()

    def problem_report(self, message: str) -> str:
        """Aviso + contexto para diagnosticar fora daqui (sem chaves: só versões, caminhos e estado do Terminal)."""
        import os
        import platform
        path = os.environ.get("PATH", "")
        alias = terminal.store_alias()
        return "\n".join([
            f"{APP_NAME} {VERSION} — problema relatado pela interface", "", message.strip(), "",
            f"Instalação: {ROOT}",
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Windows: {platform.platform()}",
            f"Windows Terminal: find={terminal.find(ROOT)} | alias da Store existe={os.path.lexists(alias)} ({alias})",
            f"PATH do processo: {len(path)} caracteres, contém WindowsApps={'WindowsApps' in path}",
            f"Atualização automática: {self.update_status}",
        ])

    def copy_models(self):
        QApplication.clipboard().setText(models_text(self.records, self.catalog))
        self.confirm_copy(f"{plural(len(self.records), 'modelo copiado', 'modelos copiados')}: cole numa planilha, cada valor vai para a sua coluna.")

    def model_menu(self, position):
        """Menu da tabela de modelos (botão direito ou tecla Menu): Copiar ID e Testar modelo (1 token), do modelo sob
        o cursor ou, sem linha apontada, do selecionado; sem modelo nenhum as ações ficam desabilitadas."""
        row = self.model_table.rowAt(position.y())
        if 0 <= row < len(self.records):
            self.model_table.selectRow(row)
        menu = DangerMenu(self)
        menu.addAction("Copiar ID", self.copy_model_id).setEnabled(self.selected_model is not None)
        menu.addAction("Testar modelo (1 token)…", self.ask_model_test).setEnabled(
            bool(self.selected_model and self.gateway and not self.model_testing))
        menu.exec(self.model_table.viewport().mapToGlobal(position))

    def copy_model_id(self):
        """Copia o ID completo do modelo selecionado, exatamente como o host o informa."""
        if self.selected_model is None:
            return
        QApplication.clipboard().setText(self.selected_model.id)
        self.confirm_copy("ID copiado: " + self.selected_model.id)

    def ask_model_test(self):
        """Testa o modelo selecionado com uma chamada real de 1 token, só depois de mostrar o custo em tokens e de o
        usuário confirmar (Cancelar é o padrão). A chamada corre em segundo plano; um teste por vez."""
        record = self.selected_model
        if record is None or self.gateway is None or self.model_testing:
            return
        try:
            protocol = model_probe.protocol_of(self.gateway, record)
        except LauncherError as error:
            self.show_error(str(error), origin=MODEL_TEST)
            return
        if not self.confirm_model_test(model_probe.cost_text(model_probe.estimate_cost(record, protocol))):
            return
        self.model_testing = True
        self.clear_error(MODEL_TEST)
        self.set_footer(MODEL_TEST, f"Testando {record.id}…", busy=True)
        self.model_test_task = ModelTestTask(self.store.home, self.gateway.connection["id"], record.id, protocol)
        run_task(self.model_test_task, self.finish_model_test)

    def confirm_model_test(self, text: str) -> bool:
        """Caixa com o custo do teste e os botões Testar e Cancelar; só Testar confirma, e Esc ou fechar cancelam."""
        box = QMessageBox(QMessageBox.Icon.Question, "Testar modelo (1 token)", text, parent=self)
        accept = box.addButton("Testar", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        confirmed = box.clickedButton() == accept
        box.deleteLater()
        return confirmed

    @Slot(object, str)
    def finish_model_test(self, result, model):
        """Resultado do teste: OK com latência e tokens do host no rodapé; erro (do host, do endpoint ou da subida do
        endpoint) com a causa real na faixa de erros."""
        self.model_testing = False
        if not isinstance(result, Exception) and result.ok:
            self.clear_error(MODEL_TEST)
            self.set_footer(MODEL_TEST, f"{model}: {model_probe.result_text(result)}")
            return
        self.end_footer(MODEL_TEST)
        self.show_error(f"Teste de {model}: {result if isinstance(result, Exception) else result.error}",
                        origin=MODEL_TEST)

    def copy_notice(self):
        QApplication.clipboard().setText(self.problem_report(self.notice.toolTip() or self.notice.text()))
        copied = "Erros copiados" if len(self.errors) > 1 else "Erro copiado"
        self.confirm_copy(f"{copied} com o contexto: cole num chat de IA ou mande para o suporte.")

    def confirm_copy(self, text):
        """Confirmação de cópia dura o tempo de um informativo: depois disso não volta junto de outro informativo."""
        self.set_footer("copia", text)
        self.copied_timer.start()

    def ensure_terminal(self, opened_in_console: bool = False):
        """Sem Terminal: instala sozinho em segundo plano; só avisa de erro se todas as formas falharem."""
        if terminal.find(ROOT):
            self.clear_error("terminal")
            return
        record_terminal_problem(self.problem_report("Windows Terminal não encontrado pela interface."))
        if opened_in_console:
            self.show_info("Esta sessão abriu em um console separado porque o Windows Terminal não foi encontrado. "
                           "Instalando o Terminal agora, sem admin; as próximas sessões abrem nele.", origin="console")
        if self.terminal_installing:
            return
        self.terminal_installing = True
        self.terminal_task = TerminalTask(ROOT)
        run_task(self.terminal_task, self.finish_terminal)

    @Slot(object, str)
    def finish_terminal(self, found, log):
        self.terminal_installing = False
        self.clear_info("console")  # o "instalando…" acabou; o resultado vem a seguir
        if found:
            self.clear_error("terminal")
            self.show_info(f"Windows Terminal pronto ({found}). As próximas sessões abrem nele.", origin="console")
            return
        details = "\n".join(line.strip() for line in log.splitlines() if "[falhou]" in line or "ambiente:" in line)
        self.show_error("Windows Terminal indisponível: nenhuma forma de instalação funcionou nesta máquina, então as "
                        "sessões abrem em consoles separados. Use \"Copiar erro\" para ver o motivo."
                        + (f"\n{details}" if details else ""), origin="terminal")
        record_terminal_problem(self.problem_report(log or "Instalação do Windows Terminal falhou sem detalhes."))

    def show_info(self, message, origin=GENERAL_ERROR):
        """Aviso informativo (versão nova, atualizado, adiado): sem o botão Copiar erro."""
        self.infos[origin] = message
        self.render_infos()

    def render_infos(self):
        text = "\n".join(self.infos.values())
        self.refresh_info_strip(text)

    def show_error(self, message, origin=GENERAL_ERROR):
        """Erro na faixa de erros, com Copiar erro. Origens diferentes convivem; a mesma origem substitui o seu erro
        anterior (e o aviso dela). O erro avulso (origem geral) é de ações diferentes: acumula, sem repetir, e só some
        quando uma nova ação o limpa."""
        previous = self.errors.get(origin)
        if origin == GENERAL_ERROR and previous:
            message = previous if message in previous.split("\n") else f"{previous}\n{message}"
        self.errors[origin] = message
        self.warnings.pop(origin, None)
        self.render_errors()

    def show_warning(self, message, origin=GENERAL_ERROR):
        """Aviso na faixa âmbar, sem Copiar erro: algo não aconteceu, mas o que já funcionava segue funcionando (a CLI
        que não atualizou continua na versão de antes). Some pelas mesmas regras do erro da mesma origem."""
        self.warnings[origin] = message
        self.errors.pop(origin, None)
        self.render_errors()

    def render_errors(self):
        """Erro em cima (com Copiar erro, que copia só os erros), aviso embaixo, sem botão."""
        for strip, messages in ((self.error_strip, self.errors), (self.warning_strip, self.warnings)):
            text = "\n".join(messages.values())
            strip.text.setText(breakable_path(text))
            strip.text.setToolTip(text)
            strip.setVisible(bool(text))
        self.copy_error.setText("Copiar erros" if len(self.errors) > 1 else "Copiar erro")
        self.problems.setVisible(bool(self.errors or self.warnings))

    def clear_error(self, origin=None):
        """Sem origem, apaga todos os erros e avisos; com origem, só o que ela causou (o resto continua valendo)."""
        if origin is None:
            self.errors.clear()
            self.warnings.clear()
        else:
            self.errors.pop(origin, None)
            self.warnings.pop(origin, None)
        self.render_errors()

    def clear_transient(self, *origins):
        """Ação do usuário que não resolve os erros persistentes: some o erro e o aviso avulsos, o informativo e as
        origens que a ação refaz (ex.: trocar de conexão refaz o erro da conexão)."""
        for origin in (GENERAL_ERROR, *origins):
            self.errors.pop(origin, None)
            self.warnings.pop(origin, None)
        self.render_errors()
        self.clear_info()

    def clear_info(self, origin=None):
        """Sem origem, apaga todos os informativos; com origem, só o dela."""
        self.cli_announced = ()  # o mesmo aviso de versão volta a aparecer na próxima verificação
        if origin is None:
            self.infos.clear()
        else:
            self.infos.pop(origin, None)
        self.render_infos()

    def add_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Escolha a pasta do projeto")
        if not folder:
            return
        try:
            fresh = Store(self.store.home)
            try:
                project = fresh.add(folder)
            except LauncherError as error:
                if "esse nome" not in str(error):
                    raise
                name, accepted = QInputDialog.getText(self, "Nome do projeto", "Já existe esse nome. Use outro:")
                if not accepted:
                    return
                project = fresh.add(folder, name)
            self.store = fresh
            self.project_search.clear()
            self.populate_projects(project["id"])
            self.clear_transient()
            warning = self.shared_warning(project["path"])
            if warning:
                self.show_info(warning, origin="pasta")
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))

    def import_projects(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importar projetos de outro config.json", str(Path.home()), "Configuração (*.json)")
        if not path:
            return
        try:
            fresh = Store(self.store.home)
            added, skipped = fresh.import_legacy(Path(path))
            self.store = fresh
            self.populate_projects(self.project["id"] if self.project else None)
            self.set_footer("importacao", f"{plural(added, 'projeto importado', 'projetos importados')}; "
                                          f"{plural(skipped, 'já existente ou inválido', 'já existentes ou inválidos')}. "
                                          "Nenhuma credencial importada.")
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))

    def project_menu(self, position):
        """Menu do projeto pelo botão direito ou pelo teclado (tecla Menu, Shift+F10), que abre no projeto selecionado."""
        item = self.project_list.itemAt(position)
        if item is None:
            item = self.project_list.currentItem()
            if item is None:
                return
            position = self.project_list.visualItemRect(item).center()
        self.project_list.setCurrentItem(item)
        menu = DangerMenu(self)
        self.add_project_actions(menu, context=True)
        menu.exec(self.project_list.viewport().mapToGlobal(position))

    def add_project_actions(self, menu, context):
        """Abrir pasta, Editar e Remover do projeto atual. No menu da lista (context) os rótulos são curtos, porque o
        projeto já está apontado, e entra Abrir no VS Code quando ele está instalado."""
        menu.addAction("Abrir pasta" if context else "Abrir pasta do projeto", self.open_project)
        if context and (code := vscode.find_code()):
            menu.addAction("Abrir no VS Code", lambda: self.open_in_vscode(code))
        if context and self.project and (open_sessions := self.open_sessions_of(self.project["id"])):
            menu.addAction("Ver a tela da sessão aberta…", lambda: self.show_session_screen(open_sessions))
        menu.addAction("Editar…" if context else "Editar projeto…", self.edit_project)
        if context and self.project:
            menu.addAction("Copiar caminho", lambda: QApplication.clipboard().setText(self.project["path"]))
        if context and self.project:
            menu.addAction("Desafixar" if self.store.is_pinned(self.project) else "Fixar no topo", self.toggle_pinned)
        menu.add_danger_action("Remover do launcher…", self.remove_project)

    @staticmethod
    def open_sessions_of(project_id: str) -> list[dict]:
        return [session for session in keys.sessions() if session.get("project_id") == project_id]

    def show_session_screen(self, open_sessions: list[dict]) -> None:
        """Mostra o que a sessão aberta exibe agora; só o clique em Enviar manda teclas, e a janela dela fica onde está."""
        SessionScreenDialog(open_sessions, self).exec()

    def toggle_pinned(self) -> None:
        """Fixa o projeto atual no topo da lista, ou o desafixa se já está fixado."""
        if not self.project:
            return
        try:
            self.store.set_pinned(self.project, not self.store.is_pinned(self.project))
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível salvar o projeto fixado: {error}")
            return
        self.populate_projects(self.project["id"])

    def edit_project(self):
        if not self.project:
            return
        dialog = ProjectDialog(self.project, self)
        while dialog.exec():
            try:
                fresh = Store(self.store.home)
                project = fresh.update(self.project["id"], dialog.name.text(), dialog.path.text())
                self.store = fresh
                self.populate_projects(project["id"])
                self.clear_transient()
                return
            except (LauncherError, OSError, ValueError) as error:
                QMessageBox.warning(dialog, "Editar projeto", str(error))

    def remove_project(self):
        if not self.project:
            return
        answer = QMessageBox.question(self, "Remover cadastro", f"Remover {self.project['name']} do launcher?\n\nA pasta, os arquivos e as sessões não serão apagados.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            fresh = Store(self.store.home)
            fresh.remove(self.project["id"])
            self.store = fresh
            self.populate_projects()
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))

    def open_project(self):
        if self.project and not QDesktopServices.openUrl(QUrl.fromLocalFile(self.project["path"])):
            self.show_error("Não foi possível abrir a pasta. Verifique se ela ainda existe.")

    def open_in_vscode(self, code: str):
        """A pasta no VS Code do usuário (o perfil dele, não o "Agent Launcher" das conexões)."""
        if not self.project:
            return
        try:
            vscode.start_code(code, project_path(self.project["path"]))
        except (LauncherError, OSError) as error:
            self.show_error(f"Não foi possível abrir no VS Code: {error}")

    def open_outside_terminal(self, harness):
        """VS Code e Copilot app: o adaptador configura o destino (perfil, provider) e o abre na pasta do projeto."""
        def confirm(question: str) -> bool:
            answer = QMessageBox.question(self, harness.LABEL, question,
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            return answer == QMessageBox.StandardButton.Yes
        try:
            message = harness.open(self.store, self.project, self.gateway, self.selected_model.id, self.catalog, confirm)
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))
            return
        try:
            fresh = Store(self.store.home)
            project = fresh.find(self.project["id"])
            fresh.remember(project, fresh.find_connection(self.gateway.connection["id"]), self.cli,
                           base_model(self.selected_model.id))
            self.store, self.project = fresh, project
            self.populate_projects(project["id"])
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"{harness.LABEL} aberto, mas não foi possível salvar a preferência: {error}")
        self.set_footer("sessao", self.quota_note() + message)

    def quota_mark(self) -> str:
        """Mensagem da marca de cota esgotada da conexão em uso ("" sem marca ou sem conexão)."""
        try:
            return quota_warning(self.store.find_connection(self.gateway.connection["id"])) if self.gateway else ""
        except (LauncherError, OSError, ValueError):
            return ""

    def quota_note(self) -> str:
        """Aviso que não bloqueia a abertura (a cota pode ter voltado), no início da faixa: ela corta o meio do texto."""
        return "Aviso: cota esgotada nesta conexão (veja o Diagnóstico). " if self.quota_mark() else ""

    # Perfis de trabalho

    def populate_profiles(self):
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        self.profile_picker.blockSignals(True)
        self.profile_picker.clear()
        for item in profiles.list_profiles(self.profile_base):
            self.profile_picker.addItem(item["name"], item["id"])
            if item["color"]:
                self.profile_picker.setItemIcon(self.profile_picker.count() - 1, icon("folder", item["color"], fixed=True))
        self.profile_picker.addItem("Novo perfil…", NEW_PROFILE)
        self.profile_picker.setCurrentIndex(max(0, self.profile_picker.findData(current["id"])))
        self.profile_picker.blockSignals(False)
        extra = current["id"] != profiles.DEFAULT
        self.rename_profile_action.setEnabled(extra)
        self.remove_profile_action.setEnabled(extra)
        self.profile_strip.setStyleSheet(f"background: {current['color']};" if current["color"] else "")
        self.profile_strip.setVisible(bool(current["color"]))
        self.setWindowTitle(f"{APP_NAME} — {current['name']}" if extra else APP_NAME)

    @property
    def profile_id(self) -> str:
        return profiles.profile_of_home(self.store.home, self.profile_base)["id"]

    def pick_profile(self, index):
        identifier = self.profile_picker.itemData(index)
        if identifier == NEW_PROFILE:
            self.populate_profiles()  # volta a seleção para o perfil atual se o usuário cancelar
            self.new_profile()
        elif identifier and identifier != self.profile_id:
            self.switch_profile(identifier)

    def switch_profile(self, profile_id: str):
        """Troca o perfil: grava como ativo e recarrega projetos, conexões e catálogo (sessões abertas continuam)."""
        try:
            profiles.set_active(profile_id, self.profile_base)
            store = Store(profiles.home_of(profile_id, self.profile_base))
        except (LauncherError, OSError, ValueError) as error:
            self.populate_profiles()
            self.show_error(f"Não foi possível abrir o perfil: {error}")
            return
        self.store = store
        self.project, self.gateway, self.catalog, self.online, self.selected_model = None, None, None, False, None
        self.clear_transient("connection", "perfil")
        self.connection_picker.blockSignals(True)
        self.connection_picker.clear()
        self.connection_picker.blockSignals(False)
        self.project_search.clear()
        for action, key in ((self.bypass_preference, "bypass_default"), (self.update_preference, "auto_update"),
                        (self.cli_update_preference, "cli_auto_update")):
            action.blockSignals(True)
            action.setChecked(self.store.preference(key))
            action.blockSignals(False)
        self.populate_profiles()
        self.populate_projects(self.store.data["last_project"])
        self.populate_connections()
        name = profiles.profile_of_home(self.store.home, self.profile_base)["name"]
        self.set_footer("perfil", f"Perfil {name}: só os projetos e as conexões dele aparecem aqui.")

    def new_profile(self):
        name, accepted = QInputDialog.getText(self, "Novo perfil", "Nome do perfil (ex.: o cliente):")
        if not accepted or not name.strip():
            return
        try:
            profile = profiles.create(name, self.profile_base)
        except (LauncherError, OSError) as error:
            self.show_error(str(error))
            return
        self.switch_profile(profile["id"])

    def rename_profile(self):
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        if current["id"] == profiles.DEFAULT:
            return
        name, accepted = QInputDialog.getText(self, "Renomear perfil", "Novo nome:", text=current["name"])
        if not accepted or not name.strip():
            return
        try:
            profiles.rename(current["id"], name, self.profile_base)
        except (LauncherError, OSError) as error:
            self.show_error(str(error))
            return
        self.populate_profiles()

    def color_profile(self, color: str | None = None):
        """Menu do perfil → Cor do perfil… (QColorDialog). `color` (#RRGGBB) pula o diálogo (testes)."""
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        if color is None:
            from PySide6.QtWidgets import QColorDialog
            start = current["color"] or (registry.get("claude").TAB_COLOR if current["id"] == profiles.DEFAULT
                                         else profiles.COLORS[0])
            chosen = QColorDialog.getColor(QColor(start), self, "Cor do perfil")
            if not chosen.isValid():
                return
            color = chosen.name()
        try:
            profiles.set_color(current["id"], color, self.profile_base)
        except (LauncherError, OSError) as error:
            self.show_error(str(error))
            return
        self.populate_profiles()

    def show_instructions(self):
        """Uma janela de instruções só: o clique seguinte traz a que já existe para a frente."""
        if getattr(self, "instructions_dialog", None) is None:
            self.instructions_dialog = InstructionsDialog(self)
        self.instructions_dialog.show()
        self.instructions_dialog.raise_()
        self.instructions_dialog.activateWindow()

    def show_release_notes_once(self):
        """Na primeira abertura de uma versão, mostra a nota dela ("O que mudou") numa janela do launcher e guarda a versão
        vista na raiz dos perfis. Sem a nota na instalação (checkout de desenvolvimento) não faz nada; sem conseguir
        guardar a versão vista, avisa em vez de repetir a janela a cada abertura."""
        seen_file = self.profile_base / RELEASE_SEEN
        try:
            seen = seen_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            seen = ""
        except OSError as error:
            self.show_warning(f"Não foi possível ler {RELEASE_SEEN}: {error}", origin="novidades")
            return
        if seen == VERSION or not release_notes_available():
            return
        try:
            atomic_text(seen_file, VERSION)
        except OSError as error:
            self.show_warning(f"Não foi possível guardar a versão vista em {RELEASE_SEEN}: {error}", origin="novidades")
            return
        self.release_notes_dialog = InstructionsDialog(self, notes=True)
        self.release_notes_dialog.show()

    def show_usage(self):
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        self.usage_dialog = UsageDialog(self, self.store.home, current["name"], self.gateway)
        self.usage_dialog.limit_saved.connect(self.limit_changed)
        self.usage_dialog.show()

    def check_limit(self):
        """Confere em segundo plano o gasto do mês frente ao teto do perfil (sem teto não calcula nada) e, à parte, o
        limite por fonte que o conector da conexão informa."""
        self.check_source_limits()
        if self.limit_checking:
            return
        if self.store.monthly_limit() is None:
            self.clear_error("teto")
            return
        self.limit_checking = True
        self.limit_task = UsageTask(self.store.home)
        run_task(self.limit_task, self.finish_limit_check)

    @Slot(object, str)
    def finish_limit_check(self, result, _unused=""):
        """Faixa âmbar a partir de 80% do teto; só avisa, nada é bloqueado. O teto é lido de novo aqui: pode ter sido
        mudado ou desligado enquanto o cálculo corria."""
        self.limit_checking = False
        limit = self.store.monthly_limit()
        if limit is None:
            self.clear_error("teto")
        elif isinstance(result, Exception):
            self.show_warning(f"Não foi possível conferir o teto de gasto do mês: {result}", origin="teto")
        else:
            status = usage.limit_status(result, limit)
            warning = usage.limit_warning(status)
            if warning:
                self.show_warning(warning, origin="teto")
                self.notify_limit(result["month"], status, warning)
            else:
                self.clear_error("teto")

    def notify_level(self, level: int, noticed: int, warning: str, save, failed: str, origin: str) -> None:
        """Balão da bandeja com `warning` se `level` passou do nível já avisado (`noticed`); só depois de o balão sair `save()`
        guarda o nível, e a falha em guardá-lo vira aviso na faixa (`failed`) em vez de repetir o balão sem fim."""
        if level <= noticed or not self.show_tray_notice(warning):
            return
        try:
            save()
        except (LauncherError, OSError) as error:
            self.show_warning(f"{warning}\n{failed}: {error}", origin=origin)

    def notify_limit(self, month: str, status: dict, warning: str) -> None:
        """Balão da bandeja para cada nível do teto (80%, 100%) ainda não avisado no mês para o limite vigente, com o
        texto da faixa âmbar; mudar o limite recomeça do nível 0. O nível só é guardado depois de o balão sair: sem
        bandeja no Windows ele fica para a próxima checagem."""
        level, limit = LIMIT_LEVELS[status["level"]], status["amount"]
        self.notify_level(level, self.store.limit_notice(month, limit), warning,
                          lambda: self.store.set_limit_notice(month, level, limit),
                          "Não foi possível guardar o aviso do teto", "teto")

    def check_source_limits(self) -> None:
        """Lê em segundo plano o custo do mês por fonte, só se a conexão da janela declara fontes e oferece a rota
        `costs` do conector; sem isso não lê nada e some o aviso de falha de uma leitura anterior."""
        gateway = self.gateway
        if gateway is None or not gateway.offers_costs:
            self.clear_error("teto-fontes")
            return
        if self.source_limit_checking:
            return
        self.source_limit_checking = True
        task = self.source_limit_task = SourceCostsTask(gateway, usage.recent_months(1)[0])
        run_task(task, lambda result, _unused="": self.finish_source_limit_check(task, result))

    def finish_source_limit_check(self, task: "SourceCostsTask", result) -> None:
        """Balão por fonte a partir de 80% do limite dela, com o nome da conexão quando o perfil tem mais de uma; a falha
        do conector vira aviso na faixa com a mensagem dele, nunca balão. A resposta de uma conexão que deixou de ser a
        da janela é descartada."""
        if task is self.source_limit_task:
            self.source_limit_checking = False
        if self.gateway is None or task.gateway.connection["id"] != self.gateway.connection["id"]:
            return
        if isinstance(result, Exception):
            self.show_warning(f"Não foi possível conferir o limite por fonte do mês: {result}", origin="teto-fontes")
            return
        self.clear_error("teto-fontes")
        named = task.gateway.name if len(self.store.connections) > 1 else None
        for status in usage.source_limit_statuses(result):
            warning = usage.source_limit_warning(status, named)
            if warning:
                self.notify_source_limit(task.month, task.gateway.connection["id"], status, warning)

    def notify_source_limit(self, month: str, connection_id: str, status: dict, warning: str) -> None:
        """Balão da bandeja para cada nível (80%, 100%) do limite da fonte da conexão ainda não avisado no mês para o
        limite vigente dela; o nível só é guardado depois de o balão sair."""
        source, level, limit = status["source"], LIMIT_LEVELS[status["level"]], status["amount"]
        self.notify_level(level, self.store.source_notice(month, connection_id, source, limit), warning,
                          lambda: self.store.set_source_notice(month, connection_id, source, level, limit),
                          "Não foi possível guardar o aviso do limite da fonte", "teto-fontes")

    def limit_changed(self):
        """O limite mudou no Uso do mês: relê a preferência e refaz o aviso da faixa."""
        try:
            self.store = Store(self.store.home)
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível ler o limite mensal: {error}")
            return
        self.check_limit()

    def remove_profile(self):
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        if current["id"] == profiles.DEFAULT:
            return
        answer = QMessageBox.question(self, "Remover perfil", f"Remover o perfil {current['name']}?\n\n"
                                      "As pastas dos projetos não são tocadas. Sessões abertas continuam.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        wipe = QMessageBox.question(self, "Remover perfil", f"Apagar também os dados do perfil {current['name']} "
                                    "(lista de projetos, conexões e chaves salvas dele)?\n\n"
                                    "Não: os dados ficam guardados na pasta do perfil.",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                    | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.No)
        if wipe == QMessageBox.StandardButton.Cancel:
            return
        clean = self.ask_clean_profile_destinations(current)
        if clean is None:
            return
        delete_data = wipe == QMessageBox.StandardButton.Yes
        if not clean:
            self.finish_remove_profile(current, delete_data, [])
            return
        # Limpeza em segundo plano (fechar o Copilot app e esperar o banco pode levar ~40 s). A pergunta já foi feita
        # aqui, na thread da interface: a tarefa confirma sozinha e nunca abre QMessageBox fora dela.
        self.set_profile_busy(True)
        self.set_footer("perfil", f"Removendo as conexões do perfil {current['name']} do Copilot app e do VS Code… "
                        "(pode levar alguns segundos)", busy=True)
        self.cleanup_task = CleanupTask(self.store.home)
        run_task(self.cleanup_task, lambda messages, _text: self.finish_remove_profile(current, delete_data, messages))

    def set_profile_busy(self, busy: bool):
        """Durante a limpeza do perfil: seletor e menu do perfil travados (o perfil ainda existe até o fim)."""
        self.removing_profile = busy
        self.profile_picker.setEnabled(not busy)
        self.profile_options.setEnabled(not busy)

    def finish_remove_profile(self, current: dict, delete_data: bool, messages):
        from agent_launcher.harnesses.cleanup import failure
        messages = [failure("Limpeza nos destinos", messages)] if isinstance(messages, Exception) else list(messages or [])
        self.set_profile_busy(False)
        self.end_footer("perfil")  # o "Removendo…" acabou, com ou sem mensagem de resultado
        self.cleanup_task = None
        from agent_launcher.harnesses.cleanup import release_profile_endpoints
        try:
            messages += release_profile_endpoints(profiles.home_of(current["id"], self.profile_base))
        except (LauncherError, OSError) as error:
            messages.append(failure("Endpoints do perfil", error))
        try:
            profiles.remove(current["id"], delete_data, self.profile_base)
        except (LauncherError, OSError) as error:
            done, failed = split_cleanup(messages)
            self.show_error(" ".join([*failed, str(error)]), origin="perfil")
            if done:
                self.show_info(" ".join(done), origin="perfil")
            return
        self.switch_profile(profiles.DEFAULT)
        if messages:  # o que foi feito (informativo) e o que falhou (erro, ex.: "VS Code: arquivo preso"), sem bloquear
            self.set_footer("perfil", " ".join(messages))
            done, failed = split_cleanup(messages)
            if failed:
                self.show_error(" ".join(failed), origin="perfil")
            if done:
                self.show_info(" ".join(done), origin="perfil")

    def ask_clean_profile_destinations(self, current: dict) -> bool | None:
        """Pergunta se tira as conexões do perfil do Copilot app, do VS Code e do endpoint local.
        True = limpar, False = só remover, None = o usuário cancelou a remoção."""
        if not self.store.connections:
            return False
        from agent_launcher.ui.connections import CLEAN_NOTE
        clean = QMessageBox.question(self, "Remover perfil", f"Remover também do Copilot app e do VS Code as conexões "
                                     f"do perfil {current['name']} (e parar o endpoint local delas)?\n\n{CLEAN_NOTE}",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                     | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes)
        if clean == QMessageBox.StandardButton.Cancel:
            return None
        return clean == QMessageBox.StandardButton.Yes

    def package_question(self, kind: str, *args):
        """Perguntas da importação de perfil: conector diferente do instalado e chave (oculta) da conexão."""
        if kind == "replace_connector":
            return QMessageBox.question(self, "Importar perfil", f"O conector {args[0]} do pacote é diferente do já "
                                        "instalado. Substituir pelo do pacote?\n\nNão: mantém o instalado.",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                        QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if kind == "secret":
            connection, field = args
            value, accepted = QInputDialog.getText(self, "Importar perfil", f"{field} da conexão {connection}:\n"
                                                   "(Cancelar deixa a conexão sem chave; informe depois em Conexões)",
                                                   QLineEdit.EchoMode.Password)
            return value.strip() if accepted and value.strip() else None
        return None

    def import_profile(self, path: str | None = None):
        """Importa um .alprofile (menu do perfil ou launcher.pyw --import) e troca para o perfil criado."""
        from agent_launcher.domain import profile_package
        if not path:
            path, _filter = QFileDialog.getOpenFileName(self, "Importar perfil", str(Path.home()),
                                                        "Perfil do Agent Launcher (*.alprofile)")
            if not path:
                return None
        try:
            result = profile_package.import_profile(self.profile_base, Path(path), ask=self.package_question)
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível importar o perfil: {error}")
            return None
        self.switch_profile(result["profile"]["id"])
        if result["warnings"]:
            QMessageBox.information(self, "Importar perfil", profile_package.describe(result))
        else:
            self.set_footer("perfil", profile_package.describe(result))
        return result

    def export_profile(self, path: str | None = None):
        from agent_launcher.domain import profile_package
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        if not path:
            suggested = str(Path.home() / f"{current['name']}{profile_package.EXTENSION}")
            path, _filter = QFileDialog.getSaveFileName(self, "Exportar perfil", suggested,
                                                        "Perfil do Agent Launcher (*.alprofile)")
            if not path:
                return None
        try:
            result = profile_package.export_profile(self.profile_base, current["id"], Path(path))
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível exportar o perfil: {error}")
            return None
        self.set_footer("perfil", f"Perfil {current['name']} exportado sem chaves em {result['path']}.")
        return result

    def shared_warning(self, path: str) -> str | None:
        try:
            return profiles.shared_warning(path, self.store.home)
        except (LauncherError, OSError, ValueError):
            return None

    def shared_resume_note(self) -> str | None:
        """Mesma pasta em outro perfil: aviso informativo (não bloqueia) ao retomar. Não há o que confirmar, porque a
        retomada já usa só as conversas abertas neste perfil (session/conversations.resume_target), como o modo texto
        (text.warn_shared com resume=True)."""
        warning = self.shared_warning(self.project["path"]) if self.project else None
        return f"{warning} Retomar usa só as conversas abertas neste perfil." if warning else None

    def show_diagnostics(self):
        lines = [f"{APP_NAME} {VERSION}", f"Dados: {self.store.home}",
                 f"Perfil: {profiles.profile_of_home(self.store.home, self.profile_base)['name']}", "",
                 "Conexão: " + (f"{self.gateway.name} ({self.gateway.module.LABEL})" if self.gateway else "nenhuma"),
                 "Endereço: " + (self.gateway.url if self.gateway else "-"),
                 "Estado: " + ("respondeu" if self.online else "não confirmado"),
                 "Windows Terminal: " + (terminal.find(ROOT) or "ausente (sessões em consoles separados)"),
                 "Atualização automática: " + self.update_status, ""]
        found_any = False
        for harness in registry.all():
            found = installed_path(harness.ID)
            found_any = found_any or bool(found)
            lines.append(f"{harness.LABEL}: encontrado\n{found}" if found else
                         f"{harness.LABEL}: não instalado. Instale pelo cartão dele." if harness.install_steps() else
                         f"{harness.LABEL}: não encontrado. {harness.MISSING_HINT}".rstrip())
        if not found_any:
            lines.append("Nenhuma ferramenta instalada ainda: normal numa instalação nova.")
        lines.extend(f"Adaptador do usuário não carregado: {error}" for error in registry.errors())
        last_open = self.last_startup()
        if last_open:
            lines.extend(["", f"Última abertura: {last_open}"])
        quota = self.quota_mark()
        lines.extend(["", self.catalog.status if self.catalog else "Sem catálogo.", "",
                      "As chaves das conexões ficam cifradas (DPAPI) e nunca aparecem aqui."])
        if quota:
            lines.insert(lines.index("Estado: " + ("respondeu" if self.online else "não confirmado")) + 1,
                         "Cota: esgotada na última verificação. " + quota)
        self.diagnostics_dialog = DiagnosticsDialog("\n".join(lines), self, lambda: self.confirm_copy(
            "Diagnóstico copiado: cole num chat de IA ou mande para o suporte."))
        self.diagnostics_dialog.open()


UPDATE_INTERVAL_MS = 4 * 60 * 60 * 1000
LIMIT_LEVELS = {"warn": int(usage.LIMIT_WARN_PERCENT), "over": int(usage.LIMIT_OVER_PERCENT)}  # nível do status -> % avisado
LIMIT_INTERVAL_MS = 10 * 60 * 1000  # o cache por arquivo do uso só relê o que cresceu
NEW_PROFILE = "__novo__"


def active_store() -> Store:
    """Store do perfil ativo (profiles.json); o padrão quando não há perfis ou o ativo sumiu."""
    try:
        return Store(profiles.home_of(profiles.active()["id"]))
    except LauncherError:
        return Store()
RESTART_DELAY_MS = 5000


def record_terminal_problem(report: str) -> None:
    """Guarda em ~/.agent-launcher/terminal.log quando e por que o Terminal não foi achado (para diagnosticar depois)."""
    import time
    from version import data_home
    try:
        destination = data_home() / "terminal.log"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{report}\n")
    except OSError:
        pass


def create_application(arguments=None):
    if sys.platform == "win32":
        # Antes da primeira janela: a barra de tarefas passa a mostrar o Agent Launcher, não o Python.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    application = QApplication(arguments if arguments is not None else sys.argv)
    application.setWindowIcon(QIcon(str(ROOT / "assets" / "launcher.ico")))
    application.setApplicationName(APP_NAME)
    application.setOrganizationName(APP_NAME)
    application.setStyle("Fusion")
    application.setFont(QFont("Segoe UI", 10))
    saved = Store(profiles.root())
    theme_module.apply(application, saved.choice("theme"), saved.choice("accent"))
    application.popup_keepers = placement.install(application)
    return application


def import_argument(arguments: list[str]) -> str | None:
    """Arquivo de `--import ARQUIVO` (duplo clique num .alprofile, pela associação do instalador)."""
    for index, value in enumerate(arguments):
        if value == "--import" and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith("--import="):
            return value.split("=", 1)[1]
    return None


def main(arguments: list[str] | None = None):
    """Abre a interface (apagando antes o cache das versões anteriores) e devolve o código do loop de eventos."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    cache.prune_old_versions()
    application = create_application()
    window = LauncherWindow()
    window.show()
    package = import_argument(arguments)
    if package:
        QTimer.singleShot(0, lambda: window.import_profile(package))
    return application.exec()

