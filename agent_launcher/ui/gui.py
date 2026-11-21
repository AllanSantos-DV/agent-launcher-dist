from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget,
    QSystemTrayIcon, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from agent_launcher import ROOT
from agent_launcher.domain import profiles, usage
from agent_launcher.domain.conversation_usage import conversation_usage, resume_window_warning, window_warning
from agent_launcher.domain.catalog import base_model, media_states, price_label, token_label
from agent_launcher.domain.errors import LauncherBusy, LauncherError
from agent_launcher.domain.files import append_capped, project_path
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import cli_update
from agent_launcher.harnesses import installers as clis
from agent_launcher.harnesses import registry, vscode
from agent_launcher.platform import keys, terminal
from agent_launcher.session.conversations import conversations_for, has_conversation_for
from agent_launcher.session.preview import plan_report, plan_text, planned
from agent_launcher.session.window import session_title, start_session
from agent_launcher.ui import placement
from agent_launcher.ui import theme as theme_module
from agent_launcher.ui.background import deliver
from agent_launcher.ui.connections import ConnectionsDialog, quota_warning
from agent_launcher.ui.instructions import InstructionsDialog
from agent_launcher.ui.text import label_of
from agent_launcher.ui.theme import (
    ACCENT_NAMES, DANGER, GREEN, INFO, MENU_SELECTED, MUTED, THEME_NAMES, WARN, CardGrid, CliButton, DangerMenu, ElidedLabel,
    FlowRow, InfoStrip, NoticeStrip, ReadableCombo, bind_icon, bind_style, icon, plural, tone, tone_item,
)
from agent_launcher.update import updater
from version import APP_NAME, APP_USER_MODEL_ID, VERSION


STARTUP_LOG = "abertura.log"
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
FIRST_MEDIA_COLUMN = 4
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


def label(text: str, role: str = "", parent=None) -> QLabel:
    widget = QLabel(text, parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    return widget


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
    """O que a próxima abertura vai iniciar (comando, conexão, modelo, ambiente e avisos), só para leitura."""

    def __init__(self, text: str, parent=None):
        super().__init__("O que vai abrir", text, "Copiar resumo", "Resumo copiado.", parent)


class ScreenTask(QRunnable):
    """Lê a tela do console de uma sessão aberta fora da thread da interface (o auxiliar pode levar alguns segundos)."""

    def __init__(self, pid: int):
        super().__init__()
        self.pid = pid
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            deliver(self.signals, "completed", keys.read_screen(self.pid), "")
        except Exception as error:  # volta para a tela como erro, nunca derruba a thread
            deliver(self.signals, "completed", error, "")


class SessionScreenDialog(TextDialog):
    """O que uma sessão aberta exibe agora, só para leitura: nada é digitado nela e a janela dela não vem para a frente."""

    def __init__(self, sessions: list[dict], parent=None):
        super().__init__("Tela da sessão", "", "Copiar tela", "Tela copiada.", parent)
        self.choice = ReadableCombo()
        self.choice.setAccessibleName("Sessão aberta")
        for session in sessions:
            self.choice.addItem(f"{label_of(session.get('cli', ''))} — {session.get('title') or session.get('project', '')}",
                                int(session["pid"]))
        self.layout().insertWidget(0, self.choice)
        self.error = label("", "notice")
        self.error.setWordWrap(True)
        self.error.hide()
        self.layout().insertWidget(1, self.error)
        self.refresh_button = QPushButton("Atualizar")
        self.refresh_button.clicked.connect(self.refresh)
        self.actions.insertWidget(1, self.refresh_button)
        self.choice.currentIndexChanged.connect(self.refresh)
        self.task = None
        self.refresh()

    def refresh(self) -> None:
        """Lê a tela da sessão escolhida; a leitura de uma escolha anterior que chega depois é descartada."""
        pid = self.choice.currentData()
        if pid is None:
            return
        self.error.hide()
        self.copied.setText("Lendo a tela…")
        task = self.task = ScreenTask(pid)
        task.signals.completed.connect(lambda result, text="": task is self.task and self.show_screen(result))
        QThreadPool.globalInstance().start(task)

    def show_screen(self, result) -> None:
        self.copied.setText("")
        if isinstance(result, Exception):
            self.error.setText(f"Não foi possível ler a tela da sessão: {result}")
            self.error.show()
            return
        self.box.setPlainText(result)
        self.box.verticalScrollBar().setValue(self.box.verticalScrollBar().maximum())


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


class ConversationsDialog(QDialog):
    """Conversas do CLI nesta pasta (as deste perfil, se a pasta é compartilhada): filtro por texto, cópia da lista
    como está filtrada, duplo clique ou Retomar."""

    def __init__(self, conversations: list, parent=None, cli_label: str = "", usages: dict | None = None,
                 record=None, verified: bool = False):
        super().__init__(parent)
        from datetime import datetime
        self.setWindowTitle(f"Conversas do {cli_label}" if cli_label else "Escolher conversa")
        self.setMinimumSize(720, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("Escolha a conversa a retomar (mais recente primeiro).", "muted"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrar por título, data ou CLI")
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filtrar conversas")
        self.filter.textChanged.connect(self.apply_filter)
        layout.addWidget(self.filter)
        usages = usages or {}
        self.table = QTableWidget(len(conversations), len(CONVERSATIONS_HEADER))
        self.table.setHorizontalHeaderLabels(list(CONVERSATIONS_HEADER))
        self.table.setAccessibleName("Conversas")
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
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
        self.copied = QLabel("")
        self.copied.setObjectName("muted")
        actions.addWidget(self.copied)
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
        layout.addLayout(actions)

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
        self.copied.setText("")

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


class InstallCliTask(QRunnable):
    """Instala a ferramenta fora da thread da interface (download de centenas de MB), relatando o progresso."""

    def __init__(self, cli: str):
        super().__init__()
        self.cli = cli
        self.signals = InstallSignals()

    @Slot()
    def run(self):
        lines = []
        try:
            found = clis.ensure(self.cli, log=lines.append, progress=lambda fraction, text: deliver(self.signals, "progress", fraction, text))
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            found, lines = None, lines + [f"{type(error).__name__}: {error}"]
        deliver(self.signals, "completed", found, "\n".join(lines))


class RefreshSignals(QObject):
    completed = Signal(object, str)


class InstallSignals(QObject):
    completed = Signal(object, str)
    progress = Signal(object, str)


class TerminalTask(QRunnable):
    """Instala o Windows Terminal sem admin (ZIP portátil oficial, depois winget) fora da thread da interface."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        lines = []
        try:
            found = terminal.ensure(self.root, log=lines.append)
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            found, lines = None, lines + [str(error)]
        deliver(self.signals, "completed", found, "\n".join(lines))


class CliCheckTask(QRunnable):
    """Lê as versões instaladas (`--version`) e as publicadas no npm das CLIs, fora da thread da interface."""

    def __init__(self):
        super().__init__()
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            deliver(self.signals, "completed", cli_update.check_all(), "")
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            deliver(self.signals, "completed", error, "")


class CliUpdateTask(QRunnable):
    """Atualiza uma CLI (o `update` dela, npm ou winget) fora da thread da interface; o resultado volta com o id da CLI."""

    def __init__(self, cli: str):
        super().__init__()
        self.cli = cli
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            deliver(self.signals, "completed", cli_update.update(self.cli), self.cli)
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            deliver(self.signals, "completed", error, self.cli)


class TerminalUpdateTask(QRunnable):
    """Atualiza o Windows Terminal portátil (a cópia que o launcher instalou) fora da thread da interface."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            deliver(self.signals, "completed", terminal.update_portable(self.root) or "", "")
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            deliver(self.signals, "completed", error, "")


class UpdateTask(QRunnable):
    """Confere o engine-registry e, havendo versão nova, instala por cima (fora da thread da interface)."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            deliver(self.signals, "completed", updater.update(self.root), "")
        except Exception as error:  # rede, integridade ou instalador: volta para a tela/diagnóstico
            deliver(self.signals, "completed", error, "")


class RefreshTask(QRunnable):
    def __init__(self, home: Path, connection: str, force: bool):
        super().__init__()
        self.home = home
        self.connection = connection
        self.force = force
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            store = Store(self.home)
            gateway = Gateway(store, store.find_connection(self.connection))
            catalog = gateway.catalog(refresh=self.force)
            deliver(self.signals, "completed", catalog, self.connection)
        except Exception as error:
            deliver(self.signals, "completed", error, self.connection)


class CleanupTask(QRunnable):
    """Limpeza nos destinos das conexões de um perfil (harnesses/cleanup.py) fora da thread da interface. A
    confirmação já foi feita pela interface: aqui o confirm sempre aceita (nada de QMessageBox em outra thread)."""

    def __init__(self, home: Path, cleanup=None):
        super().__init__()
        self.home = home
        self.cleanup = cleanup
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            from agent_launcher.harnesses.cleanup import cleanup_profile
            deliver(self.signals, "completed", (self.cleanup or cleanup_profile)(self.home, lambda *_: True), "")
        except Exception as error:  # volta para a tela como aviso, nunca derruba a thread
            from agent_launcher.harnesses.cleanup import failure
            deliver(self.signals, "completed", [failure("Limpeza nos destinos", error)], "")


class UsageTask(QRunnable):
    """Uso do mês do perfil (domain/usage.py) fora da thread da interface: pode ler muitos arquivos de conversa."""

    def __init__(self, home: Path, month: str | None = None, everyone: bool = False):
        super().__init__()
        self.home, self.month, self.everyone = home, month, everyone
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            report = usage.usage_report_all if self.everyone else usage.usage_report
            deliver(self.signals, "completed", report(self.home, self.month), "")
        except Exception as error:  # volta para a tela como erro, nunca derruba a thread
            deliver(self.signals, "completed", error, "")


def usage_rows(report: dict) -> list[tuple[str, str, str, str, str]]:
    """Linhas da tabela do diálogo de uso: destino, modelo, entrada (com cache), saída, custo."""
    names = {"claude": "Claude Code", "codex": "Codex", "copilot": "Copilot CLI", "vscode": "VS Code",
             "copilot_app": "Copilot app", "endpoint": "Endpoint local"}

    def tokens(value: int) -> str:
        return f"{value:,}".replace(",", ".")

    def money(value) -> str:
        return f"US$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    rows = []
    unpriced = False
    for row in report.get("models", []):
        entrada = row["input_tokens"] + row["cache_read"] + row["cache_write"]
        priced = row.get("priced", row["cost"] is not None) and row["cost"] is not None
        unpriced = unpriced or not priced
        rows.append((names.get(row["destination"], row["destination"]), row["model"], tokens(entrada),
                     tokens(row["output_tokens"]), money(row["cost"]) if priced else "sem preço"))
    total = report.get("total") or {}
    if rows:
        if total.get("cost") is None:
            total_cost = "sem preço"
        else:
            total_cost = money(total["cost"]) + (" (parcial)" if unpriced else "")
        rows.append(("Total", "", tokens(total.get("input_tokens", 0) + total.get("cache_read", 0)
                                         + total.get("cache_write", 0)),
                     tokens(total.get("output_tokens", 0)), total_cost))
    return rows


USAGE_HEADER = ("Destino", "Modelo", "Entrada", "Saída", "Custo")


def usage_text(report: dict) -> str:
    """O relatório de uso separado por tabulação: colado numa planilha, cada valor cai na sua coluna."""
    scope = " (todos os perfis)" if report.get("profile") == "todos" else ""
    lines = [f"Mês {report['month']}{scope}", "\t".join(USAGE_HEADER)]
    lines += ["\t".join(row) for row in usage_rows(report)]
    return "\n".join(lines)


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


MODELS_HEADER = ("Modelo", "Provider", "Entrada", "Saída", "Preço (US$ por 1M, entrada/saída)", *(title for _, title, _ in MEDIA_COLUMNS))


def models_text(records: list, catalog) -> str:
    """A lista de modelos separada por tabulação: colada numa planilha, cada valor cai na sua coluna."""
    lines = ["\t".join(MODELS_HEADER)]
    for record in records:
        states = ["—" if state is None else "sim" if state else "não" for _title, state in media_states(record)]
        output = "—" if record.max_output_tokens is None else token_label(record.max_output_tokens)
        lines.append("\t".join([record.id, provider_name(record.provider), token_label(record.max_input_tokens), output,
                                 price_label(record) or "—", *states]))
    return "\n".join(lines)


class UsageDialog(QDialog):
    from agent_launcher.domain.usage import NOTE

    def __init__(self, parent, home: Path, profile_name: str):
        super().__init__(parent)
        self.setWindowTitle(f"Uso do mês — {profile_name}")
        self.resize(760, 420)
        self.home = home
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
        self.table = QTableWidget(0, len(USAGE_HEADER))
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
        self.note = QLabel(self.NOTE)
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
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

    def calculate(self) -> None:
        """Calcula o mês escolhido; o resultado de uma escolha anterior que chega depois é descartado."""
        self.status.setText("Calculando…")
        self.error.hide()
        self.report = None
        self.copy_button.setEnabled(False)
        self.table.setRowCount(0)
        task = self.task = UsageTask(self.home, self.month_choice.currentData(), self.everyone_check.isChecked())
        task.signals.completed.connect(lambda report, text="": task is self.task and self.show_report(report, text))
        QThreadPool.globalInstance().start(task)

    def show_report(self, report, _text: str = ""):
        if isinstance(report, Exception):
            self.status.setText("")
            self.error.setText(f"Não foi possível calcular o uso: {report}")
            self.error.show()
            return
        self.error.hide()
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


PROJECT_TEXT = Qt.ItemDataRole.UserRole + 1  # texto e dica do projeto sem a marca de sessão aberta
PROJECT_PINNED = Qt.ItemDataRole.UserRole + 2
TRAY_NOTICE_MS = 10_000  # o Windows mostra o balão por tempo próprio; o ícone fica este tempo para o balão não sumir com ele
GENERAL_ERROR = "geral"  # origem do erro avulso de uma ação; os erros persistentes têm origem própria


class ModelTable(QTableWidget):
    """Lista de modelos: a coluna do nome ocupa o que sobra das colunas fixas e nunca fica menor que o nome mais
    comprido (ou NAME_MIN, sem linhas); na janela estreita a tabela rola na horizontal em vez de espremer o nome."""

    NAME_MIN = 200
    NAME_PADDING = 24

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_name_column()

    def fit_name_column(self) -> None:
        fixed = sum(self.columnWidth(column) for column in range(1, self.columnCount()))
        whole_name = self.sizeHintForColumn(0) + self.NAME_PADDING
        self.setColumnWidth(0, max(self.viewport().width() - fixed, whole_name, self.NAME_MIN))


class LauncherWindow(QMainWindow):
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
        self.terminal_installing = False
        self.terminal_updating = False
        self.cli_versions = {}
        self.cli_queue = []
        self.cli_checking = False
        self.cli_updating = None
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
        self.refresh_timer.start(1500)
        QApplication.instance().applicationStateChanged.connect(self.recheck_tools)
        self.shortcuts = []
        for sequence, callback in (("Ctrl+K", self.project_search.setFocus), ("Ctrl+F", self.model_search.setFocus),
                                   ("Ctrl+N", self.add_project), ("F5", lambda: self.refresh_catalog(True)),
                                   ("Ctrl+R", self.resume_by_shortcut), ("Ctrl+H", lambda: self.choose_conversation()),
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
            QTimer.singleShot(800, self.ensure_terminal)

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
        banner_row.addWidget(self.restart_banner)
        column.addLayout(banner_row)
        scrolled = QWidget()
        scrolled.setObjectName("page")
        body = QVBoxLayout(scrolled)
        body.setContentsMargins(32, 26, 32, 12)
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
        self.update_preference.toggled.connect(self.set_auto_update)
        menu.addAction(self.update_preference)
        self.cli_update_preference = QAction("Atualizar as CLIs automaticamente", self)
        self.cli_update_preference.setCheckable(True)
        self.cli_update_preference.setChecked(self.store.preference("cli_auto_update"))
        self.cli_update_preference.toggled.connect(self.set_cli_auto_update)
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
        self.provider_filter.setAccessibleName("Filtrar provider")
        self.provider_filter.currentIndexChanged.connect(self.populate_models)
        search_row.addWidget(self.provider_filter)
        self.media_filter = ReadableCombo()
        self.media_filter.setAccessibleName("Filtrar por entrada")
        for text, field in MEDIA_FILTERS:
            self.media_filter.addItem(text, field)
        self.media_filter.fit_to_items()
        self.media_filter.currentIndexChanged.connect(self.populate_models)
        choices_row.addWidget(self.media_filter)
        self.model_order = ReadableCombo()
        self.model_order.setAccessibleName("Ordenar modelos")
        for text, order in MODEL_ORDERS:
            self.model_order.addItem(text, order)
            self.model_order.setItemData(self.model_order.count() - 1, MODEL_ORDER_TIPS[order], Qt.ItemDataRole.ToolTipRole)
        self.model_order.fit_to_items()
        self.model_order.currentIndexChanged.connect(self.populate_models)
        choices_row.addWidget(self.model_order)
        self.copy_models_button = button("Copiar lista", "copy", "ghost")
        self.copy_models_button.setToolTip("Copia a lista como está filtrada, separada por tabulação, para colar numa planilha")
        self.copy_models_button.clicked.connect(self.copy_models)
        actions_row.addWidget(self.copy_models_button)
        self.refresh_button = button("Atualizar", "refresh")
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
        self.model_table = ModelTable(0, FIRST_MEDIA_COLUMN + len(MEDIA_COLUMNS))
        self.model_table.setAccessibleName("Modelos disponíveis: nome, provider, entrada, saída, visão, áudio e vídeo")
        self.model_table.setHorizontalHeaderLabels(["Modelo", "Provider", "Entrada", "Saída", *(title for _, title, _ in MEDIA_COLUMNS)])
        self.model_table.verticalHeader().hide()
        self.model_table.verticalHeader().setDefaultSectionSize(40)
        self.model_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.model_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.model_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.model_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.model_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        for column in (2, 3):
            self.model_table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.model_table.setColumnWidth(1, 115)
        self.model_table.setColumnWidth(2, 90)
        self.model_table.setColumnWidth(3, 80)
        for column in range(FIRST_MEDIA_COLUMN, FIRST_MEDIA_COLUMN + len(MEDIA_COLUMNS)):
            self.model_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.model_table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.model_table.setColumnWidth(column, 64)
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.model_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.model_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.model_table.setShowGrid(False)
        self.model_table.setMinimumHeight(160)
        self.model_table.itemSelectionChanged.connect(self.select_model)
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

    def mark_open_sessions(self):
        """Marca na lista os projetos com sessão aberta agora, aberta por esta ou por outra instância do launcher."""
        open_clis: dict[str, list[str]] = {}
        for session in keys.sessions():
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

    def set_auto_update(self, checked):
        try:
            fresh = Store(self.store.home)
            fresh.set_preference("auto_update", checked)
            self.store = fresh
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível salvar a preferência: {error}")

    def set_cli_auto_update(self, checked):
        try:
            fresh = Store(self.store.home)
            fresh.set_preference("cli_auto_update", checked)
            self.store = fresh
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(f"Não foi possível salvar a preferência: {error}")

    def start_cli_check(self):
        if self.cli_checking or self.cli_updating:
            return
        self.cli_checking = True
        self.cli_check_task = CliCheckTask()
        self.cli_check_task.signals.completed.connect(self.finish_cli_check)
        QThreadPool.globalInstance().start(self.cli_check_task)

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
        self.cli_update_task.signals.completed.connect(self.finish_cli_update)
        QThreadPool.globalInstance().start(self.cli_update_task)

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
        self.terminal_update_task.signals.completed.connect(self.finish_terminal_update)
        QThreadPool.globalInstance().start(self.terminal_update_task)

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
            self.update_task.signals.completed.connect(self.finish_auto_update)
            QThreadPool.globalInstance().start(self.update_task)

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
            for column, (field, _title, symbol) in enumerate(MEDIA_COLUMNS, start=FIRST_MEDIA_COLUMN):
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
        identifier = self.connection_picker.currentData()
        self.gateway, self.catalog, self.online = None, None, False
        self.clear_transient("connection")
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
        self.populate_models()

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
        self.provider_filter.blockSignals(False)

    def populate_models(self):
        preferred = self.selected_model.id if self.selected_model else None
        if not preferred and self.project and self.gateway:
            preferred = self.store.last_model(self.project, self.gateway.connection, self.cli)
        query = self.model_search.text().strip().casefold()
        provider = self.provider_filter.currentData()
        media = self.media_filter.currentData()
        all_records = self.catalog.for_cli(self.cli) if self.catalog else []
        self.records = [record for record in all_records if (provider is None or record.provider == provider)
                        and (media is None or getattr(record, media) is True)
                        and (query in record.id.casefold() or query in model_name(record.id).casefold())]
        self.records = order_models(self.records, self.model_order.currentData())
        self.model_table.blockSignals(True)
        self.model_table.clearContents()
        self.model_table.setRowCount(len(self.records))
        selected_row = 0
        for row, record in enumerate(self.records):
            values = (model_name(record.id), provider_name(record.provider), token_label(record.max_input_tokens).replace(".", ","),
                      "—" if record.max_output_tokens is None else token_label(record.max_output_tokens).replace(".", ","))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(record.id if column < 2 else self.catalog.describe(record))
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.model_table.setItem(row, column, item)
            for column, (field, title, symbol) in enumerate(MEDIA_COLUMNS, start=FIRST_MEDIA_COLUMN):
                self.model_table.setItem(row, column, self.media_item(title, symbol, getattr(record, field)))
            if record.id == preferred:
                selected_row = row
        self.model_table.fit_name_column()
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
        PlanDialog(plan_text(report) if "command" in report else json.dumps(report, ensure_ascii=False, indent=2),
                   self).exec()

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
        dialog = ConversationsDialog(found, self, registry.get(self.cli).LABEL, self.conversation_usages(found),
                                     self.selected_model, bool(self.catalog and self.catalog.verified))
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected():
            self.launch_session(True, conversation=dialog.selected())

    def conversation_usages(self, conversations: list) -> dict:
        """Custo e contexto de cada conversa da lista, lidos dos transcripts (cache por arquivo)."""
        cache, tables = usage.FileCache(self.store.home), usage.price_table(self.store.home)
        found = {item.id: conversation_usage(self.store.home, item.cli, item.id, tables, cache) for item in conversations}
        cache.save()
        return found

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
        self.refresh_task.signals.completed.connect(self.finish_refresh)
        QThreadPool.globalInstance().start(self.refresh_task)

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
        elif self.catalog is not None:
            self.catalog.verified = False
        if error:
            self.show_error(f"Não foi possível acessar a conexão. Verifique o host e clique em Atualizar. {error}",
                            origin="connection")
        else:
            self.clear_error("connection")
        self.populate_providers()
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
        self.install_task.signals.completed.connect(self.finish_install_cli)
        QThreadPool.globalInstance().start(self.install_task)

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

    def check_sessions(self):
        self.mark_open_sessions()
        finished = [(process, title) for process, title in self.sessions if process.poll() is not None]
        self.sessions = [(process, title) for process, title in self.sessions if process.poll() is None]
        if finished:
            message = (f"Sessão encerrada: {finished[-1][1]} · "
                       f"{plural(len(self.sessions), 'janela ainda aberta', 'janelas ainda abertas')}")
            self.set_footer("sessao", message)
            self.notify_session_ended(message)
        self.sync_tray()

    def notify_session_ended(self, message: str) -> None:
        """Balão do Windows com a mensagem do rodapé, só com o launcher minimizado ou sem foco: à vista, o rodapé basta."""
        if self.isActiveWindow() and not self.isMinimized():
            return
        self.tray_notified = True
        self.sync_tray()
        if self.tray is None:
            self.tray_notified = False
            return
        self.tray.showMessage(APP_NAME, message, self.windowIcon(), TRAY_NOTICE_MS)
        self.tray_timer.start()

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
        self.terminal_task.signals.completed.connect(self.finish_terminal)
        QThreadPool.globalInstance().start(self.terminal_task)

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
        """Mostra, só para leitura, o que a sessão aberta exibe agora; a janela dela fica onde está."""
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

    def show_usage(self):
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        self.usage_dialog = UsageDialog(self, self.store.home, current["name"])
        self.usage_dialog.show()

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
        self.cleanup_task.signals.completed.connect(
            lambda messages, _text: self.finish_remove_profile(current, delete_data, messages))
        QThreadPool.globalInstance().start(self.cleanup_task)

    def set_profile_busy(self, busy: bool):
        """Durante a limpeza do perfil: seletor e menu do perfil travados (o perfil ainda existe até o fim)."""
        self.removing_profile = busy
        self.profile_picker.setEnabled(not busy)
        self.profile_options.setEnabled(not busy)

    def finish_remove_profile(self, current: dict, delete_data: bool, messages):
        from agent_launcher.harnesses.cleanup import Failure, failure
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
            self.show_error(" ".join([*(m for m in messages if isinstance(m, Failure)), str(error)]), origin="perfil")
            if any(not isinstance(m, Failure) for m in messages):
                self.show_info(" ".join(m for m in messages if not isinstance(m, Failure)), origin="perfil")
            return
        self.switch_profile(profiles.DEFAULT)
        if messages:  # o que foi feito (informativo) e o que falhou (erro, ex.: "VS Code: arquivo preso"), sem bloquear
            self.set_footer("perfil", " ".join(messages))
            failed = [message for message in messages if isinstance(message, Failure)]
            if failed:
                self.show_error(" ".join(failed), origin="perfil")
            if len(failed) < len(messages):
                self.show_info(" ".join(message for message in messages if not isinstance(message, Failure)), origin="perfil")

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
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    application = create_application()
    window = LauncherWindow()
    window.show()
    package = import_argument(arguments)
    if package:
        QTimer.singleShot(0, lambda: window.import_profile(package))
    return application.exec()

