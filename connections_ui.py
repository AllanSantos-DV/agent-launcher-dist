from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core import Gateway, LauncherError, Store


def _label(text: str, role: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if role:
        widget.setObjectName(role)
    return widget


class _Signals(QObject):
    done = Signal(object, str)


class _Task(QRunnable):
    """Teste/gravação de conexão fora da thread da interface (a verificação consulta a rede)."""

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.signals = _Signals()

    @Slot()
    def run(self):
        try:
            self.signals.done.emit(self.job(), "")
        except Exception as error:
            self.signals.done.emit(None, str(error))


class ConnectionForm(QDialog):
    """Formulário gerado pelos FIELDS do conector. Editar: chave em branco mantém a salva."""

    def __init__(self, home: Path, connection: dict | None = None, parent=None):
        super().__init__(parent)
        self.home = home
        self.connection = connection
        self.saved = None
        self.inputs = {}
        self.modules, errors = Store(home).connectors()
        self.setWindowTitle("Editar conexão" if connection else "Nova conexão")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        self.kind = QComboBox()
        self.kind.setAccessibleName("Tipo de provedor")
        for name, module in self.modules.items():
            self.kind.addItem(module.LABEL, name)
        if connection:
            self.kind.setCurrentIndex(max(0, self.kind.findData(connection["connector"])))
        layout.addWidget(_label("Tipo de provedor", "section"))
        layout.addWidget(self.kind)
        self.hint = _label("", "muted")
        layout.addWidget(self.hint)
        if errors:
            layout.addWidget(_label("Conectores do usuário com erro: " + "; ".join(errors), "notice"))
        self.name = QLineEdit(connection["name"] if connection else "")
        self.name.setAccessibleName("Nome da conexão")
        self.name.setPlaceholderText("Ex.: Meu túnel, OpenRouter pessoal")
        self.fields_host = QWidget()
        self.form = QFormLayout(self.fields_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setVerticalSpacing(8)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addWidget(self.fields_host)
        self.notice = _label("", "notice")
        self.notice.hide()
        layout.addWidget(self.notice)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.save_button = QPushButton("Testar e salvar")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.kind.currentIndexChanged.connect(self.build_fields)
        self.build_fields()

    @property
    def module(self):
        return self.modules[self.kind.currentData()]

    def build_fields(self):
        typed = self.name.text()  # removeRow apaga o widget antigo; o nome digitado sobrevive à troca de tipo
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs = {}
        module = self.module
        same = self.connection is not None and self.connection["connector"] == module.NAME
        stored = self.connection["options"] if same else {}
        self.hint.setText(getattr(module, "HINT", ""))
        labels = {candidate.LABEL for candidate in self.modules.values()}
        self.name = QLineEdit(typed if typed and typed not in labels else module.LABEL)
        self.name.setAccessibleName("Nome da conexão")
        self.form.addRow("Nome", self.name)
        for item in module.FIELDS:
            label = item["label"] + (" *" if item.get("required") else "")
            if item.get("type") == "bool":
                widget = QCheckBox(item["label"])
                widget.setChecked(bool(stored.get(item["name"], item.get("default", False))))
                label = ""
            else:
                widget = QLineEdit(str(stored.get(item["name"], item.get("default", ""))) if not item.get("secret") else "")
                widget.setPlaceholderText(("Salva • deixe em branco para manter" if same else "")
                                          if item.get("secret") else item.get("placeholder", ""))
                if item.get("secret"):
                    widget.setEchoMode(QLineEdit.EchoMode.Password)
            widget.setAccessibleName(item["label"])
            if item.get("hint"):
                widget.setToolTip(item["hint"])
            self.inputs[item["name"]] = (item, widget)
            self.form.addRow(label, widget)
            if item.get("hint"):
                self.form.addRow("", _label(item["hint"], "muted"))

    def values(self) -> dict:
        values = {}
        editing = self.connection is not None and self.connection["connector"] == self.module.NAME
        for key, (item, widget) in self.inputs.items():
            if isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif item.get("secret") and editing and not widget.text():
                values[key] = None
            else:
                values[key] = widget.text()
        return values

    def save(self):
        name, connector, values = self.name.text(), self.module.NAME, self.values()
        reference = self.connection["id"] if self.connection else None
        home = self.home
        self.save_button.setEnabled(False)
        self.save_button.setText("Testando…")
        self.notice.hide()
        self.task = _Task(lambda: Store(home).save_connection(name, connector, values, reference))
        self.task.signals.done.connect(self.finished)
        QThreadPool.globalInstance().start(self.task)

    @Slot(object, str)
    def finished(self, connection, error):
        self.save_button.setEnabled(True)
        self.save_button.setText("Testar e salvar")
        if error:
            self.notice.setText(error)
            self.notice.show()
            return
        self.saved = connection
        self.accept()


class ConnectionsDialog(QDialog):
    def __init__(self, home: Path, parent=None):
        super().__init__(parent)
        self.home = home
        self.setWindowTitle("Conexões")
        self.setMinimumSize(620, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_label("Conexões com provedores", "title"))
        layout.addWidget(_label("Cada conexão é um host de modelos: LiteLLM ou host próprio, 9router, OmniRoute, "
                                "OpenRouter, Anthropic, OpenAI ou qualquer host compatível. As chaves ficam cifradas "
                                "para o seu usuário do Windows.", "muted"))
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setAccessibleName("Conexões cadastradas")
        self.list.currentItemChanged.connect(self.update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.edit())
        body.addWidget(self.list, 1)
        buttons = QVBoxLayout()
        self.buttons = {}
        for key, text, callback in (("add", "Adicionar…", self.add), ("edit", "Editar…", self.edit),
                                    ("test", "Testar", self.test), ("default", "Tornar padrão", self.make_default),
                                    ("remove", "Remover…", self.remove)):
            widget = QPushButton(text)
            widget.clicked.connect(callback)
            buttons.addWidget(widget)
            self.buttons[key] = widget
        self.buttons["add"].setObjectName("primary")
        buttons.addStretch()
        body.addLayout(buttons)
        layout.addLayout(body, 1)
        self.status = _label("", "muted")
        layout.addWidget(self.status)
        close = QPushButton("Fechar")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.populate()

    def populate(self, preferred: str | None = None):
        store = Store(self.home)
        modules, _errors = store.connectors()
        self.list.clear()
        for connection in store.connections:
            module = modules.get(connection["connector"])
            default = " • padrão" if connection["id"] == store.data["default_connection"] else ""
            item = QListWidgetItem(f"{connection['name']}{default}\n{module.LABEL if module else connection['connector']}"
                                   f" • {connection['options'].get('base_url', '')}")
            item.setData(Qt.ItemDataRole.UserRole, connection["id"])
            self.list.addItem(item)
            if connection["id"] == preferred:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.status.setText("" if self.list.count() else "Nenhuma conexão ainda. Clique em Adicionar.")
        self.update_buttons()

    def current(self) -> dict | None:
        item = self.list.currentItem()
        return Store(self.home).find_connection(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def update_buttons(self, *_args):
        for key in ("edit", "test", "default", "remove"):
            self.buttons[key].setEnabled(self.list.currentItem() is not None)

    def add(self):
        form = ConnectionForm(self.home, parent=self)
        if form.exec() and form.saved:
            self.populate(form.saved["id"])

    def edit(self):
        connection = self.current()
        if connection:
            form = ConnectionForm(self.home, connection, self)
            if form.exec() and form.saved:
                self.populate(form.saved["id"])

    def test(self):
        connection = self.current()
        if not connection:
            return
        home = self.home
        self.status.setText(f"Testando {connection['name']}…")
        self.task = _Task(lambda: Gateway(Store(home), Store(home).find_connection(connection["id"])).verify())
        self.task.signals.done.connect(lambda _result, error: self.status.setText(
            f"{connection['name']}: {error}" if error else f"{connection['name']}: conexão respondeu."))
        QThreadPool.globalInstance().start(self.task)

    def make_default(self):
        connection = self.current()
        if connection:
            Store(self.home).set_default_connection(connection["id"])
            self.populate(connection["id"])

    def remove(self):
        connection = self.current()
        if not connection:
            return
        answer = QMessageBox.question(self, "Remover conexão", f"Remover {connection['name']} e a chave salva?\n\n"
                                      "Os projetos continuam cadastrados.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                Store(self.home).remove_connection(connection["id"])
            except LauncherError as error:
                self.status.setText(str(error))
            self.populate()
