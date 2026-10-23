"""Limpeza nos destinos ao remover uma conexão ou um perfil: o provider da conexão no
Copilot app e no perfil "Agent Launcher" do VS Code, e o endpoint local (para, sai do autostart, libera a porta).

Cada passo é independente: a falha de um vira aviso e não impede os outros (nem a remoção da conexão)."""
from __future__ import annotations

from types import SimpleNamespace

from agent_launcher.domain.errors import LauncherError


def provider_name(store, connection: dict) -> str:
    """Nome do provider que o VS Code e o Copilot app receberam para esta conexão (o mesmo dos dois adaptadores)."""
    from agent_launcher.harnesses.copilot_app import CopilotApp
    return CopilotApp.provider_name(SimpleNamespace(name=connection["name"], store=store))


def cleanup_destinations(store, connection: dict, confirm=lambda question: True, *, copilot_remove=None,
                         vscode_remove=None, stop=None, autostart=None, forget=None) -> list[str]:
    """Remove a conexão dos destinos. Devolve as mensagens (o que foi feito e os avisos). Os parâmetros com
    palavra-chave existem para os testes trocarem os passos que tocam no Windows (registro, processos)."""
    from agent_launcher.endpoint import registry
    from agent_launcher.harnesses import copilot_app, vscode
    copilot_remove = copilot_remove or copilot_app.remove_provider
    vscode_remove = vscode_remove or vscode.remove_provider
    if autostart is None:
        # Só mexe no registro (HKCU\\...\\Run) quando a conexão está mesmo no autostart.
        autostart = lambda home, identifier, enabled: (  # noqa: E731
            registry.autostart(home, identifier, enabled) if registry.autostart_enabled(identifier, home) else None)
    stop, forget = stop or registry.stop, forget or registry.forget
    name = provider_name(store, connection)
    messages = []
    try:
        message = copilot_remove(name, confirm)
        if message:
            messages.append(message)
    except (LauncherError, OSError, Exception) as error:  # sqlite3.Error e afins: vira aviso
        messages.append(f"Copilot app: {error}")
    try:
        if vscode_remove(name):
            messages.append(f"Provider \"{name}\" removido do VS Code (perfil Agent Launcher).")
    except (OSError, ValueError) as error:
        messages.append(f"VS Code: {error}")
    try:
        if stop(store.home, connection["id"]):
            messages.append("Endpoint local parado.")
    except (LauncherError, OSError) as error:
        messages.append(f"Endpoint local: {error}")
    try:
        autostart(store.home, connection["id"], False)
    except (OSError, ImportError) as error:
        messages.append(f"Iniciar com o Windows: {error}")
    try:
        port = forget(store.home, connection["id"])
        if port:
            messages.append(f"Porta {port} do endpoint local liberada.")
    except OSError as error:
        messages.append(f"Porta do endpoint: {error}")
    return messages


def release_endpoint(home, connection_id: str) -> list[str]:
    """Sempre que uma conexão (ou o perfil dela) deixa de existir: para o endpoint, tira do autostart e libera a porta.
    Independe da limpeza dos apps (que o usuário pode recusar): um autostart apontando para uma pasta apagada não serve
    para nada e sobe um processo com erro a cada logon. Cada passo falha sozinho, como aviso."""
    from agent_launcher.endpoint import registry
    messages = []
    for label, step in (("Endpoint local", lambda: registry.stop(home, connection_id)),
                        ("Iniciar com o Windows", lambda: registry.autostart_enabled(connection_id, home)
                         and registry.autostart(home, connection_id, False)),
                        ("Porta do endpoint", lambda: registry.forget(home, connection_id))):
        try:
            step()
        except (LauncherError, OSError, ImportError) as error:
            messages.append(f"{label}: {error}")
    return messages


def release_profile_endpoints(home) -> list[str]:
    from agent_launcher.domain.store import Store
    messages = []
    for connection in list(Store(home).connections):
        messages.extend(release_endpoint(home, connection["id"]))
    return messages


def cleanup_profile(home, confirm=lambda question: True, **steps) -> list[str]:
    """cleanup_destinations para cada conexão do perfil cuja pasta de dados é `home`."""
    from agent_launcher.domain.store import Store
    store = Store(home)
    messages = []
    for connection in list(store.connections):
        messages.extend(cleanup_destinations(store, connection, confirm, **steps))
    return messages
