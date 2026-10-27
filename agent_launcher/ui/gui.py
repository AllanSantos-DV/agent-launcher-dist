from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from agent_launcher import ROOT
from agent_launcher.domain import profiles
from agent_launcher.domain.catalog import base_model, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import project_path
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import installers as clis
from agent_launcher.harnesses import registry
from agent_launcher.platform import terminal
from agent_launcher.session.conversations import conversations_for, has_conversation_for
from agent_launcher.session.window import session_title, start_session
from agent_launcher.ui.connections import ConnectionsDialog
from agent_launcher.ui.theme import GREEN, MUTED, STYLE, CliButton, ElidedLabel, icon
from agent_launcher.update import updater
from version import APP_NAME, APP_USER_MODEL_ID, VERSION


def installed_path(cli: str) -> str | None:
    """Executável do destino (CLIs de terminal pelo módulo de instalação, que os testes substituem)."""
    if cli in clis.NAMES:
        return clis.find(cli)
    return registry.get(cli).find_executable()


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
        widget.setIcon(icon(symbol, "#ffffff" if role == "primary" else GREEN))
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


class ConversationsDialog(QDialog):
    """Conversas do CLI nesta pasta (as deste perfil, se a pasta é compartilhada): duplo clique ou Retomar."""

    def __init__(self, conversations: list, parent=None, cli_label: str = ""):
        super().__init__(parent)
        from datetime import datetime
        self.setWindowTitle(f"Conversas do {cli_label}" if cli_label else "Escolher conversa")
        self.setMinimumSize(720, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("Escolha a conversa a retomar (mais recente primeiro).", "muted"))
        self.table = QTableWidget(len(conversations), 4)
        self.table.setHorizontalHeaderLabels(["Data", "CLI", "Título", "Mensagens"])
        self.table.setAccessibleName("Conversas")
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, conversation in enumerate(conversations):
            values = (datetime.fromtimestamp(conversation.updated).strftime("%d/%m/%Y %H:%M"),
                      registry.get(conversation.cli).LABEL if registry.find(conversation.cli) else conversation.cli,
                      conversation.title or "(sem título)",
                      "" if conversation.messages is None else str(conversation.messages))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, conversation.id)
                if column == 2:
                    item.setToolTip(f"{conversation.title}\nID: {conversation.id}")
                self.table.setItem(row, column, item)
        if conversations:
            self.table.selectRow(0)
        self.table.cellDoubleClicked.connect(lambda *_args: self.accept())
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
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

    def selected(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row(), 0) if rows else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class InstallCliTask(QRunnable):
    """Instala o CLI fora da thread da interface (download de centenas de MB)."""

    def __init__(self, cli: str):
        super().__init__()
        self.cli = cli
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        lines = []
        try:
            found = clis.ensure(self.cli, log=lines.append) if self.cli in clis.NAMES else None
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            found, lines = None, lines + [str(error)]
        self.signals.completed.emit(found, "\n".join(lines))


class RefreshSignals(QObject):
    completed = Signal(object, str)


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
        self.signals.completed.emit(found, "\n".join(lines))


class TerminalUpdateTask(QRunnable):
    """Atualiza o Windows Terminal portátil (a cópia que o launcher instalou) fora da thread da interface."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            self.signals.completed.emit(terminal.update_portable(self.root) or "", "")
        except Exception as error:  # falha inesperada também precisa voltar para a tela
            self.signals.completed.emit(error, "")


class UpdateTask(QRunnable):
    """Confere o npm e, havendo versão nova, instala por cima (fora da thread da interface)."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            self.signals.completed.emit(updater.update(self.root), "")
        except Exception as error:  # rede, integridade ou instalador: volta para a tela/diagnóstico
            self.signals.completed.emit(error, "")


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
            self.signals.completed.emit(catalog, self.connection)
        except Exception as error:
            self.signals.completed.emit(error, self.connection)


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
            self.signals.completed.emit((self.cleanup or cleanup_profile)(self.home, lambda *_: True), "")
        except Exception as error:  # volta para a tela como aviso, nunca derruba a thread
            self.signals.completed.emit([str(error)], "")


class UsageTask(QRunnable):
    """Uso do mês do perfil (domain/usage.py) fora da thread da interface: pode ler muitos arquivos de conversa."""

    def __init__(self, home: Path, month: str | None = None):
        super().__init__()
        self.home, self.month = home, month
        self.signals = RefreshSignals()

    @Slot()
    def run(self):
        try:
            from agent_launcher.domain.usage import usage_report
            self.signals.completed.emit(usage_report(self.home, self.month), "")
        except Exception as error:  # volta para a tela como erro, nunca derruba a thread
            self.signals.completed.emit(error, "")


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


class UsageDialog(QDialog):
    from agent_launcher.domain.usage import NOTE

    def __init__(self, parent, home: Path, profile_name: str):
        super().__init__(parent)
        self.setWindowTitle(f"Uso do mês — {profile_name}")
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        self.status = QLabel("Calculando…")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Destino", "Modelo", "Entrada", "Saída", "Custo"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        note = QLabel(self.NOTE)
        note.setWordWrap(True)
        layout.addWidget(note)
        close = QPushButton("Fechar")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.task = UsageTask(home)
        self.task.signals.completed.connect(self.show_report)
        QThreadPool.globalInstance().start(self.task)

    def show_report(self, report, _text: str = ""):
        if isinstance(report, Exception):
            self.status.setText(f"Não foi possível calcular o uso: {report}")
            return
        rows = usage_rows(report)
        self.status.setText(f"Mês {report['month']}" + ("" if rows else ": nenhum uso encontrado."))
        self.table.setRowCount(len(rows))
        for number, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QTableWidgetItem(value)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(number, column, item)
        self.table.resizeColumnsToContents()


class LauncherWindow(QMainWindow):
    def __init__(self, store: Store | None = None, auto_refresh: bool = True):
        super().__init__()
        self.store = store or active_store()
        # Raiz dos perfis: a pasta-mãe quando o Store é de um perfil extra; a própria pasta senão (testes).
        self.profile_base = profiles.base_of(self.store.home)
        self.auto_refresh = auto_refresh
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
        self.update_status = "não verificada nesta execução"
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(ROOT / "assets" / "launcher.ico")))
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1220, available.width() - 40), min(850, available.height() - 60))
        self.setMinimumSize(990, 680)
        self._build()
        self.populate_profiles()
        self.populate_projects(self.store.data["last_project"])
        self.populate_connections()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.check_sessions)
        self.refresh_timer.start(1500)
        self.shortcuts = []
        for sequence, callback in (("Ctrl+K", self.project_search.setFocus), ("Ctrl+F", self.model_search.setFocus),
                                   ("Ctrl+N", self.add_project), ("F5", lambda: self.refresh_catalog(True))):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        if self.auto_refresh:
            QTimer.singleShot(1500, self.start_auto_update)
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
        rail = QVBoxLayout()
        rail.setContentsMargins(20, 21, 20, 20)
        rail.setSpacing(13)
        outer.addLayout(rail, 1)
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
        self.profile_picker = QComboBox()
        self.profile_picker.setAccessibleName("Perfil de trabalho")
        self.profile_picker.setToolTip("Cada perfil tem os seus projetos e as suas conexões")
        self.profile_picker.activated.connect(self.pick_profile)
        profile_row.addWidget(self.profile_picker, 1)
        self.profile_options = QToolButton()
        self.profile_options.setObjectName("ghost")
        self.profile_options.setIcon(icon("more", "#c8d9c7"))
        self.profile_options.setToolTip("Novo perfil, renomear, remover, importar ou exportar")
        self.profile_options.setAccessibleName("Opções do perfil")
        self.profile_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        profile_menu = QMenu(self)
        profile_menu.addAction("Novo perfil…", self.new_profile)
        self.rename_profile_action = profile_menu.addAction("Renomear perfil…", self.rename_profile)
        self.color_profile_action = profile_menu.addAction("Cor do perfil…", self.color_profile)
        self.remove_profile_action = profile_menu.addAction("Remover perfil…", self.remove_profile)
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
        search_palette = self.project_search.palette()
        search_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#c4d8c6"))
        self.project_search.setPalette(search_palette)
        self.project_search.addAction(icon("search", "#c8d9c7"), QLineEdit.ActionPosition.LeadingPosition)
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
        list_hint = label("Mais recentes primeiro. Botão direito para editar ou remover.", "railMuted")
        list_hint.setWordWrap(True)
        rail.addWidget(list_hint)
        add = button("Adicionar projeto", "plus")
        add.setIcon(icon("plus", "#edf5e6"))
        add.clicked.connect(self.add_project)
        rail.addWidget(add)
        import_button = button("Importar projetos", role="ghost")
        import_button.clicked.connect(self.import_projects)
        rail.addWidget(import_button)
        rail.addSpacing(10)
        rail.addWidget(label("CONEXÃO", "railMuted"))
        self.connection_picker = QComboBox()
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
        body = QVBoxLayout(content)
        body.setContentsMargins(32, 26, 32, 22)
        body.setSpacing(12)
        top = QHBoxLayout()
        self.project_title = ElidedLabel("Escolha um projeto")
        self.project_title.setObjectName("title")
        self.project_title.setMinimumHeight(43)
        top.addWidget(self.project_title, 1)
        self.connection = label("Verificando conexão…", "status")
        top.addWidget(self.connection)
        self.options = QToolButton()
        self.options.setObjectName("ghost")
        self.options.setIcon(icon("more"))
        self.options.setToolTip("Opções do projeto e diagnóstico")
        self.options.setAccessibleName("Opções do projeto e diagnóstico")
        self.options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        menu.addAction("Abrir pasta do projeto", self.open_project)
        menu.addAction("Editar projeto…", self.edit_project)
        menu.addAction("Remover do launcher…", self.remove_project)
        menu.addSeparator()
        menu.addAction("Conexões…", self.manage_connections)
        menu.addAction("Diagnóstico da conexão", self.show_diagnostics)
        self.bypass_preference = QAction("Bypass marcado por padrão", self)
        self.bypass_preference.setCheckable(True)
        self.bypass_preference.setChecked(self.store.preference("bypass_default"))
        self.bypass_preference.toggled.connect(self.set_bypass_default)
        menu.addAction(self.bypass_preference)
        self.update_preference = QAction("Atualizar automaticamente", self)
        self.update_preference.setCheckable(True)
        self.update_preference.setChecked(self.store.preference("auto_update"))
        self.update_preference.toggled.connect(self.set_auto_update)
        menu.addAction(self.update_preference)
        menu.addAction("Abrir instruções", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / "README.md"))))
        self.options.setMenu(menu)
        top.addWidget(self.options)
        body.addLayout(top)
        project_path_row = QHBoxLayout()
        self.project_path = ElidedLabel("Seus projetos e suas sessões, em um só lugar.")
        self.project_path.setObjectName("muted")
        project_path_row.addWidget(self.project_path, 1)
        self.open_folder = button("Abrir pasta", "folder", "ghost")
        self.open_folder.clicked.connect(self.open_project)
        project_path_row.addWidget(self.open_folder)
        body.addLayout(project_path_row)
        body.addSpacing(2)
        cli_row = QGridLayout()
        cli_row.setSpacing(12)
        self.cli_group = QButtonGroup(self)
        self.cli_buttons = {}
        for index, harness in enumerate(registry.all()):
            widget = CliButton(harness.LABEL + (" (prévia)" if harness.PREVIEW else ""), harness.SUBTITLE, harness.SYMBOL)
            self.cli_group.addButton(widget)
            self.cli_buttons[harness.ID] = widget
            widget.clicked.connect(lambda checked=False, chosen=harness.ID: self.select_cli(chosen))
            cli_row.addWidget(widget, index // 3, index % 3)
        self.cli_buttons["claude"].setChecked(True)
        body.addLayout(cli_row)
        model_heading = QHBoxLayout()
        model_heading.addWidget(label("Modelo da sessão", "section"))
        model_heading.addStretch()
        self.model_count = label("", "muted")
        model_heading.addWidget(self.model_count)
        body.addLayout(model_heading)
        search_row = QHBoxLayout()
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("Buscar modelo, família ou ID")
        self.model_search.setAccessibleName("Buscar modelos")
        self.model_search.addAction(icon("search", MUTED), QLineEdit.ActionPosition.LeadingPosition)
        self.model_search.textChanged.connect(self.populate_models)
        search_row.addWidget(self.model_search, 1)
        self.provider_filter = QComboBox()
        self.provider_filter.setAccessibleName("Filtrar provider")
        self.provider_filter.currentIndexChanged.connect(self.populate_models)
        search_row.addWidget(self.provider_filter)
        self.refresh_button = button("Atualizar", "refresh")
        self.refresh_button.setToolTip("Atualizar catálogo e verificar proxy (F5)")
        self.refresh_button.clicked.connect(lambda: self.refresh_catalog(True))
        search_row.addWidget(self.refresh_button)
        body.addLayout(search_row)
        self.model_table = QTableWidget(0, 3)
        self.model_table.setAccessibleName("Modelos disponíveis: nome, provider e entrada")
        self.model_table.setHorizontalHeaderLabels(["Modelo", "Provider", "Entrada"])
        self.model_table.verticalHeader().hide()
        self.model_table.verticalHeader().setDefaultSectionSize(40)
        self.model_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.model_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.model_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.model_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.model_table.horizontalHeaderItem(2).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.model_table.setColumnWidth(1, 115)
        self.model_table.setColumnWidth(2, 100)
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.model_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.model_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.model_table.setShowGrid(False)
        self.model_table.setMinimumHeight(110)
        self.model_table.itemSelectionChanged.connect(self.select_model)
        body.addWidget(self.model_table, 1)
        self.empty_models = label("Nenhum modelo encontrado. Limpe a busca ou atualize o catálogo.", "muted")
        self.empty_models.setWordWrap(True)
        self.empty_models.hide()
        body.addWidget(self.empty_models)
        self.model_id = ElidedLabel()
        self.model_id.setObjectName("muted")
        body.addWidget(self.model_id)
        self.context_info = label("Escolha um modelo para ver o contexto.")
        self.context_info.setWordWrap(True)
        body.addWidget(self.context_info)
        self.family_info = ElidedLabel()
        self.family_info.setObjectName("muted")
        body.addWidget(self.family_info)
        self.notice = label("", "notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        notice_row = QHBoxLayout()
        notice_row.addWidget(self.notice, 1)
        self.copy_error = button("Copiar erro", "copy", "ghost")
        self.copy_error.setToolTip("Copia o aviso com o contexto (versões, Windows, Terminal) para colar num chat de IA")
        self.copy_error.clicked.connect(self.copy_notice)
        self.copy_error.hide()
        notice_row.addWidget(self.copy_error, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(notice_row)
        self.notice.installEventFilter(self)  # o botão aparece e some junto com a faixa
        self.update_notice = label("", "notice")
        self.update_notice.setWordWrap(True)
        self.update_notice.setTextFormat(Qt.TextFormat.RichText)
        self.update_notice.linkActivated.connect(lambda _link: self.restart())
        self.update_notice.hide()
        if self.store.recovered:
            self.update_notice.setText(self.store.recovered.replace("<", "&lt;"))
            self.update_notice.show()
        body.addWidget(self.update_notice)
        actions = QHBoxLayout()
        session_options = QVBoxLayout()
        session_options.setSpacing(4)
        self.bypass_checkbox = QCheckBox("&Bypass de permissões")
        self.bypass_checkbox.setChecked(self.bypass_default)
        self.bypass_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bypass_checkbox.toggled.connect(self.update_actions)
        session_options.addWidget(self.bypass_checkbox)
        self.session_hint = label("A sessão abre em outra janela.", "muted")
        self.session_hint.setWordWrap(True)
        session_options.addWidget(self.session_hint)
        actions.addLayout(session_options, 1)
        self.install_cli_button = button("Instalar", "plus")
        self.install_cli_button.setMinimumHeight(44)
        self.install_cli_button.clicked.connect(self.install_cli)
        self.install_cli_button.hide()
        actions.addWidget(self.install_cli_button)
        self.installing_cli = False
        self.resume_button = button("Retomar última", "resume")
        self.resume_button.setMinimumHeight(44)
        self.resume_button.clicked.connect(lambda: self.launch_session(True))
        actions.addWidget(self.resume_button)
        self.build_conversation_menu(actions)
        self.launch_button = button("Nova sessão", "arrow", "primary")
        self.launch_button.setMinimumHeight(44)
        self.launch_button.clicked.connect(lambda: self.launch_session(False))
        actions.addWidget(self.launch_button)
        body.addLayout(actions)
        self.footer = ElidedLabel("Consultando a conexão…")
        self.footer.setObjectName("muted")
        body.addWidget(self.footer)
        layout.addWidget(content, 1)

    def populate_projects(self, preferred=None):
        self.project_list.blockSignals(True)
        self.project_list.clear()
        selected = None
        for project in self.store.projects:
            item = QListWidgetItem(icon("folder", "#c8d9c7"), project["name"])
            item.setData(Qt.ItemDataRole.UserRole, project["id"])
            item.setToolTip(project["path"])
            if not Path(project["path"]).is_dir():
                item.setText(project["name"] + "  (pasta não encontrada)")
                item.setToolTip(f"Pasta não encontrada: {project['path']}\nBotão direito → Editar para apontar a nova pasta.")
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            self.project_list.addItem(item)
            if project["id"] == preferred:
                selected = item
        self.project_list.blockSignals(False)
        self.project_count.setText(str(len(self.store.projects)))
        self.filter_projects()
        if selected or self.project_list.count():
            self.project_list.setCurrentItem(selected or self.project_list.item(0))
        else:
            self.select_project(None)

    def filter_projects(self):
        query = self.project_search.text().strip().casefold()
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            item.setHidden(query not in item.text().casefold() and query not in item.toolTip().casefold())

    def select_project(self, item, previous=None):
        if previous:
            previous.setIcon(icon("folder", "#c8d9c7"))
        self.project = self.store.find(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if item:
            item.setIcon(icon("folder", GREEN))
        self.project_title.setText(self.project["name"] if self.project else "Seu próximo projeto começa aqui")
        self.project_path.setText(self.project["path"] if self.project else "Adicione uma pasta para começar. Nenhum arquivo será alterado.")
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
            self.show_info(result)

    def start_auto_update(self):
        self.update_portable_terminal()
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
            self.update_notice.setText(f"Atualização automática falhou: {result}".replace("<", "&lt;"))
            self.update_notice.show()
        elif result:
            self.update_status = f"atualizado para {result}; reiniciando a interface"
            self.update_notice.setText(f"{APP_NAME} atualizado para {result}. Reiniciando a interface em "
                                       f"{RESTART_DELAY_MS // 1000} s; as sessões abertas continuam. "
                                       f'<a href="restart">Reiniciar agora</a>')
            self.update_notice.show()
            # A janela aberta segue com o código antigo na memória: sem reiniciar, correções não chegam a ela.
            QTimer.singleShot(RESTART_DELAY_MS, self.restart_if_idle)
        else:
            self.update_status = f"em dia ({VERSION})"
        updater.record(self.update_status)

    def restart_if_idle(self):
        """Reinicia sozinho, a menos que haja diálogo aberto ou instalação em curso (aí fica a faixa com o link)."""
        busy = QApplication.activeModalWidget() is not None or self.installing_cli or self.terminal_installing
        if busy:
            self.update_status = self.update_status.replace("reiniciando a interface", "reabra para usar")
            self.update_notice.setText(f"{APP_NAME} atualizado. Termine o que está fazendo e "
                                       f'<a href="restart">reinicie</a> para usar a versão nova.')
            return
        self.restart()

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
        self.notice.hide()
        try:
            if identifier:
                self.gateway = Gateway(self.store, self.store.find_connection(identifier))
                self.catalog = self.gateway.catalog(offline=True)
        except LauncherError as error:
            self.gateway = None
            self.show_error(str(error))
        self.address_label.setText(self.gateway.url if self.gateway else "Nenhuma conexão")
        self.address_label.setToolTip(self.gateway.url if self.gateway else "")
        for cli, widget in self.cli_buttons.items():
            widget.setEnabled(bool(self.gateway and cli in self.gateway.clis))
        if not self.gateway:
            self.connection.setText("Sem conexão")
            self.connection.setProperty("offline", True)
            if not self.store.connections:
                self.show_error("Adicione uma conexão com um provedor (Gerenciar conexões) para listar modelos e abrir sessões.")
        else:
            self.connection.setText("Verificando conexão…")
        preferred = self.store.last_cli(self.project) if self.project else self.cli
        available = [cli for cli in registry.ids() if self.gateway and cli in self.gateway.clis] or ["claude"]
        self.select_cli(preferred if preferred in available else available[0])
        if self.gateway and self.auto_refresh:
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

    def select_cli(self, cli):
        self.cli = cli
        self.bypass_checkbox.setChecked(self.bypass_default)
        self.cli_buttons[cli].setChecked(True)
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
        self.provider_filter.blockSignals(False)

    def populate_models(self):
        preferred = self.selected_model.id if self.selected_model else None
        if not preferred and self.project and self.gateway:
            preferred = self.store.last_model(self.project, self.gateway.connection, self.cli)
        query = self.model_search.text().strip().casefold()
        provider = self.provider_filter.currentData()
        all_records = self.catalog.for_cli(self.cli) if self.catalog else []
        self.records = [record for record in all_records if (provider is None or record.provider == provider)
                        and (query in record.id.casefold() or query in model_name(record.id).casefold())]
        families = {"opus": 0, "sonnet": 1, "haiku": 2}
        self.records.sort(key=lambda record: record.version, reverse=True)
        self.records.sort(key=lambda record: families.get(record.family, 3))
        self.model_table.blockSignals(True)
        self.model_table.clearContents()
        self.model_table.setRowCount(len(self.records))
        selected_row = 0
        for row, record in enumerate(self.records):
            values = (model_name(record.id), provider_name(record.provider), token_label(record.max_input_tokens).replace(".", ","))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(record.id if column != 2 else self.catalog.describe(record))
                if column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.model_table.setItem(row, column, item)
            if record.id == preferred:
                selected_row = row
        self.model_table.blockSignals(False)
        if self.records:
            self.model_table.selectRow(selected_row)
        self.empty_models.setVisible(not self.records)
        self.model_count.setText(f"{len(self.records)} de {len(all_records)} disponíveis")
        self.select_model()

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
        self.update_actions()

    def update_actions(self):
        harness = registry.get(self.cli)
        missing = installed_path(self.cli) is None
        installable = bool(harness.install_steps())
        self.install_cli_button.setVisible((missing and installable) or self.installing_cli)
        self.install_cli_button.setEnabled(not self.installing_cli)
        self.install_cli_button.setText(f"Instalando {harness.LABEL}…" if self.installing_cli
                                        else f"Instalar {harness.LABEL}")
        enabled = bool(self.project and self.gateway and self.selected_model and self.online and not self.refreshing
                       and not missing)
        self.launch_button.setEnabled(enabled)
        self.launch_button.setText("Nova sessão" if harness.KIND == "terminal" else f"Abrir no {harness.LABEL}")
        resumable = enabled and "resume" in harness.CAPABILITIES and self.has_previous_conversation()
        self.resume_button.setVisible("resume" in harness.CAPABILITIES)
        self.resume_button.setEnabled(resumable)
        self.conversation_button.setVisible("resume" in harness.CAPABILITIES)
        self.conversation_button.setEnabled(resumable)
        self.bypass_checkbox.setVisible("bypass" in harness.CAPABILITIES)
        self.bypass_checkbox.setEnabled(enabled)
        bypass = self.bypass_checkbox.isChecked() and "bypass" in harness.CAPABILITIES
        description = harness.BYPASS_RISK + " Vale só para a próxima abertura; o padrão fica em Opções → Bypass marcado por padrão."
        self.bypass_checkbox.setToolTip(description)
        self.bypass_checkbox.setAccessibleDescription(description)
        self.session_hint.setText(harness.BYPASS_HINT if bypass else
                                  harness.MISSING_HINT if missing and not installable else harness.SESSION_HINT)
        self.session_hint.setToolTip(description if bypass else
                                     session_title(self.project, self.cli, self.store.home) if self.project else "Selecione um projeto")
        self.session_hint.setProperty("bypass", bypass)
        self.session_hint.style().unpolish(self.session_hint)
        self.session_hint.style().polish(self.session_hint)
        reason = "Retoma a última sessão nativa deste CLI na pasta do projeto." if enabled else "Escolha projeto, conexão e modelo; a conexão precisa responder."
        self.resume_button.setToolTip(reason if resumable or not enabled else
                                      f"Ainda não há conversa do {harness.LABEL} nesta pasta para retomar: use Nova sessão.")
        self.launch_button.setToolTip(session_title(self.project, self.cli, self.store.home) if enabled else reason)

    def has_previous_conversation(self) -> bool:
        """Pasta compartilhada com outro perfil: só conta conversa aberta por este perfil."""
        try:
            return has_conversation_for(self.store.home, self.cli, project_path(self.project["path"]))
        except (LauncherError, OSError, ValueError):
            return False

    def build_conversation_menu(self, actions):
        """Seta ao lado de 'Retomar última' com 'Escolher conversa…' (lista as conversas do CLI nesta pasta)."""
        self.conversation_button = QToolButton()
        self.conversation_button.setText("▾")
        self.conversation_button.setMinimumHeight(44)
        self.conversation_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.conversation_button.setAccessibleName("Escolher conversa")
        self.conversation_button.setToolTip("Escolher qual conversa retomar")
        self.conversation_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.conversation_button)
        menu.addAction("Escolher conversa…", self.choose_conversation)
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
            self.show_info(f"Ainda não há conversa do {registry.get(self.cli).LABEL} nesta pasta.")
            return
        dialog = ConversationsDialog(found, self, registry.get(self.cli).LABEL)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected():
            self.launch_session(True, conversation=dialog.selected())

    @Slot()
    def refresh_catalog(self, force=False):
        if self.refreshing or not self.gateway:
            return
        self.refreshing = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Verificando…")
        self.connection.setText("Verificando conexão…")
        self.update_actions()
        self.refresh_task = RefreshTask(self.store.home, self.gateway.connection["id"], bool(force))
        self.refresh_task.signals.completed.connect(self.finish_refresh)
        QThreadPool.globalInstance().start(self.refresh_task)

    @Slot(object, str)
    def finish_refresh(self, result, connection):
        self.refreshing = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Atualizar")
        if not self.gateway or connection != self.gateway.connection["id"]:
            self.update_actions()
            return  # resposta de uma conexão que já foi trocada
        catalog, error = (None, str(result)) if isinstance(result, Exception) else (result, "")
        self.online = not error
        self.connection.setText(f"Conectado - {self.gateway.name}" if self.online else f"Indisponível - {self.gateway.name}")
        self.connection.setProperty("offline", not self.online)
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)
        if catalog is not None:
            self.catalog = catalog
        elif self.catalog is not None:
            self.catalog.verified = False
        self.notice.setVisible(bool(error))
        self.notice.setText(f"Não foi possível acessar a conexão. Verifique o host e clique em Atualizar. {error}" if error else "")
        self.notice.setToolTip(error)
        self.footer.setText(self.catalog.status.replace("atualizar: r", "atualizar: F5") if self.online and self.catalog else error)
        self.populate_providers()
        self.populate_models()

    def launch_session(self, resume=False, conversation=None):
        if not self.project or not self.gateway or not self.selected_model or not self.online or self.refreshing:
            return
        self.notice.hide()
        harness = registry.get(self.cli)
        if harness.KIND != "terminal":
            self.open_outside_terminal(harness)
            return
        bypass_permissions = self.bypass_checkbox.isChecked()
        try:
            process = start_session(self.store, self.project, self.cli, self.selected_model.id, resume,
                                    bypass_permissions=bypass_permissions, connection=self.gateway.connection["id"],
                                    **({"conversation": conversation} if conversation else {}))
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
        self.footer.setText(f"Janela aberta: {title}{mode}. A janela verifica a conexão antes de iniciar.")
        note = self.shared_resume_note() if resume else None
        if note:
            self.show_info(note)
        if terminal.find(ROOT) is None:
            self.ensure_terminal(opened_in_console=True)

    def install_cli(self):
        if self.installing_cli:
            return
        self.installing_cli = True
        self.footer.setText(f"Instalando {registry.get(self.cli).LABEL} sem admin…")
        self.update_actions()
        self.install_task = InstallCliTask(self.cli)
        self.install_task.signals.completed.connect(self.finish_install_cli)
        QThreadPool.globalInstance().start(self.install_task)

    @Slot(object, str)
    def finish_install_cli(self, found, log):
        self.installing_cli = False
        if found:
            self.notice.hide()
            self.footer.setText(f"Instalado: {found}")
        else:
            self.show_error(log or "Não foi possível instalar o CLI.")
        self.update_actions()

    def check_sessions(self):
        finished = [(process, title) for process, title in self.sessions if process.poll() is not None]
        self.sessions = [(process, title) for process, title in self.sessions if process.poll() is None]
        if finished:
            self.footer.setText(f"Sessão encerrada: {finished[-1][1]} · {len(self.sessions)} janela(s) ainda abertas")

    def eventFilter(self, watched, event):
        # *ToParent: chegam ao mostrar/esconder a faixa mesmo com a janela ainda invisível (Show/Hide não).
        if watched is self.notice and event.type() in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self.copy_error.setVisible(event.type() == QEvent.Type.ShowToParent)
        return super().eventFilter(watched, event)

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

    def copy_notice(self):
        QApplication.clipboard().setText(self.problem_report(self.notice.toolTip() or self.notice.text()))
        self.footer.setText("Erro copiado com o contexto: cole num chat de IA ou mande para o suporte.")

    def ensure_terminal(self, opened_in_console: bool = False):
        """Sem Terminal: instala sozinho em segundo plano; só avisa de erro se todas as formas falharem."""
        if terminal.find(ROOT):
            return
        record_terminal_problem(self.problem_report("Windows Terminal não encontrado pela interface."))
        if opened_in_console:
            self.show_info("Esta sessão abriu em um console separado porque o Windows Terminal não foi encontrado. "
                           "Instalando o Terminal agora, sem admin; as próximas sessões abrem nele.")
        if self.terminal_installing:
            return
        self.terminal_installing = True
        self.terminal_task = TerminalTask(ROOT)
        self.terminal_task.signals.completed.connect(self.finish_terminal)
        QThreadPool.globalInstance().start(self.terminal_task)

    @Slot(object, str)
    def finish_terminal(self, found, log):
        self.terminal_installing = False
        if found:
            self.show_info(f"Windows Terminal pronto ({found}). As próximas sessões abrem nele.")
            return
        details = "\n".join(line.strip() for line in log.splitlines() if "[falhou]" in line or "ambiente:" in line)
        self.show_error("Windows Terminal indisponível: nenhuma forma de instalação funcionou nesta máquina, então as "
                        "sessões abrem em consoles separados. Use \"Copiar erro\" para ver o motivo."
                        + (f"\n{details}" if details else ""))
        record_terminal_problem(self.problem_report(log or "Instalação do Windows Terminal falhou sem detalhes."))

    def show_info(self, message):
        self.notice.setText(message)
        self.notice.setToolTip(message)
        self.notice.show()

    def show_error(self, message):
        self.notice.setText(message)
        self.notice.setToolTip(message)
        self.notice.show()

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
            self.notice.hide()
            warning = self.shared_warning(project["path"])
            if warning:
                self.show_info(warning)
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
            self.footer.setText(f"{added} projeto(s) importado(s); {skipped} já existentes ou inválidos. Nenhuma credencial importada.")
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))

    def project_menu(self, position):
        item = self.project_list.itemAt(position)
        if item is None:
            return
        self.project_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Abrir pasta", self.open_project)
        menu.addAction("Editar…", self.edit_project)
        menu.addAction("Remover do launcher…", self.remove_project)
        menu.exec(self.project_list.viewport().mapToGlobal(position))

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
                self.notice.hide()
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
        self.footer.setText(message)

    # Perfis de trabalho

    def populate_profiles(self):
        current = profiles.profile_of_home(self.store.home, self.profile_base)
        self.profile_picker.blockSignals(True)
        self.profile_picker.clear()
        for item in profiles.list_profiles(self.profile_base):
            self.profile_picker.addItem(item["name"], item["id"])
            if item["color"]:
                self.profile_picker.setItemIcon(self.profile_picker.count() - 1, icon("folder", item["color"]))
        self.profile_picker.addItem("Novo perfil…", NEW_PROFILE)
        self.profile_picker.setCurrentIndex(max(0, self.profile_picker.findData(current["id"])))
        self.profile_picker.blockSignals(False)
        extra = current["id"] != profiles.DEFAULT
        self.rename_profile_action.setEnabled(extra)
        self.color_profile_action.setEnabled(extra)
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
        self.notice.hide()
        self.connection_picker.blockSignals(True)
        self.connection_picker.clear()
        self.connection_picker.blockSignals(False)
        self.project_search.clear()
        for action, key in ((self.bypass_preference, "bypass_default"), (self.update_preference, "auto_update")):
            action.blockSignals(True)
            action.setChecked(self.store.preference(key))
            action.blockSignals(False)
        self.populate_profiles()
        self.populate_projects(self.store.data["last_project"])
        self.populate_connections()
        name = profiles.profile_of_home(self.store.home, self.profile_base)["name"]
        self.footer.setText(f"Perfil {name}: só os projetos e as conexões dele aparecem aqui.")

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
        if current["id"] == profiles.DEFAULT:
            return
        if color is None:
            from PySide6.QtWidgets import QColorDialog
            chosen = QColorDialog.getColor(QColor(current["color"] or profiles.COLORS[0]), self, "Cor do perfil")
            if not chosen.isValid():
                return
            color = chosen.name()
        try:
            profiles.set_color(current["id"], color, self.profile_base)
        except (LauncherError, OSError) as error:
            self.show_error(str(error))
            return
        self.populate_profiles()

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
        self.footer.setText(f"Removendo as conexões do perfil {current['name']} do Copilot app e do VS Code… "
                            "(pode levar alguns segundos)")
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
        messages = [str(messages)] if isinstance(messages, Exception) else list(messages or [])
        self.set_profile_busy(False)
        self.cleanup_task = None
        from agent_launcher.harnesses.cleanup import release_profile_endpoints
        messages += release_profile_endpoints(profiles.home_of(current["id"], self.profile_base))
        try:
            profiles.remove(current["id"], delete_data, self.profile_base)
        except (LauncherError, OSError) as error:
            self.show_error(" ".join([*messages, str(error)]))
            return
        self.switch_profile(profiles.DEFAULT)
        if messages:  # o que foi feito e os avisos de cada passo (ex.: "VS Code: arquivo preso"), sem bloquear
            self.footer.setText(" ".join(messages))
            self.show_info(" ".join(messages))

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
            self.footer.setText(profile_package.describe(result))
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
        self.footer.setText(f"Perfil {current['name']} exportado sem chaves em {result['path']}.")
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
        for harness in registry.all():
            found = installed_path(harness.ID)
            lines.append(f"{harness.LABEL}: encontrado\n{found}" if found else
                         f"{harness.LABEL}: não encontrado. {harness.MISSING_HINT}".rstrip())
        lines.extend(f"Adaptador do usuário não carregado: {error}" for error in registry.errors())
        lines.extend(["", self.catalog.status if self.catalog else "Sem catálogo.", "",
                      "As chaves das conexões ficam cifradas (DPAPI) e nunca aparecem aqui."])
        QMessageBox.information(self, "Diagnóstico", "\n".join(lines))


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
    palette = application.palette()
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#60725f"))
    application.setPalette(palette)
    application.setStyleSheet(STYLE)
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

