from __future__ import annotations

import functools
import itertools
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFontDatabase, QTextOption
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QApplication, QFileDialog, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from agent_launcher.connectors import ConnectorError, LoginPending, QuotaExhausted
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.gateway import Gateway
from agent_launcher.domain.store import Store
from agent_launcher.endpoint import registry as endpoint_registry
from agent_launcher.ui.background import BackgroundTask, run_task
from agent_launcher.ui.placement import GrowingDialog, reveal
from agent_launcher.ui.theme import ReadableCombo, StatusLine, bind_style, label, plural


_label = functools.partial(label, wrap=True)


CLEAN_NOTE = ("Se o Copilot app estiver aberto, ele é fechado e aberto de novo (inclusive o ícone da bandeja). O "
              "launcher faz backup do banco do app antes e não mexe nos seus outros providers.")


DONE_DELAY_MS = 1500  # o painel de login mostra "Autorizado" por este tempo antes de o formulário fechar


class _Task(BackgroundTask):
    """Teste/gravação de conexão fora da thread da interface (a verificação consulta a rede). A exceção fica em
    `failure_cause`: o login por código (LoginPending) chega à tela pela causa dela, não pelo texto."""

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.failure_cause = None

    def outcome(self):
        return self.job(), ""

    def failure(self, error):
        self.failure_cause = error
        return None, str(error)


def split_cleanup(messages) -> tuple[list, list]:
    """As mensagens de uma limpeza separadas em (o que foi feito, o que falhou), cada lado na ordem em que vieram."""
    from agent_launcher.harnesses.cleanup import Failure
    done = [message for message in messages if not isinstance(message, Failure)]
    return done, [message for message in messages if isinstance(message, Failure)]


def cause_of(task, kind: type):
    """A causa do tipo `kind` que fez a tarefa falhar (a verificação embrulha o erro do conector em LauncherError)."""
    failure = getattr(task, "failure_cause", None)
    while failure is not None and not isinstance(failure, kind):
        failure = failure.__cause__
    return failure


def login_of(task) -> LoginPending | None:
    """O login por código pendente que fez a tarefa falhar, ou None."""
    return cause_of(task, LoginPending)


def quota_warning(connection: dict | None) -> str:
    """Mensagem de cota esgotada gravada na conexão ("" se ela não está nesse estado)."""
    status = (connection or {}).get("status") or {}
    return str(status.get("message") or "") if status.get("state") == "quota_exhausted" else ""


BATCH_STATES = ("ok", "chave recusada", "host fora do ar", "cota esgotada", "aguarda login", "falhou")
CONNECTIONS_HEADER = ("Nome", "Conector", "Endereço", "Padrão", "Estado")


def verdict(task, error: str) -> tuple[str, str]:
    """Estado de uma conexão testada (um de BATCH_STATES) e o texto do erro do conector, que vai para a dica da linha."""
    if not error:
        return "ok", ""
    cause = cause_of(task, ConnectorError)
    detail = str(cause) if cause is not None else error
    if login_of(task) is not None:
        return "aguarda login", detail
    if cause_of(task, QuotaExhausted) is not None:
        return "cota esgotada", detail
    if getattr(cause, "status", None) in (401, 403):
        return "chave recusada", detail
    if getattr(cause, "transient", False):
        return "host fora do ar", detail
    return "falhou", detail


def public_address(url: str) -> str:
    """O endereço sem usuário, senha, consulta nem fragmento ("" se não for um endereço): nada que sirva de segredo sai
    da tela."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if not parts.scheme or not host:
        return ""
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def connections_text(rows: list[list[str]]) -> str:
    """A lista de conexões separada por tabulação: colada numa planilha, cada valor cai na sua coluna."""
    return "\n".join("\t".join(row) for row in [list(CONNECTIONS_HEADER), *rows])


LOG_LINES = 200
SUMMARY_MODELS = 5
SUMMARY_ROWS = 10
LOG_VIEW_STYLE = ("QPlainTextEdit { background: #ffffff; color: #233e32; border: 1px solid #d5ded1; "
                  "border-radius: 8px; padding: 6px; selection-background-color: #d5e6cf; }")
EMPTY_LOG = "O endpoint local ainda não registrou nenhum pedido neste perfil."


def _remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class LoginPanel(QWidget):
    """Login por código na própria tela: endereço (abre no navegador), código (copiável), tempo restante e estado
    (aguardando, autorizado, expirado, erro). Enquanto aguarda, emite `poll` a cada intervalo do provedor para quem o
    usa repetir a verificação, sem mais cliques; ao expirar, para de pedir (um novo código só vem pelo botão)."""

    poll = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.login: LoginPending | None = None
        self.state = ""
        self.next_poll = 0.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(_label("Entrar com o código", "section"))
        self.address_label = _label("", "muted")
        self.address_label.setAccessibleName("Endereço de autorização")
        layout.addWidget(self.address_label)
        self.code_label = QLabel("")
        self.code_label.setTextFormat(Qt.TextFormat.PlainText)
        self.code_label.setAccessibleName("Código de autorização")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setStyleSheet("font-size: 28px; font-weight: 700; letter-spacing: 4px;")
        layout.addWidget(self.code_label)
        self.state_label = _label("", "notice")
        self.state_label.setAccessibleName("Estado do login")
        layout.addWidget(self.state_label)
        row = QHBoxLayout()
        self.open_button = QPushButton("Abrir no navegador")
        self.open_button.clicked.connect(self.open_address)
        row.addWidget(self.open_button)
        self.copy_button = QPushButton("Copiar código")
        self.copy_button.clicked.connect(self.copy_code)
        row.addWidget(self.copy_button)
        row.addStretch()
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.hide()

    def start(self, login: LoginPending) -> None:
        """Mostra o código recebido. O mesmo código de novo só mantém a contagem (a verificação seguinte)."""
        same = self.login is not None and (self.login.code, self.login.uri) == (login.code, login.uri)
        self.login = login
        if not same or self.state != "waiting":
            self.next_poll = time.time() + login.interval
        self.state = "waiting"
        self.address_label.setText(f"Abra {login.uri} e digite o código:")
        self.code_label.setText(login.code)
        self.open_button.setEnabled(self.secure(login.uri))
        self.copy_button.setEnabled(True)
        self.show()
        self.timer.start()
        self.tick()

    @staticmethod
    def secure(uri: str) -> bool:
        return QUrl(uri).scheme() == "https"

    def tick(self) -> None:
        if self.state != "waiting" or self.login is None:
            return
        left = self.login.expires_at - time.time()
        if left <= 0:
            self.expire()
            return
        self.state_label.setText(f"Aguardando a autorização… o código vale por mais {_remaining(left)}.")
        if time.time() >= self.next_poll:
            self.next_poll = time.time() + self.login.interval
            self.poll.emit()

    def expire(self) -> None:
        self.timer.stop()
        self.state = "expired"
        self.state_label.setText("O código expirou. Clique em Testar e salvar para gerar outro.")
        self.copy_button.setEnabled(False)
        self.open_button.setEnabled(False)

    def authorized(self, text: str = "Autorizado. Conexão salva.") -> None:
        self.timer.stop()
        self.state = "authorized"
        self.state_label.setText(text)
        self.copy_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.show()

    def failed(self, error: str) -> None:
        """Erro durante o login: o painel fica com o código (a verificação seguinte retoma o mesmo, enquanto valer)."""
        self.timer.stop()
        self.state = "error"
        retry = "" if "Clique em" in error else "\nClique em Testar e salvar para tentar de novo."
        self.state_label.setText(f"Erro: {error}{retry}")
        self.show()

    def clear(self) -> None:
        self.timer.stop()
        self.login, self.state = None, ""
        self.hide()

    def open_address(self) -> None:
        if self.login is not None and self.secure(self.login.uri):
            QDesktopServices.openUrl(QUrl(self.login.uri))

    def copy_code(self) -> None:
        if self.login is not None:
            QApplication.clipboard().setText(self.login.code)


def copy_name(name: str, taken: set[str]) -> str:
    """Nome livre para a cópia de uma conexão ("X (cópia)", "X (cópia 2)"…), dentro do limite de 60 caracteres."""
    taken = {item.casefold() for item in taken}
    for number in itertools.count(1):
        suffix = " (cópia)" if number == 1 else f" (cópia {number})"
        candidate = name[:60 - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in taken:
            return candidate


class ConnectionForm(QDialog):
    """Formulário gerado pelos FIELDS do conector. Editar: chave em branco mantém a salva. Duplicar (`source`): vem
    preenchido com os campos da conexão de origem e salva uma conexão nova; a chave não é copiada."""

    def __init__(self, home: Path, connection: dict | None = None, parent=None, source: dict | None = None):
        super().__init__(parent)
        self.home = home
        self.connection = connection
        self.source = connection or source
        self.saved = None
        self.inputs = {}
        store = Store(home)
        self.modules, errors = store.connectors()
        self.setWindowTitle("Editar conexão" if connection else "Duplicar conexão" if source else "Nova conexão")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        self.kind = ReadableCombo()
        self.kind.setAccessibleName("Tipo de provedor")
        for name, module in self.modules.items():
            self.kind.addItem(module.LABEL, name)
        if self.source:
            self.kind.setCurrentIndex(max(0, self.kind.findData(self.source["connector"])))
        layout.addWidget(_label("Tipo de provedor", "section"))
        layout.addWidget(self.kind)
        self.hint = _label("", "muted")
        layout.addWidget(self.hint)
        if errors:
            layout.addWidget(_label("Conectores do usuário com erro: " + "; ".join(errors), "notice"))
        initial = connection["name"] if connection else ""
        if source and not connection:
            initial = copy_name(source["name"], {item["name"] for item in store.connections})
        self.name = QLineEdit(initial)
        self.name.setAccessibleName("Nome da conexão")
        self.name.setPlaceholderText("Ex.: Meu proxy, OpenRouter pessoal")
        self.fields_host = QWidget()
        self.form = QFormLayout(self.fields_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setVerticalSpacing(8)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addWidget(self.fields_host)
        self.login_panel = LoginPanel()
        self.login_panel.poll.connect(self.poll_login)
        layout.addWidget(self.login_panel)
        self.notice = _label("", "notice")
        self.notice.hide()
        layout.addWidget(self.notice)
        self.running = False
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.save_button = QPushButton("Testar e salvar")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda: self.save())
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
        stored = self.source["options"] if self.source and self.source["connector"] == module.NAME else {}
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

    def save(self, polling: bool = False):
        """Testar e salvar. Com login por código pendente (`polling`), é a conferência automática: não mexe no botão."""
        if self.running:
            return
        name, connector, values = self.name.text(), self.module.NAME, self.values()
        reference = self.connection["id"] if self.connection else None
        home = self.home
        self.running = True
        if not polling:
            self.save_button.setEnabled(False)
            self.save_button.setText("Testando…")
            self.notice.hide()
            self.login_panel.clear()
        self.task = _Task(lambda: Store(home).save_connection(name, connector, values, reference))
        run_task(self.task, self.saved_result)

    @Slot()
    def poll_login(self):
        self.save(polling=True)

    @Slot(object, str)
    def saved_result(self, connection, error):
        # não pode se chamar `finished`: esse nome é o sinal do QDialog e o connect falha no clique
        self.running = False
        self.save_button.setEnabled(True)
        self.save_button.setText("Testar e salvar")
        if error:
            login = login_of(self.task)
            if login is not None:
                self.notice.hide()
                self.login_panel.start(login)
            elif self.login_panel.state == "waiting":
                self.login_panel.failed(error)
            else:
                self.notice.setText(error)
                self.notice.show()
            return
        self.saved = connection
        if quota_warning(connection):
            # gravada, mas não funcionando: o aviso fica na tela até o usuário fechar (não some com o diálogo)
            if self.login_panel.state == "waiting":
                self.login_panel.authorized("Autorizado.")
            self.notice.setText("Conexão salva, mas a cota está esgotada: " + quota_warning(connection))
            self.notice.show()
            self.adjustSize()  # o aviso tem várias linhas: sem isso a janela mantém a altura e corta o texto
            self.save_button.setText("Fechar")
            self.save_button.clicked.disconnect()
            self.save_button.clicked.connect(self.accept)
            return
        if self.login_panel.state == "waiting":
            self.login_panel.authorized()
            QTimer.singleShot(DONE_DELAY_MS, self.accept)
        else:
            self.accept()

    def done(self, result: int):
        if result == QDialog.DialogCode.Rejected and self.saved:
            result = QDialog.DialogCode.Accepted  # já gravou (com aviso de cota): fechar não desfaz nem esconde a lista nova
        self.login_panel.clear()
        super().done(result)


class EndpointPanel(QWidget):
    """Endpoint local da conexão selecionada (porta, estado, requisições, Iniciar/Parar, iniciar com o Windows).

    Só aparece para conexão com porta gravada (já usada por VS Code ou Copilot app). O estado é lido ao abrir a tela,
    depois de cada ação e no botão Atualizar; sem polling. Tudo que consulta a rede ou espera o processo roda fora da
    thread da interface (ensure_running espera até 15 s). Copiar endereço leva o endereço base mostrado no painel, sem
    chave (o endpoint acrescenta a credencial); Ver log abre o fim do endpoint.log do perfil."""

    copied = Signal(str)

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
        tools = QHBoxLayout()
        self.copy_address_button = QPushButton("Copiar endereço")
        self.copy_address_button.setToolTip("Copia o endereço do endpoint para colar no campo de endereço de outra ferramenta")
        self.copy_address_button.clicked.connect(self.copy_address)
        tools.addWidget(self.copy_address_button)
        self.copy_config_button = QPushButton("Copiar configuração")
        self.copy_config_button.setToolTip("Copia as variáveis de ambiente do endpoint (endereço certo por protocolo e uma "
                                           "chave sem valor) para o PowerShell ou o cmd")
        config_menu = QMenu(self.copy_config_button)
        for title, shell in (("PowerShell", "powershell"), ("cmd", "cmd")):
            config_menu.addAction(title, lambda shell=shell: self.copy_config(shell))
        self.copy_config_button.setMenu(config_menu)
        tools.addWidget(self.copy_config_button)
        self.log_button = QPushButton("Ver log")
        self.log_button.clicked.connect(self.show_log)
        tools.addWidget(self.log_button)
        tools.addStretch()
        layout.addLayout(tools)
        self.notice = _label("", "notice")
        self.notice.hide()
        layout.addWidget(self.notice)
        self.state_error = ""  # leitura do estado: vale para todas as conexões até a próxima leitura que der certo
        self.action_errors: dict[str, str] = {}  # erro de Iniciar/Parar/autostart, por conexão
        self.hide()

    def render_notice(self):
        """Erros ativos desta conexão: o da leitura do estado e o da última ação nela (o de outra conexão não aparece)."""
        text = "\n".join(filter(None, [self.state_error, self.action_errors.get(self.connection_id or "", "")]))
        news = bool(text) and text != self.notice.text()
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        if news:
            reveal(self.notice)

    def _start(self, job, slot):
        task = _Task(job)
        self.tasks.add(task)
        task.signals.completed.connect(lambda *_args, task=task: self.tasks.discard(task))
        run_task(task, slot)

    def _act(self, connection_id: str, job, busy_text: str = ""):
        """Dispara a ação do usuário sobre a conexão: apaga o erro da ação anterior dela, trava os botões e roda `job`."""
        self.action_errors.pop(connection_id, None)
        self.render_notice()
        self._set_busy(True, busy_text)
        self._start(job, self._acted_for(connection_id))

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
        self.copy_address_button.setEnabled(running and bool(state.get("url")))
        self.copy_config_button.setEnabled(running and bool(state.get("url")))
        self.autostart_box.setChecked(bool(state.get("autostart")))
        self.show()

    def copy_address(self):
        """Copia o endereço base do endpoint da conexão selecionada; parado, não copia nada."""
        state = self.states.get(self.connection_id or "") or {}
        if not state.get("running") or not state.get("url"):
            return
        QApplication.clipboard().setText(state["url"])
        self.copied.emit("Endereço copiado: cole no campo de endereço da outra ferramenta. Não precisa de chave, o "
                         "endpoint acrescenta a credencial.")

    def copy_config(self, shell: str):
        """Copia as variáveis de ambiente do endpoint da conexão selecionada para o `shell` (powershell ou cmd); parado,
        não copia nada. Falha ao montar aparece na faixa do painel."""
        connection_id = self.connection_id or ""
        state = self.states.get(connection_id) or {}
        if not state.get("running") or not state.get("port"):
            return
        self.action_errors.pop(connection_id, None)
        try:
            text = endpoint_registry.connection_config(self.home, connection_id, state["port"], shell)
        except LauncherError as error:
            self.action_errors[connection_id] = str(error)
            self.render_notice()
            return
        self.render_notice()
        QApplication.clipboard().setText(text)
        self.copied.emit(f"Configuração copiada ({'PowerShell' if shell == 'powershell' else 'cmd'}): cole no terminal da "
                         "outra ferramenta. Vai sem chave real, o endpoint acrescenta a credencial.")

    def show_log(self):
        EndpointLogDialog(self.home, self.window()).exec()

    def toggle(self):
        connection_id = self.connection_id
        state = self.states.get(connection_id or "")
        if not state:
            return
        home = self.home
        if state.get("running"):
            self._act(connection_id, lambda: endpoint_registry.stop(home, connection_id), "Parando o endpoint…")
        else:
            self._act(connection_id, lambda: endpoint_registry.ensure_running(home, connection_id), "Iniciando o endpoint…")

    def set_autostart(self, enabled: bool):
        connection_id = self.connection_id
        if not connection_id:
            return
        home = self.home
        self._act(connection_id, lambda: endpoint_registry.autostart(home, connection_id, bool(enabled)))

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


class EndpointLogDialog(GrowingDialog):
    """O fim do endpoint.log do perfil, só para leitura, com Atualizar, Copiar e Fechar. Log ausente ou vazio mostra o
    texto de vazio; log que não abre mostra o erro, nunca um vazio de mentira."""

    def __init__(self, home: Path, parent=None):
        super().__init__(parent, QSize(560, 360))
        self.home = home
        self.text = ""
        self.setWindowTitle("Log do endpoint local")
        layout = self.body_layout
        layout.addWidget(_label("Log do endpoint local", "title"))
        layout.addWidget(_label(f"As últimas {LOG_LINES} linhas do endpoint.log deste perfil: rota, modelo, status e "
                                "duração de cada pedido. Nunca o corpo nem a chave.", "muted"))
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setAccessibleName("Resumo do log do endpoint local")
        self.summary.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        bind_style(self.summary, LOG_VIEW_STYLE)
        self.summary.hide()
        layout.addWidget(self.summary)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setAccessibleName("Log do endpoint local")
        self.view.setMinimumHeight(140)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        bind_style(self.view, LOG_VIEW_STYLE)
        layout.addWidget(self.view, 1)
        self.status = StatusLine()
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.refresh_button = QPushButton("Atualizar")
        self.refresh_button.clicked.connect(self.load)
        row.addWidget(self.refresh_button)
        self.copy_button = QPushButton("Copiar")
        self.copy_button.clicked.connect(self.copy)
        row.addWidget(self.copy_button)
        row.addStretch()
        self.close_button = QPushButton("Fechar")
        self.close_button.setObjectName("primary")
        self.close_button.clicked.connect(self.accept)
        row.addWidget(self.close_button)
        self.footer_layout.addLayout(row)
        self.load()

    def load(self):
        """Lê o log de novo e mostra o fim dele."""
        self.status.setText("")
        try:
            self.text = endpoint_registry.read_log(self.home, LOG_LINES)
            shown = self.text or EMPTY_LOG
        except OSError as error:
            self.text = ""
            shown = f"Não foi possível ler o log do endpoint: {error}"
        self.show_summary(endpoint_registry.summary_text(endpoint_registry.summarize_log(self.text), SUMMARY_MODELS)
                          if self.text else "")
        self.view.setPlainText(shown)
        self.view.moveCursor(self.view.textCursor().MoveOperation.End)
        self.copy_button.setEnabled(bool(self.text))

    def showEvent(self, event):
        """Abre já na altura que o corpo e o rodapé pedem: a medida da área rolável é limitada a umas 24 linhas de texto,
        e um corpo mais alto abriria com barra de rolagem. A área útil da tela (PopupKeeper) ainda limita o tamanho."""
        super().showEvent(event)
        self.fit_summary()
        body = self.body_host.layout().heightForWidth(self.body_scroll.viewport().width())
        self.resize(self.width(), max(self.height(), body + self.footer_host.sizeHint().height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_summary()

    def show_summary(self, text: str):
        """Mostra o resumo numa área só de leitura de altura fixa (as linhas já quebradas, no máximo SUMMARY_ROWS): rótulo
        que quebra linha muda de altura com a largura e o diálogo mede errado."""
        self.summary.setPlainText(text)
        self.summary.setVisible(bool(text))
        self.fit_summary()

    def fit_summary(self):
        """Altura da área do resumo = as linhas visuais do texto na largura de agora, até SUMMARY_ROWS."""
        if not self.summary.isVisibleTo(self):
            return
        self.summary.ensurePolished()
        rows = min(max(int(self.summary.document().size().height()), 1), SUMMARY_ROWS)
        frame = 2 * self.summary.frameWidth() + 2 * int(self.summary.document().documentMargin()) + 12
        height = rows * self.summary.fontMetrics().lineSpacing() + frame
        if self.summary.height() != height or self.summary.minimumHeight() != height:
            self.summary.setFixedHeight(height)

    def copy(self):
        if not self.text:
            return
        QApplication.clipboard().setText(self.text)
        self.status.setText(f"{plural(len(self.text.splitlines()), 'linha copiada', 'linhas copiadas')}.")


class ConnectionSettingPanel(QWidget):
    """Base dos painéis de uma configuração gravada na conexão selecionada: guarda a conexão, esconde-se sem ela, tem a
    faixa de recusa (`notice`) e deixa à subclasse só `fill`, o desenho do valor gravado."""

    def __init__(self, home: Path, parent=None):
        super().__init__(parent)
        self.home = home
        self.connection_id: str | None = None
        self.notice = _label("", "notice")
        self.notice.hide()

    def reset_for_other_connection(self) -> None:
        """Limpa o que era da conexão anterior; a subclasse acrescenta o que mais guarda."""
        self.notice.hide()

    def fill(self, store: Store, connection: dict) -> None:
        """Mostra no painel o valor gravado em `connection`."""
        raise NotImplementedError(f"{type(self).__name__} não define fill()")

    def show_for(self, connection_id: str | None):
        """Mostra o painel com o valor gravado da conexão `connection_id`; sem conexão, esconde o painel."""
        if connection_id != self.connection_id:
            self.reset_for_other_connection()
        self.connection_id = connection_id
        store = Store(self.home)
        try:
            connection = store.find_connection(connection_id) if connection_id else None
        except LauncherError:
            connection = None  # a conexão sumiu entre a lista e a leitura
        if connection is None:
            self.hide()
            return
        self.fill(store, connection)
        self.show()

    def refuse(self, error: Exception) -> None:
        """Mostra a recusa do cadastro na faixa do painel."""
        self.notice.setText(str(error))
        self.notice.show()
        reveal(self.notice)

    def commit(self, write: Callable[[Store, str], None], undo: Callable[[], None]) -> bool:
        """Grava com `write(store, connection_id)` na conexão selecionada (sem conexão, não faz nada). Se o cadastro
        recusar, chama `undo` e mostra a recusa na faixa; se aceitar, esconde a faixa. Devolve se gravou."""
        connection_id = self.connection_id
        if not connection_id:
            return False
        try:
            write(Store(self.home), connection_id)
        except LauncherError as error:
            undo()
            self.refuse(error)
            return False
        self.notice.hide()
        return True


class FallbackPanel(ConnectionSettingPanel):
    """Conexão reserva da conexão selecionada: quando ela responde cota esgotada, o endpoint local refaz o pedido na
    reserva. Seletor com as outras conexões e Salvar; ao trocar de conexão mostra o valor gravado. A recusa do cadastro
    (ciclo, conexão que sumiu) aparece na faixa do painel e nada é gravado."""

    NONE = "Sem reserva"

    def __init__(self, home: Path, parent=None):
        super().__init__(home, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(_label("Conexão reserva", "section"))
        layout.addWidget(_label("Se esta conexão responder cota esgotada, o endpoint local refaz o pedido na reserva.",
                                "muted"))
        row = QHBoxLayout()
        self.combo = ReadableCombo()
        self.combo.setAccessibleName("Conexão reserva")
        row.addWidget(self.combo, 1)
        self.save_button = QPushButton("Salvar")
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.status = StatusLine()
        layout.addWidget(self.status)
        layout.addWidget(self.notice)
        self.hide()

    def reset_for_other_connection(self) -> None:
        self.status.setText("")
        super().reset_for_other_connection()

    def fill(self, store: Store, connection: dict) -> None:
        """O seletor com as outras conexões e a reserva gravada de `connection` marcada."""
        current = store.fallback_of(connection)
        self.combo.clear()
        self.combo.addItem(self.NONE, None)
        for other in store.connections:
            if other["id"] != connection["id"]:
                self.combo.addItem(other["name"], other["id"])
        self.combo.setCurrentIndex(max(self.combo.findData(current["id"]) if current else 0, 0))
        self.combo.fit_to_items()

    def save(self):
        """Grava a reserva escolhida (ou tira a reserva com "Sem reserva")."""
        reserve_id = self.combo.currentData()

        def write(store: Store, connection_id: str) -> None:
            if reserve_id:
                store.set_fallback(connection_id, reserve_id)
            else:
                store.clear_fallback(connection_id)

        if self.commit(write, lambda: self.status.setText("")):
            self.status.setText("Reserva salva." if reserve_id else "Reserva removida.")


class EndpointSessionsPanel(ConnectionSettingPanel):
    """Preferência da conexão selecionada: abrir as sessões de terminal (Claude Code e Codex) pelo endpoint local. Fica
    abaixo do painel do endpoint e aparece para toda conexão, com ou sem porta gravada. Marcar ou desmarcar grava na hora;
    a recusa do cadastro aparece na faixa do painel e a caixa volta ao valor gravado."""

    def __init__(self, home: Path, parent=None):
        super().__init__(home, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.box = QCheckBox("Abrir sessões de terminal pelo endpoint local")
        self.box.setToolTip("Claude Code e Codex desta conexão passam pelo endpoint local: o log, a conexão reserva e o uso "
                            "valem para a sessão, e a chave real não vai para o ambiente dela. Desmarcado, a sessão vai "
                            "direto ao host, como sempre.")
        self.box.clicked.connect(self.save)  # clicked: só a ação do usuário, não o setChecked
        layout.addWidget(self.box)
        layout.addWidget(self.notice)
        self.hide()

    def fill(self, store: Store, connection: dict) -> None:
        """A caixa com o valor gravado de `connection`."""
        self.box.setChecked(Store.endpoint_sessions_of(connection))

    def save(self, enabled: bool):
        """Grava a preferência da conexão selecionada."""
        self.commit(lambda store, connection_id: store.set_endpoint_sessions(connection_id, bool(enabled)),
                    lambda: self.box.setChecked(not enabled))


class ConnectionsDialog(GrowingDialog):
    """Conexões cadastradas, com o painel da reserva e o do endpoint local da selecionada. A janela cresce quando um
    painel ou uma faixa de erro aparece: os botões da lista nunca ficam abaixo do tamanho do texto."""

    def __init__(self, home: Path, parent=None):
        super().__init__(parent, QSize(620, 520))
        self.home = home
        self.cleaning = False
        self.cleanup_task = None
        self.setWindowTitle("Conexões")
        layout = self.body_layout
        layout.addWidget(_label("Conexões com provedores", "title"))
        layout.addWidget(_label("Cada conexão é um host de modelos: LiteLLM ou host próprio, 9router, OmniRoute, "
                                "OpenRouter, Anthropic, OpenAI ou qualquer host compatível. As chaves ficam cifradas "
                                "para o seu usuário do Windows.", "muted"))
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setAccessibleName("Conexões cadastradas")
        self.list.currentItemChanged.connect(self.selection_changed)
        self.list.itemDoubleClicked.connect(lambda _item: self.edit())
        body.addWidget(self.list, 1)
        buttons = QVBoxLayout()
        self.buttons = {}
        for key, text, callback in (("add", "Adicionar…", self.add), ("edit", "Editar…", self.edit),
                                    ("duplicate", "Duplicar…", self.duplicate),
                                    ("test", "Testar", self.test), ("test_all", "Testar todas", self.test_all),
                                    ("default", "Tornar padrão", self.make_default),
                                    ("copy", "Copiar lista", self.copy_list),
                                    ("share", "Compartilhar sem chave…", self.share), ("remove", "Remover…", self.remove)):
            widget = QPushButton(text)
            widget.clicked.connect(callback)
            buttons.addWidget(widget)
            self.buttons[key] = widget
        self.buttons["add"].setObjectName("primary")
        buttons.addStretch()
        body.addLayout(buttons)
        layout.addLayout(body, 1)
        self.fallback = FallbackPanel(home, self)
        layout.addWidget(self.fallback)
        self.endpoint = EndpointPanel(home, self)
        self.endpoint.copied.connect(lambda text: self.set_status("copia", text))
        layout.addWidget(self.endpoint)
        self.sessions = EndpointSessionsPanel(home, self)
        layout.addWidget(self.sessions)
        self.login_panel = LoginPanel()
        self.login_panel.poll.connect(lambda: self.test(polling=True))
        layout.addWidget(self.login_panel)
        self.testing = False
        self.testing_all = False
        self.batch: list[str] = []  # conexões que ainda faltam testar na rodada de "Testar todas"
        self.batch_total = 0
        self.results: dict[str, tuple[str, str]] = {}  # conexão -> (estado, erro) do último "Testar todas"
        self.errors: dict[str, str] = {}  # origem ("limpeza", "teste") -> erro; some só quando a origem se resolve
        self.error_notice = _label("", "notice")  # não some com a lista que recarrega nem com a linha de status
        self.error_notice.setWordWrap(True)
        self.error_notice.hide()
        layout.addWidget(self.error_notice)
        self.statuses: dict[str, str] = {}  # origem ("lista", "teste", "limpeza") -> linha; convivem, a mesma substitui a sua
        self.status = _label("", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.close_button = QPushButton("Fechar")
        self.close_button.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.close_button)
        self.footer_layout.addLayout(row)
        self.populate()
        self.endpoint.refresh()

    def populate(self, preferred: str | None = None):
        store = Store(self.home)
        modules, _errors = store.connectors()
        self.list.clear()
        self.results = {key: value for key, value in self.results.items()
                        if any(connection["id"] == key for connection in store.connections)}
        for connection in store.connections:
            module = modules.get(connection["connector"])
            default = " • padrão" if connection["id"] == store.data["default_connection"] else ""
            details = [module.LABEL if module else connection["connector"], public_address(str(connection["options"].get("base_url", ""))),
                       self.state_text(connection)]
            item = QListWidgetItem(f"{connection['name']}{default}\n" + " • ".join(part for part in details if part))
            item.setData(Qt.ItemDataRole.UserRole, connection["id"])
            item.setToolTip(self.results.get(connection["id"], ("", ""))[1])
            self.list.addItem(item)
            if connection["id"] == preferred:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.set_status("lista", "" if self.list.count() else "Nenhuma conexão ainda. Clique em Adicionar.")
        self.set_status("copia", "")
        self.set_status("cota", "\n".join(f"{connection['name']}: cota esgotada, a conexão volta a funcionar quando a cota "
                                          "voltar ou com outra conta." for connection in store.connections
                                          if quota_warning(connection)))
        self.update_buttons()

    def state_text(self, connection: dict) -> str:
        """Estado a mostrar: o do último "Testar todas" ou, sem ele, a cota esgotada gravada na conexão."""
        state, detail = self.results.get(connection["id"], ("", ""))
        if state == "falhou":
            return f"falhou: {' '.join(detail.split())[:120]}"
        return state or ("cota esgotada" if quota_warning(connection) else "")

    def selected_id(self) -> str | None:
        """O id da conexão selecionada na lista, ou None."""
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def current(self) -> dict | None:
        item = self.list.currentItem()
        return Store(self.home).find_connection(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def selection_changed(self, *_args):
        """A confirmação de cópia era da conexão anterior: não fica na faixa da nova."""
        self.set_status("copia", "")
        self.update_buttons()

    def update_buttons(self, *_args):
        for key in ("edit", "duplicate", "default", "share", "remove"):
            self.buttons[key].setEnabled(self.list.currentItem() is not None)
        self.buttons["test"].setEnabled(self.list.currentItem() is not None and not self.testing_all)
        self.buttons["test_all"].setEnabled(self.list.count() > 0 and not self.testing_all)
        self.buttons["copy"].setEnabled(self.list.count() > 0)
        selected = self.selected_id()
        self.endpoint.show_for(selected)
        self.sessions.show_for(selected)
        self.fallback.show_for(selected)

    def add(self):
        form = ConnectionForm(self.home, parent=self)
        if form.exec() and form.saved:
            self.populate(form.saved["id"])

    def edit(self):
        connection = self.current()
        if connection:
            form = ConnectionForm(self.home, connection, self)
            if form.exec() and form.saved:
                self.results.pop(form.saved["id"], None)
                self.populate(form.saved["id"])

    def duplicate(self):
        connection = self.current()
        if connection:
            form = ConnectionForm(self.home, parent=self, source=connection)
            if form.exec() and form.saved:
                self.populate(form.saved["id"])

    def test(self, polling: bool = False):
        """Testar a conexão selecionada. Com login por código pendente (`polling`), é a conferência automática."""
        connection = self.current()
        if not connection or self.testing:
            return
        self.testing = True
        if not polling:
            self.login_panel.clear()
            self.set_status("teste", f"Testando {connection['name']}…")
            if self.results.pop(connection["id"], None) is not None:
                self.populate(connection["id"])
        self.task = self.verification(connection)
        task = self.task
        run_task(task, lambda _result, error: self.show_test_result(connection["name"], error, task))

    def verification(self, connection: dict) -> _Task:
        """Tarefa de fundo que confere a conexão pelo Gateway: a mesma do teste individual e a de cada passo do Testar
        todas (Gateway.verify lista os modelos, não pede resposta de modelo)."""
        home, reference = self.home, connection["id"]
        return _Task(lambda: Gateway(Store(home), Store(home).find_connection(reference)).verify())

    def test_all(self):
        """Testa as conexões uma depois da outra, cada uma na tarefa de fundo do teste individual. Uma falha não
        interrompe as outras e o login por código pendente vira "aguarda login" sem abrir o painel."""
        if self.testing or self.cleaning or not self.list.count():
            return
        self.login_panel.clear()
        self.errors.pop("teste", None)
        self.render_errors()
        self.batch = [connection["id"] for connection in Store(self.home).connections]
        self.batch_total = len(self.batch)
        self.results.clear()
        self.testing = self.testing_all = True
        self.populate(self.selected_id())
        self.test_next()

    def test_next(self):
        """Dispara o teste da próxima conexão da rodada (pulando as removidas) ou encerra a rodada."""
        store = Store(self.home)
        connection = None
        while self.batch and connection is None:
            try:
                connection = store.find_connection(self.batch.pop(0))
            except LauncherError:
                pass  # removida durante a rodada: não há o que testar
        if connection is None:
            self.finish_batch()
            return
        self.set_status("teste", f"Testando {self.batch_total - len(self.batch)} de {self.batch_total}: "
                                 f"{connection['name']}…")
        self.task = self.verification(connection)
        task = self.task
        run_task(task, lambda _result, error: self.batch_result(connection, error, task))

    def batch_result(self, connection: dict, error: str, task):
        """Guarda o resultado de uma conexão da rodada, mostra na lista e segue para a próxima."""
        self.results[connection["id"]] = verdict(task, error)
        self.populate(self.selected_id())
        self.test_next()

    def finish_batch(self):
        """Fim da rodada: libera os botões e resume quantas conexões ficaram em cada estado."""
        self.testing = self.testing_all = False
        counts = Counter(state for state, _detail in self.results.values())
        summary = ", ".join(f"{counts[state]} {state}" for state in BATCH_STATES if counts[state])
        hint = " Para entrar, selecione a conexão e clique em Testar." if counts["aguarda login"] else ""
        self.set_status("teste", f"{plural(len(self.results), 'conexão testada', 'conexões testadas')}: {summary}.{hint}"
                        if self.results else "")
        self.update_buttons()

    def copy_list(self):
        """Copia nome, conector, endereço, padrão e estado de cada conexão, separados por tabulação. Chave e segredo
        nunca entram: as chaves ficam no cofre e o endereço sai sem usuário, senha ou consulta."""
        store = Store(self.home)
        modules, _errors = store.connectors()
        rows = []
        for connection in store.connections:
            module = modules.get(connection["connector"])
            rows.append([connection["name"], module.LABEL if module else connection["connector"],
                         public_address(str(connection["options"].get("base_url", ""))) or "—",
                         "sim" if connection["id"] == store.data["default_connection"] else "não",
                         self.state_text(connection) or "—"])
        QApplication.clipboard().setText(connections_text(rows))
        self.set_status("copia", f"{plural(len(rows), 'conexão copiada', 'conexões copiadas')}: cole numa planilha, "
                                 "cada valor vai para a sua coluna.")

    def share(self):
        """Grava a conexão selecionada, sem a chave, no arquivo que o usuário escolhe. Nada é gravado antes disso."""
        from agent_launcher.domain import profile_package
        connection = self.current()
        if not connection:
            return
        suggested = str(Path.home() / (re.sub(r'[\\/:*?"<>|]', "-", connection["name"]) + profile_package.EXTENSION))
        path, _filter = QFileDialog.getSaveFileName(self, "Compartilhar conexão sem chave", suggested,
                                                    "Conexão do Agent Launcher (*.alprofile)")
        if not path:
            return
        try:
            result = profile_package.export_connection(Store(self.home), connection["id"], Path(path))
        except (LauncherError, OSError) as error:
            self.errors["compartilhar"] = f"{connection['name']}: não foi possível compartilhar: {error}"
            self.set_status("compartilhar", "")
        else:
            self.errors.pop("compartilhar", None)
            self.set_status("compartilhar", f"{connection['name']}: {Path(result['path']).name} gravado. O arquivo não "
                                            "leva a chave: quem receber informa a sua em Conexões.")
        self.render_errors()

    def show_test_result(self, name: str, error: str, task=None):
        self.testing = False
        login = login_of(task) if error else None
        if login is not None:
            self.errors.pop("teste", None)
            self.set_status("teste", f"{name}: aguardando a autorização do login…")
            self.login_panel.start(login)
        elif error and self.login_panel.state == "waiting":
            self.login_panel.failed(error)  # o erro fica no painel, junto do código que ainda vale
            self.errors.pop("teste", None)
            self.set_status("teste", "")
        elif error and cause_of(task, QuotaExhausted) is not None:
            self.errors.pop("teste", None)
            item = self.list.currentItem()
            self.populate(item.data(Qt.ItemDataRole.UserRole) if item else None)
            self.set_status("teste", f"Aviso: {name} está sem cota. {error}")
        elif error:
            self.errors["teste"] = f"{name}: {error}"
            self.set_status("teste", "")
        else:
            if self.login_panel.state == "waiting":
                self.login_panel.authorized("Autorizado.")
            self.errors.pop("teste", None)
            item = self.list.currentItem()
            self.populate(item.data(Qt.ItemDataRole.UserRole) if item else None)  # o aviso de cota gravado já foi limpo
            self.set_status("teste", f"{name}: conexão respondeu.")
        self.render_errors()

    def done(self, result: int):
        """Não fecha no meio da limpeza nos destinos (Fechar, Esc ou o X): o fim dela atualiza este diálogo."""
        if self.cleaning:
            return
        self.batch = []
        self.login_panel.clear()
        super().done(result)

    def set_status(self, origin: str, text: str):
        """Linha de status: cada origem tem a sua (o fim de uma não apaga a das outras)."""
        if text:
            self.statuses[origin] = text
        else:
            self.statuses.pop(origin, None)
        self.status.setText("\n".join(self.statuses.values()))

    def render_errors(self):
        text = "\n".join(self.errors.values())
        news = bool(text) and text != self.error_notice.text()
        self.error_notice.setText(text)
        self.error_notice.setVisible(bool(text))
        if news:
            reveal(self.error_notice)

    def make_default(self):
        connection = self.current()
        if connection:
            Store(self.home).set_default_connection(connection["id"])
            self.populate(connection["id"])

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
        run_task(task, finished)

    def show_cleanup_result(self, messages):
        """O que foi feito vai para a linha de status; o que falhou, para a faixa própria, até a próxima limpeza."""
        done, failed = split_cleanup(messages)
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
