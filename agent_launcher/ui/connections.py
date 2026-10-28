from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.endpoint import registry as endpoint_registry


def _label(text: str, role: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if role:
        widget.setObjectName(role)
    return widget


CLEAN_NOTE = ("Se o Copilot app estiver aberto, ele é fechado e aberto de novo (inclusive o ícone da bandeja). O "
              "launcher faz backup do banco do app antes e não mexe nos seus outros providers.")


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


class EndpointPanel(QWidget):
    """Endpoint local da conexão selecionada (porta, estado, requisições, Iniciar/Parar, iniciar com o Windows).

    Só aparece para conexão com porta gravada (já usada por VS Code ou Copilot app). O estado é lido ao abrir a tela,
    depois de cada ação e no botão Atualizar; sem polling. Tudo que consulta a rede ou espera o processo roda fora da
    thread da interface (ensure_running espera até 15 s)."""

    def __init__(self, home: Path, parent=None):
        super().__init__(parent)
        self.home = home
        self.states: dict[str, dict] = {}
        self.connection_id: str | None = None
        self.busy = False
        self.tasks: set = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(_label("Endpoint local", "section"))
        self.summary = _label("", "muted")
        self.summary.setAccessibleName("Estado do endpoint local")
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.toggle_button = QPushButton("Iniciar")
        self.toggle_button.clicked.connect(self.toggle)
        row.addWidget(self.toggle_button)
        self.autostart_box = QCheckBox("Iniciar com o Windows")
        self.autostart_box.clicked.connect(self.set_autostart)  # clicked: só a ação do usuário, não o setChecked
        row.addWidget(self.autostart_box)
        row.addStretch()
        self.refresh_button = QPushButton("Atualizar")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.notice = _label("", "notice")
        self.notice.hide()
        layout.addWidget(self.notice)
        self.state_error = ""  # leitura do estado: vale para todas as conexões até a próxima leitura que der certo
        self.action_errors: dict[str, str] = {}  # erro de Iniciar/Parar/autostart, por conexão
        self.hide()

    def render_notice(self):
        """Erros ativos desta conexão: o da leitura do estado e o da última ação nela (o de outra conexão não aparece)."""
        text = "\n".join(filter(None, [self.state_error, self.action_errors.get(self.connection_id or "", "")]))
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _start(self, job, slot):
        task = _Task(job)
        self.tasks.add(task)
        task.signals.done.connect(slot)
        task.signals.done.connect(lambda *_args, task=task: self.tasks.discard(task))
        QThreadPool.globalInstance().start(task)

    def _set_busy(self, busy: bool, text: str = ""):
        self.busy = busy
        for widget in (self.toggle_button, self.autostart_box, self.refresh_button):
            widget.setEnabled(not busy)
        if busy and text:
            self.summary.setText(text)

    def refresh(self):
        home = self.home

        def job():
            connections = Store(home).connections
            return {connection["id"]: state
                    for connection, state in zip(connections, endpoint_registry.status(home, connections))}
        self._set_busy(True)
        self._start(job, self.refreshed)

    @Slot(object, str)
    def refreshed(self, states, error):
        self._set_busy(False)
        if error:
            self.state_error = f"Estado do endpoint: {error}"
        else:
            self.state_error = ""
            self.states = states or {}
        self.show_for(self.connection_id)

    def show_for(self, connection_id: str | None):
        self.connection_id = connection_id
        self.render_notice()
        state = self.states.get(connection_id) if connection_id else None
        if not state or not state.get("port"):
            self.hide()
            return
        running = bool(state.get("running"))
        parts = [f"Porta {state['port']}", "rodando" if running else "parado"]
        if running and state.get("requests") is not None:
            parts.append(f"{state['requests']} requisições")
        if not self.busy:
            self.summary.setText(" • ".join(parts) + (f"\n{state['url']}" if state.get("url") else ""))
        self.toggle_button.setText("Parar" if running else "Iniciar")
        self.autostart_box.setChecked(bool(state.get("autostart")))
        self.show()

    def toggle(self):
        connection_id = self.connection_id
        state = self.states.get(connection_id or "")
        if not state:
            return
        home = self.home
        self.action_errors.pop(connection_id, None)
        self.render_notice()
        if state.get("running"):
            self._set_busy(True, "Parando o endpoint…")
            self._start(lambda: endpoint_registry.stop(home, connection_id), self._acted_for(connection_id))
        else:
            self._set_busy(True, "Iniciando o endpoint…")
            self._start(lambda: endpoint_registry.ensure_running(home, connection_id), self._acted_for(connection_id))

    def set_autostart(self, enabled: bool):
        connection_id = self.connection_id
        if not connection_id:
            return
        home = self.home
        self.action_errors.pop(connection_id, None)
        self.render_notice()
        self._set_busy(True)
        self._start(lambda: endpoint_registry.autostart(home, connection_id, bool(enabled)), self._acted_for(connection_id))

    def _acted_for(self, connection_id):
        """A resposta chega depois: o erro é da conexão que disparou a ação, não da que está selecionada agora."""
        return lambda result, error: self.acted(result, error, connection_id)

    @Slot(object, str)
    def acted(self, _result, error, connection_id=None):
        connection_id = connection_id or self.connection_id
        if error and connection_id:
            self.action_errors[connection_id] = error
            self.render_notice()
        self.refresh()


class ConnectionsDialog(QDialog):
    def __init__(self, home: Path, parent=None):
        super().__init__(parent)
        self.home = home
        self.cleaning = False
        self.cleanup_task = None
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
        self.endpoint = EndpointPanel(home, self)
        layout.addWidget(self.endpoint)
        self.errors: dict[str, str] = {}  # origem ("limpeza", "teste") -> erro; some só quando a origem se resolve
        self.error_notice = _label("", "notice")  # não some com a lista que recarrega nem com a linha de status
        self.error_notice.setWordWrap(True)
        self.error_notice.hide()
        layout.addWidget(self.error_notice)
        self.statuses: dict[str, str] = {}  # origem ("lista", "teste", "limpeza") -> linha; convivem, a mesma substitui a sua
        self.status = _label("", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton("Fechar")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.populate()
        self.endpoint.refresh()

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
        self.set_status("lista", "" if self.list.count() else "Nenhuma conexão ainda. Clique em Adicionar.")
        self.update_buttons()

    def current(self) -> dict | None:
        item = self.list.currentItem()
        return Store(self.home).find_connection(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def update_buttons(self, *_args):
        for key in ("edit", "test", "default", "remove"):
            self.buttons[key].setEnabled(self.list.currentItem() is not None)
        item = self.list.currentItem()
        self.endpoint.show_for(item.data(Qt.ItemDataRole.UserRole) if item else None)

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
        self.set_status("teste", f"Testando {connection['name']}…")
        self.task = _Task(lambda: Gateway(Store(home), Store(home).find_connection(connection["id"])).verify())
        self.task.signals.done.connect(lambda _result, error: self.show_test_result(connection["name"], error))
        QThreadPool.globalInstance().start(self.task)

    def show_test_result(self, name: str, error: str):
        if error:
            self.errors["teste"] = f"{name}: {error}"
            self.set_status("teste", "")
        else:
            self.errors.pop("teste", None)
            self.set_status("teste", f"{name}: conexão respondeu.")
        self.render_errors()

    def set_status(self, origin: str, text: str):
        """Linha de status: cada origem tem a sua (o fim de uma não apaga a das outras)."""
        if text:
            self.statuses[origin] = text
        else:
            self.statuses.pop(origin, None)
        self.status.setText("\n".join(self.statuses.values()))

    def render_errors(self):
        text = "\n".join(self.errors.values())
        self.error_notice.setText(text)
        self.error_notice.setVisible(bool(text))

    def make_default(self):
        connection = self.current()
        if connection:
            Store(self.home).set_default_connection(connection["id"])
            self.populate(connection["id"])

    def done(self, result: int):
        """Não fecha no meio da limpeza nos destinos (Fechar, Esc ou o X): o fim dela atualiza este diálogo."""
        if self.cleaning:
            return
        super().done(result)

    def _set_cleaning(self, cleaning: bool):
        """Durante a limpeza nos destinos: lista e botões travados (a conexão ainda existe até o fim)."""
        self.cleaning = cleaning
        self.list.setEnabled(not cleaning)
        for widget in self.buttons.values():
            widget.setEnabled(not cleaning)
        if not cleaning:
            self.update_buttons()

    def clean_destinations(self, connection: dict, then=None) -> None:
        """Limpeza nos destinos em segundo plano: fechar o Copilot app e esperar o banco pode levar dezenas de
        segundos. A pergunta já foi feita na thread da interface (remove), então a tarefa confirma sozinha
        (confirm=lambda *_: True) e nunca abre QMessageBox fora da thread da interface. `then(messages)` roda na
        thread da interface no fim."""
        from agent_launcher.harnesses.cleanup import cleanup_destinations
        home = self.home
        self._set_cleaning(True)
        self.set_status("limpeza", f"Removendo {connection['name']} do Copilot app e do VS Code… (pode levar alguns segundos)")
        task = _Task(lambda: cleanup_destinations(Store(home), connection, lambda *_: True))
        self.cleanup_task = task

        def finished(messages, error):
            from agent_launcher.harnesses.cleanup import Failure
            messages = list(messages or []) + ([Failure(error)] if error else [])
            self._set_cleaning(False)
            self.show_cleanup_result(messages)
            self.cleanup_task = None
            if then:
                then(messages)
        task.signals.done.connect(finished)
        QThreadPool.globalInstance().start(task)

    def show_cleanup_result(self, messages):
        """O que foi feito vai para a linha de status; o que falhou, para a faixa própria, até a próxima limpeza."""
        from agent_launcher.harnesses.cleanup import Failure
        failed = [message for message in messages if isinstance(message, Failure)]
        done = [message for message in messages if not isinstance(message, Failure)]
        if failed:
            self.errors["limpeza"] = " ".join(failed)
        else:
            self.errors.pop("limpeza", None)
        self.render_errors()
        self.set_status("limpeza", " ".join(done))

    def remove(self):
        connection = self.current()
        if not connection:
            return
        answer = QMessageBox.question(self, "Remover conexão", f"Remover {connection['name']} e a chave salva?\n\n"
                                      "Os projetos continuam cadastrados.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            clean = QMessageBox.question(self, "Remover conexão", "Remover também do Copilot app e do VS Code (e parar "
                                         f"o endpoint local desta conexão)?\n\n{CLEAN_NOTE}",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.Yes)
            if clean == QMessageBox.StandardButton.Yes:
                self.clean_destinations(connection, lambda messages: self.finish_remove(connection, messages))
            else:
                self.finish_remove(connection, [])

    def finish_remove(self, connection: dict, messages: list[str]):
        from agent_launcher.harnesses.cleanup import release_endpoint
        from agent_launcher.harnesses.cleanup import Failure
        messages = [*messages, *release_endpoint(self.home, connection["id"])]
        try:
            Store(self.home).remove_connection(connection["id"])
        except LauncherError as error:
            messages = [*messages, Failure(str(error))]
        self.populate()
        self.show_cleanup_result(messages)
