from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

import clis
import terminal
import updater
from catalog import base_model, token_label
from connections_ui import ConnectionsDialog
from core import ROOT, Gateway, LauncherError, Store, has_previous_conversation, project_path
from desktop import CLI_NAMES, session_title, start_session
from ui_theme import GREEN, MUTED, STYLE, CliButton, ElidedLabel, icon
from version import APP_NAME, APP_USER_MODEL_ID, VERSION


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
            found = clis.ensure(self.cli, log=lines.append)
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


class LauncherWindow(QMainWindow):
    def __init__(self, store: Store | None = None, auto_refresh: bool = True):
        super().__init__()
        self.store = store or Store()
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
        self.update_status = "não verificada nesta execução"
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(ROOT / "assets" / "launcher.ico")))
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1220, available.width() - 40), min(850, available.height() - 60))
        self.setMinimumSize(990, 680)
        self._build()
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
        rail = QVBoxLayout(sidebar)
        rail.setContentsMargins(20, 26, 20, 20)
        rail.setSpacing(13)
        brand = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(QIcon(str(ROOT / "assets/launcher.svg")).pixmap(38, 38))
        brand.addWidget(mark)
        brand.addWidget(label(APP_NAME, "brand"))
        brand.addStretch()
        brand.addWidget(label(VERSION, "railMuted"))
        rail.addLayout(brand)
        rail.addSpacing(20)
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
        cli_row = QHBoxLayout()
        cli_row.setSpacing(12)
        self.cli_group = QButtonGroup(self)
        self.cli_buttons = {}
        for cli, subtitle, symbol in (("claude", "Anthropic Messages", "terminal"),
                                      ("codex", "OpenAI Responses", "code")):
            widget = CliButton(CLI_NAMES[cli], subtitle, symbol)
            self.cli_group.addButton(widget)
            self.cli_buttons[cli] = widget
            widget.clicked.connect(lambda checked=False, chosen=cli: self.select_cli(chosen))
            cli_row.addWidget(widget)
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

    def start_auto_update(self):
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
        preferred = self.project["last_cli"] if self.project else self.cli
        clis = sorted(self.gateway.clis) if self.gateway else ["claude"]
        self.select_cli(preferred if preferred in clis else clis[0])
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
            budget = token_label(record.max_input_tokens).replace(".", ",")
            suffix = "   ·   [1m] aplicado pelo Claude" if self.cli == "claude" and record.one_million else ""
            self.context_info.setText(f"Entrada declarada: {budget} tokens{suffix}")
            if self.cli == "claude":
                mapped = self.catalog.claude_families(record)
                self.family_info.setText("Famílias: " + "   ·   ".join(
                    f"{family.title()} {token_label(model.max_input_tokens)}" for family, model in mapped.items()))
            else:
                self.family_info.setText("Contexto informado pelo host. O Codex mantém sua margem interna.")
        else:
            self.context_info.setText("Contexto não confirmado. Nenhum aumento será forçado." if record else "Escolha um modelo da lista.")
            self.family_info.setText("Este host não informa limites (ou o cache venceu); os CLIs usam os seus próprios." if record else "")
        self.update_actions()

    def update_actions(self):
        missing = clis.find(self.cli) is None
        self.install_cli_button.setVisible(missing or self.installing_cli)
        self.install_cli_button.setEnabled(not self.installing_cli)
        self.install_cli_button.setText(f"Instalando {clis.NAMES[self.cli]}…" if self.installing_cli
                                        else f"Instalar {clis.NAMES[self.cli]}")
        enabled = bool(self.project and self.gateway and self.selected_model and self.online and not self.refreshing
                       and not missing)
        self.launch_button.setEnabled(enabled)
        resumable = enabled and self.has_previous_conversation()
        self.resume_button.setEnabled(resumable)
        self.bypass_checkbox.setEnabled(enabled)
        bypass = self.bypass_checkbox.isChecked()
        risk = ("Executa comandos sem confirmações e sem sandbox no Codex." if self.cli == "codex" else
                "Ignora as verificações de permissão do Claude Code.")
        description = risk + " Vale só para a próxima abertura; o padrão fica em Opções → Bypass marcado por padrão."
        self.bypass_checkbox.setToolTip(description)
        self.bypass_checkbox.setAccessibleDescription(description)
        hint = "Sem sandbox ou confirmações" if self.cli == "codex" else "Sem pedir permissão"
        self.session_hint.setText(hint if bypass else "Terminal com título do projeto")
        self.session_hint.setToolTip(description if bypass else
                                     session_title(self.project, self.cli) if self.project else "Selecione um projeto")
        self.session_hint.setProperty("bypass", bypass)
        self.session_hint.style().unpolish(self.session_hint)
        self.session_hint.style().polish(self.session_hint)
        reason = "Retoma a última sessão nativa deste CLI na pasta do projeto." if enabled else "Escolha projeto, conexão e modelo; a conexão precisa responder."
        self.resume_button.setToolTip(reason if resumable or not enabled else
                                      f"Ainda não há conversa do {clis.NAMES[self.cli]} nesta pasta para retomar: use Nova sessão.")
        self.launch_button.setToolTip(session_title(self.project, self.cli) if enabled else reason)

    def has_previous_conversation(self) -> bool:
        try:
            return has_previous_conversation(self.cli, project_path(self.project["path"]))
        except (LauncherError, OSError, ValueError):
            return False

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

    def launch_session(self, resume=False):
        if not self.project or not self.gateway or not self.selected_model or not self.online or self.refreshing:
            return
        self.notice.hide()
        bypass_permissions = self.bypass_checkbox.isChecked()
        try:
            process = start_session(self.store, self.project, self.cli, self.selected_model.id, resume,
                                    bypass_permissions=bypass_permissions, connection=self.gateway.connection["id"])
        except (LauncherError, OSError, ValueError) as error:
            self.show_error(str(error))
            return
        title = session_title(self.project, self.cli)
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
        if terminal.find(ROOT) is None:
            self.ensure_terminal(opened_in_console=True)

    def install_cli(self):
        if self.installing_cli:
            return
        self.installing_cli = True
        self.footer.setText(f"Instalando {clis.NAMES[self.cli]} pelo instalador oficial (sem admin)…")
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

    def show_diagnostics(self):
        from core import resolve_cli
        lines = [f"{APP_NAME} {VERSION}", f"Dados: {self.store.home}", "",
                 "Conexão: " + (f"{self.gateway.name} ({self.gateway.module.LABEL})" if self.gateway else "nenhuma"),
                 "Endereço: " + (self.gateway.url if self.gateway else "-"),
                 "Estado: " + ("respondeu" if self.online else "não confirmado"),
                 "Windows Terminal: " + (terminal.find(ROOT) or "ausente (sessões em consoles separados)"),
                 "Atualização automática: " + self.update_status, ""]
        for cli, name in CLI_NAMES.items():
            try:
                executable = resolve_cli(cli)
                lines.append(f"{name}: encontrado\n{executable[-1]}")
            except LauncherError as error:
                lines.append(f"{name}: {error}")
        lines.extend(["", self.catalog.status if self.catalog else "Sem catálogo.", "",
                      "As chaves das conexões ficam cifradas (DPAPI) e nunca aparecem aqui."])
        QMessageBox.information(self, "Diagnóstico", "\n".join(lines))


UPDATE_INTERVAL_MS = 4 * 60 * 60 * 1000
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


def main():
    application = create_application()
    window = LauncherWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
