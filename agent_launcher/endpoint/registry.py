"""Porta fixa por conexão (o destino guarda a URL), estado do processo e subir/parar o endpoint local.

As portas ficam num arquivo só, na raiz (vale para todos os perfis), com chave "<perfil>/<conexão>": dois perfis nunca
gravam a mesma porta. A entrada antiga (só o id da conexão, do tempo de um arquivo por pasta) é adotada com a mesma
porta, porque o VS Code e o Copilot app guardam a URL."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from agent_launcher import ROOT
from agent_launcher.domain import profiles
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, file_lock, read_json_state
from agent_launcher.endpoint import supervisor
from agent_launcher.platform import native

# As portas do produto começam em 47910 (as anteriores ficam livres para testes locais).
PORT_RANGE = range(47910, 48000)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_SHELLS = ("powershell", "cmd")
PLACEHOLDER_KEY = "agent-launcher-local"
_REQUEST_LINE = re.compile(r"^(?P<stamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \S+ POST \S+ \S+ model=(?P<model>'.*'|\".*\") "
                           r"status=(?P<status>\d+) (?P<seconds>\d+(?:\.\d+)?)s$")
_FAILOVER_DONE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d failover .+ -> .+ motivo=")
_FAILOVER_FAILED = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d failover para .+ não feito")


def state_file(home: Path | None = None) -> Path:
    """Arquivo único na raiz; `home` fica só por compatibilidade de assinatura."""
    return profiles.root() / "endpoints.json"


def _read_file(path: Path) -> dict:
    data = read_json_state(path, accept=lambda data: isinstance(data, dict) and isinstance(data.get("ports"), dict))
    return data or {"ports": {}}


def _read(home: Path | None = None) -> dict:
    return _read_file(state_file())


def _profile_id(home: Path) -> str:
    return profiles.profile_of_home(Path(home))["id"]


def _key(home: Path, connection_id: str) -> str:
    return f"{_profile_id(home)}/{connection_id}"


def _stored(data: dict, home: Path, connection_id: str) -> tuple[int | None, bool]:
    """Porta já gravada para perfil+conexão: (porta, veio de entrada antiga a migrar)."""
    port = data["ports"].get(_key(home, connection_id))
    if isinstance(port, int) and port in PORT_RANGE:
        return port, False
    default = _profile_id(home) == profiles.DEFAULT
    taken = {value for name, value in data["ports"].items()
             if isinstance(value, int) and not (default and name == connection_id)}
    legacy = []
    if default:
        legacy.append(data["ports"].get(connection_id))  # arquivo antigo do padrão era o próprio arquivo da raiz
    old_file = Path(home) / "endpoints.json"
    if old_file.resolve() != state_file().resolve():
        legacy.append(_read_file(old_file)["ports"].get(connection_id))
    for port in legacy:
        if isinstance(port, int) and port in PORT_RANGE and port not in taken:
            return port, True
    return None, False


def _free(port: int) -> bool:
    return supervisor.port_free(port)


def port_for(home: Path, connection_id: str, reserve: bool = True) -> int:
    """Porta da conexão: a já gravada (estável: o VS Code e o Copilot app guardam a URL); senão a preferida pelo hash
    do id, ou a próxima livre da faixa. Ler-alterar-gravar sob trava: dois processos nunca reservam a mesma porta."""
    with file_lock(state_file()):
        return _port_for(home, connection_id, reserve)


def _port_for(home: Path, connection_id: str, reserve: bool) -> int:
    data = _read()
    key = _key(home, connection_id)
    port, legacy = _stored(data, home, connection_id)
    if port is not None:
        if legacy and reserve:
            if _profile_id(home) == profiles.DEFAULT:
                data["ports"].pop(connection_id, None)
            data["ports"][key] = port
            atomic_json(state_file(), data)
        return port
    taken = {value for value in data["ports"].values() if isinstance(value, int)}
    start = int(hashlib.sha256(connection_id.encode()).hexdigest()[:8], 16) % len(PORT_RANGE)
    for offset in range(len(PORT_RANGE)):
        candidate = PORT_RANGE[(start + offset) % len(PORT_RANGE)]
        if candidate not in taken and _free(candidate):
            if reserve:
                data["ports"][key] = candidate
                atomic_json(state_file(), data)
            return candidate
    raise LauncherError(f"Nenhuma porta livre entre {PORT_RANGE.start} e {PORT_RANGE.stop - 1} para o endpoint local.")


def forget(home: Path, connection_id: str) -> int | None:
    """Libera a porta da conexão no endpoints.json (e a entrada antiga, só com o id, no padrão). Devolve a porta."""
    with file_lock(state_file()):
        data = _read()
        port = data["ports"].pop(_key(home, connection_id), None)
        if _profile_id(home) == profiles.DEFAULT:
            legacy = data["ports"].pop(connection_id, None)
            port = port if port is not None else legacy
        if port is not None:
            atomic_json(state_file(), data)
    return port if isinstance(port, int) else None


def base_url(home: Path, connection_id: str) -> str:
    return f"http://127.0.0.1:{port_for(home, connection_id)}"


def health(port: int, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            data = json.loads(response.read(65536))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("agent_launcher_endpoint") else None


def server_command(home: Path, connection_id: str, action: str = "serve", wait_port: int | None = None) -> list[str]:
    """Linha de comando do endpoint (`action` "serve") ou do supervisor que o mantém de pé ("supervise"): o nativo
    (nativo-bin/endpoint.exe) quando existe, com o Python só como resolvedor de uma execução; senão o próprio endpoint.py.
    `wait_port` (só no supervisor): ele espera essa porta ficar livre antes de abrir o `serve`."""
    # Só biblioteca padrão: roda no Python base, sem o redirecionador da venv (um processo a menos).
    python = Path(sys.executable)
    base = Path(getattr(sys, "_base_executable", "") or "")
    if sys.prefix != sys.base_prefix and base.is_file():
        python = base
    pythonw = python.with_name("pythonw.exe")
    script = [str(pythonw if pythonw.is_file() else python), str(ROOT / "endpoint.py")]
    arguments = [action, "--home", str(Path(home).resolve()), "--connection", connection_id]
    if wait_port is not None:
        arguments += ["--port", str(wait_port), "--wait-port"]
    native_exe = native.executable("endpoint")
    if native_exe:
        return [str(native_exe), *arguments, "--resolver", *script]
    return [*script, *arguments]


def _command_executable(command: str) -> str:
    """Primeiro token de uma linha de comando do registro ("C:\\Python\\pythonw.exe" arg ... ou sem aspas)."""
    command = command.strip()
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    return command.split(" ", 1)[0]


def _autostart_value(connection_id: str, home: Path | None) -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, _run_value(connection_id, home))[0]
    except (OSError, ImportError):
        return None


def autostart_stale(connection_id: str, home: Path | None = None) -> str | None:
    """Motivo se o início no logon está ligado com um executável que não existe mais (Python movido ou removido)."""
    value = _autostart_value(connection_id, home)
    if value and not Path(os.path.expandvars(_command_executable(value))).is_file():
        return f"o início no logon aponta para {_command_executable(value)}, que não existe mais"
    return None


def refresh_autostart(home: Path, connection_id: str) -> bool:
    """Com o início no logon ligado e o comando gravado diferente do de agora (outro Python base, pasta movida),
    regrava o valor em HKCU\\...\\Run (sem admin). Desligado fica desligado. True se regravou."""
    current = _autostart_value(connection_id, home)
    wanted = subprocess.list2cmdline(server_command(home, connection_id, "supervise"))
    if current is None or current == wanted:
        return False
    autostart(home, connection_id, True)
    return True


def _launch_supervisor(home: Path, connection_id: str, wait_port: int | None = None) -> None:
    """Lança o supervisor da conexão (que, com `wait_port`, espera essa porta liberar antes do `serve`): processo
    oculto, desligado deste."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    # cwd na pasta de dados: o processo não segura a pasta da instalação (desinstalar e atualizar precisam apagá-la).
    subprocess.Popen(server_command(home, connection_id, "supervise", wait_port), cwd=Path(home), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def _adopt(home: Path, connection_id: str, port: int) -> None:
    """Endpoint que responde sem supervisor (aberto antes dele existir): o endpoint segue atendendo, sem queda de
    conexão, e o supervisor lançado agora espera a porta liberar, sem tentar o `serve` enquanto ela está ocupada, e o
    assume quando o endpoint avulso sair. Fica uma linha no endpoint.log."""
    supervisor.note(Path(home) / "endpoint.log", "endpoint já aberto sem supervisor; supervisor lançado para assumir quando ele sair")
    _launch_supervisor(home, connection_id, port)


def ensure_running(home: Path, connection_id: str, timeout: float = 15) -> int:
    """Sobe o supervisor do endpoint da conexão se o endpoint não responde; devolve a porta. Processo oculto, desligado
    deste. Se o endpoint não responder no prazo, o supervisor que subiu é parado: nada fica insistindo às escondidas."""
    try:
        refresh_autostart(home, connection_id)
    except OSError as error:  # o endpoint sobe mesmo assim; o registro fica como estava
        print(f"Aviso: o início no logon do endpoint não foi regravado ({error}).", file=sys.stderr)
    port = port_for(home, connection_id)
    current = health(port)
    if _same(current, home, connection_id):
        if not supervisor.alive(home, connection_id):
            _adopt(home, connection_id, port)
        return port
    if current:
        raise LauncherError(f"A porta {port} responde por outra conexão ({current.get('connection')}, perfil "
                            f"{current.get('profile') or profiles.DEFAULT}).")
    if not _free(port):
        raise LauncherError(f"A porta {port} do endpoint local está ocupada por outro programa. Libere a porta ou "
                            f"apague a entrada {_key(home, connection_id)} de {state_file()} para escolher outra (e abra o destino de novo pelo launcher).")
    _launch_supervisor(home, connection_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health(port):
            return port
        time.sleep(0.25)
    supervisor.request_stop(home, connection_id)
    raise LauncherError(f"O endpoint local não respondeu em {timeout:.0f} s. Veja {Path(home) / 'endpoint.log'}.")


def _same(current: dict | None, home: Path, connection_id: str) -> bool:
    """O endpoint que responde é desta conexão neste perfil (endpoint antigo, sem 'profile', é do padrão)."""
    return bool(current) and current.get("connection_id") == connection_id \
        and (current.get("profile") or profiles.DEFAULT) == _profile_id(home)


def _wait_released(port: int, timeout: float) -> bool:
    """Espera a porta ficar livre. Perguntar por /health a uma porta já fechada demora ~2 s no Windows (a recusa em
    127.0.0.1 só vem depois das retransmissões do SYN), e o endpoint nativo fecha a porta na hora; o bind é imediato."""
    deadline = time.monotonic() + timeout
    while not _free(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop(home: Path, connection_id: str, timeout: float = 5) -> bool:
    """Para o endpoint da conexão e o supervisor dele (que, parado só o endpoint, o religaria). True se o endpoint
    respondia e agora não responde."""
    port = port_for(home, connection_id, reserve=False)
    supervised = supervisor.request_stop(home, connection_id)
    if _free(port) or not _same(health(port), home, connection_id):
        stopped = False
    else:
        _request_shutdown(port, timeout)
        stopped = _wait_released(port, timeout)
    if supervised:
        supervisor.wait_gone(home, connection_id, timeout)
    return stopped


def _supervisors_of_ports(data: dict) -> list[tuple[Path, str]]:
    """(pasta de dados, conexão) de cada entrada do endpoints.json cujo perfil ainda existe."""
    found = []
    for name in data["ports"]:
        profile_id, _separator, connection_id = name.rpartition("/")
        try:
            found.append((profiles.home_of(profile_id or profiles.DEFAULT), connection_id))
        except LauncherError:
            continue
    return found


def _request_shutdown(port: int, timeout: float) -> None:
    """Pede o desligamento ao endpoint da porta; endpoint que não atende já está desligado."""
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", data=b"", method="POST"), timeout=timeout).close()
    except OSError:
        pass


def stop_all(timeout: float = 5) -> int:
    """Para todo endpoint deste usuário que responde (portas do endpoints.json, todos os perfis) e os supervisores deles.
    Devolve quantos endpoints pararam."""
    stopped = 0
    data = _read()
    supervised = [item for item in _supervisors_of_ports(data) if supervisor.request_stop(*item)]
    for port in sorted({value for value in data["ports"].values() if isinstance(value, int)}):
        if _free(port) or not health(port):
            continue
        _request_shutdown(port, timeout)
        stopped += _wait_released(port, timeout)
    for home, connection_id in supervised:
        supervisor.wait_gone(home, connection_id, timeout)
    return stopped


def remove_autostart_entries(install_root: Path) -> list[str]:
    """Tira de HKCU\\...\\Run os valores AgentLauncherEndpoint-* (todos os perfis) cujo comando roda a partir de
    `install_root`: desinstalar não pode deixar o logon tentando abrir um endpoint que não existe mais."""
    import winreg
    removed = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        names = []
        index = 0
        while True:
            try:
                name, value, _kind = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if name.startswith("AgentLauncherEndpoint-") and isinstance(value, str) \
                    and str(install_root).lower().rstrip("\\/") + "\\" in value.lower():  # com a barra: AgentLauncher-dev não casa
                names.append(name)
        for name in names:
            winreg.DeleteValue(key, name)
            removed.append(name)
    return removed


def _run_value(connection_id: str, home: Path | None = None) -> str:
    """Nome do valor em HKCU\\...\\Run, único por perfil+conexão (o padrão mantém o nome antigo)."""
    profile_id = _profile_id(home) if home is not None else profiles.DEFAULT
    if profile_id == profiles.DEFAULT:
        return f"AgentLauncherEndpoint-{connection_id[:8]}"
    return f"AgentLauncherEndpoint-{profile_id}-{connection_id[:8]}"


def autostart(home: Path, connection_id: str, enabled: bool) -> None:
    """Sobe o endpoint no logon do usuário (HKCU\\...\\Run, sem admin), para o VS Code e o Copilot app acharem a
    conexão mesmo sem o launcher aberto."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _run_value(connection_id, home), 0, winreg.REG_SZ,
                              subprocess.list2cmdline(server_command(home, connection_id, "supervise")))
        else:
            try:
                winreg.DeleteValue(key, _run_value(connection_id, home))
            except FileNotFoundError:
                pass


def autostart_enabled(connection_id: str, home: Path | None = None) -> bool:
    """Se a conexão tem entrada de início no logon (Run) do usuário."""
    return _autostart_value(connection_id, home) is not None


def status(home: Path, connections: list[dict]) -> list[dict]:
    data = _read()
    result = []
    for connection in connections:
        port, _legacy = _stored(data, home, connection["id"])
        current = health(port) if isinstance(port, int) else None
        result.append({"connection": connection["name"], "port": port, "running": bool(current),
                       "url": f"http://127.0.0.1:{port}/v1" if port else None,
                       "requests": (current or {}).get("requests"), "autostart": autostart_enabled(connection["id"], home),
                       "autostart_stale": autostart_stale(connection["id"], home)})
    return result


def read_log(home: Path, lines: int) -> str:
    """As últimas `lines` linhas do endpoint.log do perfil; "" quando o log não existe. O log traz rota, modelo, status
    e duração, nunca corpo nem chave. Um log que existe e não abre levanta OSError."""
    try:
        text = (home / "endpoint.log").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def client_config(port: int, protocols, shell: str = "powershell") -> str:
    """Variáveis de ambiente para outra ferramenta usar o endpoint local da porta `port`, no `shell` (powershell ou cmd):
    Messages (`ANTHROPIC_*`) com o endereço sem /v1, que o cliente completa com /v1/messages, e Responses e Chat
    Completions (`OPENAI_*`) com o endereço terminado em /v1. A chave é um texto fixo sem valor: o endpoint descarta a
    credencial do cliente e acrescenta a da conexão."""
    if shell not in CONFIG_SHELLS:
        raise LauncherError(f"Shell {shell!r} desconhecido; use {' ou '.join(CONFIG_SHELLS)}.")
    from agent_launcher.connectors import PROTOCOLS
    served = set(protocols)
    unknown = served - set(PROTOCOLS)
    if unknown:
        raise LauncherError(f"Protocolo desconhecido: {', '.join(sorted(unknown))}.")
    if not served:
        raise LauncherError("A conexão não serve nenhum protocolo que o endpoint local repasse.")
    root = f"http://127.0.0.1:{port}"
    blocks = []
    if "messages" in served:
        blocks.append(("Anthropic Messages: endereço sem /v1 (o cliente acrescenta /v1/messages)",
                       {"ANTHROPIC_BASE_URL": root, "ANTHROPIC_API_KEY": PLACEHOLDER_KEY}))
    openai = [name for protocol, name in (("responses", "Responses"), ("chat_completions", "Chat Completions"))
              if protocol in served]
    if openai:
        title = f"OpenAI {openai[0]}" if len(openai) == 1 else f"OpenAI ({' e '.join(openai)})"
        blocks.append((f"{title}: endereço com /v1",
                       {"OPENAI_BASE_URL": f"{root}/v1", "OPENAI_API_KEY": PLACEHOLDER_KEY}))
    comment, assign = ("# {}", "$env:{}='{}'") if shell == "powershell" else ("REM {}", 'set "{}={}"')
    lines = []
    for title, variables in blocks:
        lines.append(comment.format(title))
        lines += [assign.format(name, value) for name, value in variables.items()]
    lines.append(comment.format("A chave pode ser qualquer texto: o endpoint descarta a do cliente e acrescenta a da conexão."))
    return "\n".join(lines)


def connection_config(home: Path, connection_id: str, port: int, shell: str = "powershell") -> str:
    """`client_config` com os protocolos que a conexão serve, lidos do conector (sem rede e sem usar a chave)."""
    from agent_launcher.domain.gateway import Gateway
    from agent_launcher.domain.store import Store
    store = Store(home)
    gateway = Gateway(store, store.find_connection(connection_id))
    return client_config(port, gateway.protocols, shell)


def summarize_log(text: str) -> dict:
    """Conta as linhas de pedido e de failover de um trecho do endpoint.log: pedidos, erros (4xx e 5xx, no total e por
    modelo), failovers feitos e não feitos, latência média e maior, período e quantas linhas foram lidas. Linha que não
    é pedido nem failover (supervisor, redirecionamento da reserva) só conta como linha lida."""
    log_lines = text.splitlines()
    models: dict[str, list] = {}
    stamps, seconds, errors_4xx, errors_5xx, failovers, failed = [], [], 0, 0, 0, 0
    for line in log_lines:
        found = _REQUEST_LINE.match(line)
        if found:
            stamp, model, status, took = found["stamp"], ast.literal_eval(found["model"]), int(found["status"]), float(found["seconds"])
            stamps.append(stamp)
            seconds.append(took)
            item = models.setdefault(model, [0, 0, []])
            item[0] += 1
            item[2].append(took)
            if status >= 400:
                item[1] += 1
                if status < 500:
                    errors_4xx += 1
                else:
                    errors_5xx += 1
        elif _FAILOVER_FAILED.match(line):
            failed += 1
        elif _FAILOVER_DONE.match(line):
            failovers += 1
    by_model = [{"model": model, "requests": count, "errors": errors, "avg_seconds": sum(took) / len(took),
                 "max_seconds": max(took)} for model, (count, errors, took) in models.items()]
    by_model.sort(key=lambda item: (-item["requests"], item["model"]))
    return {"lines": len(log_lines), "requests": len(seconds), "errors": errors_4xx + errors_5xx,
            "errors_4xx": errors_4xx, "errors_5xx": errors_5xx, "failovers": failovers, "failovers_failed": failed,
            "avg_seconds": sum(seconds) / len(seconds) if seconds else None, "max_seconds": max(seconds, default=None),
            "first": stamps[0] if stamps else None, "last": stamps[-1] if stamps else None, "models": by_model}


def summary_text(summary: dict, max_models: int | None = None) -> str:
    """O resumo de `summarize_log` em pt-BR: uma linha de totais, a janela lida e um bloco por modelo (os `max_models`
    mais usados; todos quando None)."""
    if not summary["requests"]:
        return f"Nenhum pedido nas linhas lidas (linhas lidas: {summary['lines']})."
    lines = [f"Pedidos: {summary['requests']} • com erro: {summary['errors']} (4xx: {summary['errors_4xx']}, "
             f"5xx: {summary['errors_5xx']}) • failovers feitos: {summary['failovers']}, não feitos: "
             f"{summary['failovers_failed']} • latência média {summary['avg_seconds']:.1f} s, maior {summary['max_seconds']:.1f} s",
             f"Linhas lidas: {summary['lines']}, de {summary['first']} a {summary['last']}. O log guarda só o fim (tem "
             "teto de tamanho): o resumo não cobre o que já saiu dele.",
             "Por modelo:"]
    shown = summary["models"] if max_models is None else summary["models"][:max_models]
    lines += [f"  {item['model']} • pedidos: {item['requests']} • erros: {item['errors']} • média "
              f"{item['avg_seconds']:.1f} s • maior {item['max_seconds']:.1f} s" for item in shown]
    if len(shown) < len(summary["models"]):
        lines.append(f"  … e mais {len(summary['models']) - len(shown)} modelo(s)")
    return "\n".join(lines)
