"""Tarefas em segundo plano (QRunnable) da janela e entrega do resultado delas."""
import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


def deliver(signals, name: str, *args) -> None:
    """Emite `signals.<name>(*args)`. Se o QObject de sinais já foi destruído (o processo fechou com a tarefa em curso),
    não há mais quem receba e o resultado é descartado; qualquer outro erro sobe."""
    if not shiboken6.isValid(signals):
        return
    try:
        getattr(signals, name).emit(*args)
    except RuntimeError:
        if shiboken6.isValid(signals):
            raise


class TaskSignals(QObject):
    completed = Signal(object, str)


class ProgressSignals(TaskSignals):
    progress = Signal(object, str)


class BackgroundTask(QRunnable):
    """Trabalho fora da thread da interface. A subclasse escreve `outcome()` (o resultado e o texto que o acompanha) e,
    se a falha precisar de outra forma, `failure(error)`; o resultado chega por `signals.completed(valor, texto)` e uma
    exceção nunca derruba a thread: volta para a tela como `failure` a traduz (por padrão, o próprio erro e o `tag`)."""

    signals_class = TaskSignals
    tag = ""

    def __init__(self):
        super().__init__()
        self.signals = self.signals_class()

    def outcome(self) -> tuple[object, str]:
        """O trabalho em si: devolve (resultado, texto) para a janela."""
        raise NotImplementedError(f"{type(self).__name__} não define outcome()")

    def failure(self, error: Exception) -> tuple[object, str]:
        """O (resultado, texto) que vai à janela quando `outcome()` levanta."""
        return error, self.tag

    @Slot()
    def run(self):
        try:
            result = self.outcome()
        except Exception as error:
            result = self.failure(error)
        deliver(self.signals, "completed", *result)


def run_task(task: BackgroundTask, slot) -> None:
    """Liga o resultado da tarefa ao `slot` e a inicia no pool global: o único ponto por onde a interface começa
    trabalho em segundo plano. A ligação vem antes do início para o resultado nunca sair sem destino."""
    task.signals.completed.connect(slot)
    QThreadPool.globalInstance().start(task)
