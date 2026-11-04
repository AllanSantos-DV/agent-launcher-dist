"""Entrega do resultado das tarefas em segundo plano (QRunnable) para a janela."""
import shiboken6


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
