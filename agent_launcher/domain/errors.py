class LauncherError(Exception):
    """Erro com mensagem acionável para a interface, o modo texto e a janela da sessão."""


class LauncherBusy(LauncherError):
    """Pedido que não é defeito: o recurso está ocupado agora (atualização em andamento) e vale tentar depois."""
