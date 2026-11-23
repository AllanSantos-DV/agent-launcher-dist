"""Supervisor do endpoint local de uma conexão: mantém o `serve` de pé e o religa quando ele cai.

Contrato (igual ao de `nativo/endpoint/Supervisor.cs`):

- Instância única por pasta de dados + conexão, pelo mutex `Local\\agent-launcher-endpoint-<chave>`; o segundo supervisor
  sai com 0 na hora.
- Filho morto por fora ou com código diferente de 0: reinicia depois de uma espera que começa em MIN_DELAY, dobra a cada
  morte (teto MAX_DELAY) e volta ao começo quando o filho viveu ao menos STABLE_SECONDS. Cada reinício deixa uma linha
  `supervisor: ...` no endpoint.log.
- O `serve` supervisionado sempre leva `--no-idle-exit`: o fim por ociosidade tiraria o endpoint do ar com o supervisor
  junto. O `serve` avulso, sem supervisor, mantém o prazo de ociosidade.
- Com `wait_port` (adoção de um endpoint avulso), antes de cada `serve` o supervisor espera a porta ficar livre, sem abrir
  o `serve` enquanto ela está ocupada: duas notas no endpoint.log (começou a esperar, porta livre), nenhuma por tentativa.
- Código 0 (/shutdown): o supervisor sai com 0.
- Parada deliberada: o arquivo `endpoint-stop-<chave>` na pasta de dados. Quem o vê sai sem reiniciar, apagando o
  arquivo; filho que não sai em STOP_GRACE_SECONDS é encerrado à força.
"""
from __future__ import annotations

import ctypes
import functools
import hashlib
import os
import socket
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from agent_launcher.domain.files import log_line

MIN_DELAY = 0.5
MAX_DELAY = 30.0
STABLE_SECONDS = 10.0
POLL_SECONDS = 0.25
STOP_GRACE_SECONDS = 10.0
LOG_MAX_BYTES = 1_000_000
MUTEX_PREFIX = "Local\\agent-launcher-endpoint-"
STOP_PREFIX = "endpoint-stop-"
NO_IDLE_EXIT = "--no-idle-exit"
SYNCHRONIZE = 0x00100000
ERROR_ALREADY_EXISTS = 183
CHILD_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)


@functools.cache
def _kernel32():
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateMutexW.restype = wintypes.HANDLE
    lib.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    lib.OpenMutexW.restype = wintypes.HANDLE
    lib.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    return lib


def instance_key(home: Path | str, connection: str) -> str:
    """12 hex do sha256 de "<pasta de dados sem diferenciar maiúsculas nem barra final>|<conexão>" em UTF-8."""
    text = f"{os.path.normcase(os.path.abspath(home)).rstrip(chr(92) + '/')}|{connection}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def mutex_name(home: Path | str, connection: str) -> str:
    """Nome do mutex de sessão que garante um supervisor por pasta de dados e conexão."""
    return MUTEX_PREFIX + instance_key(home, connection)


def stop_file(home: Path | str, connection: str) -> Path:
    """Arquivo, na pasta de dados, cuja presença pede a parada do supervisor da conexão."""
    return Path(home) / (STOP_PREFIX + instance_key(home, connection))


def alive(home: Path | str, connection: str) -> bool:
    """Há um supervisor desta conexão rodando (o mutex existe)."""
    kernel32 = _kernel32()
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name(home, connection))
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def request_stop(home: Path | str, connection: str) -> bool:
    """Pede a parada ao supervisor da conexão, se houver um; True se o pedido foi gravado. Falha de gravação sobe: sem o
    pedido o supervisor religaria o endpoint que se quis parar."""
    if not alive(home, connection):
        return False
    stop_file(home, connection).write_text("stop\n", encoding="utf-8")
    return True


def wait_gone(home: Path | str, connection: str, timeout: float) -> bool:
    """Espera o supervisor da conexão sair (o mutex some com o processo)."""
    deadline = time.monotonic() + timeout
    while alive(home, connection):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def note(log: Path, message: str) -> None:
    """Acrescenta uma linha `supervisor: <mensagem>` ao endpoint.log; log que não grava não derruba quem anota."""
    log_line(log, f"supervisor: {message}", LOG_MAX_BYTES)


def port_free(port: int) -> bool:
    """A porta de 127.0.0.1 está livre (o bind é imediato; uma consulta de rede a porta fechada leva ~2 s no Windows)."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _consume(stop: Path, log: Path) -> bool:
    """Parada pedida: apaga o pedido, anota e devolve True."""
    if not stop.exists():
        return False
    note(log, "parada pedida; encerrando")
    _unlink(stop)
    return True


def _wait_child(process: subprocess.Popen, stop: Path) -> int:
    """Espera o endpoint sair; com a parada pedida e o filho ainda de pé depois da tolerância, encerra-o."""
    requested = None
    while True:
        try:
            return process.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            if stop.exists():
                requested = requested or time.monotonic()
                if time.monotonic() - requested > STOP_GRACE_SECONDS:
                    process.kill()


def _sleep(seconds: float, stop: Path) -> None:
    """Espera `seconds`, acordando antes se a parada for pedida."""
    deadline = time.monotonic() + seconds
    while not stop.exists() and time.monotonic() < deadline:
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _await_port(port: int, stop: Path, log: Path) -> bool:
    """Espera a porta ficar livre, acordando antes se a parada for pedida; False nesse caso."""
    waited = False
    while not port_free(port):
        if not waited:
            note(log, f"porta {port} ocupada; esperando liberar para iniciar o endpoint")
            waited = True
        if stop.exists():
            return False
        _sleep(POLL_SECONDS, stop)
    if waited:
        note(log, f"porta {port} livre; iniciando o endpoint")
    return True


def supervise(home: Path | str, connection: str, child: list[str], wait_port: int | None = None) -> int:
    """Laço do supervisor (processo oculto). `child` é a linha de comando do `serve` desta forma; com `wait_port`, cada
    abertura do `serve` espera antes essa porta ficar livre."""
    child = child if NO_IDLE_EXIT in child else [*child, NO_IDLE_EXIT]
    kernel32 = _kernel32()
    mutex = kernel32.CreateMutexW(None, False, mutex_name(home, connection))
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex)
        return 0
    log, stop = Path(home) / "endpoint.log", stop_file(home, connection)
    _unlink(stop)
    delay = MIN_DELAY
    try:
        while True:
            if _consume(stop, log):
                return 0
            if wait_port is not None and not _await_port(wait_port, stop, log):
                _consume(stop, log)
                return 0
            began = time.monotonic()
            try:
                process = subprocess.Popen(child, cwd=home, close_fds=True, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CHILD_FLAGS)
            except OSError as error:
                reason, code = f"não abriu o endpoint ({error})", None
            else:
                code = _wait_child(process, stop)
                reason = f"endpoint saiu com código {code} após {time.monotonic() - began:.1f} s"
            if _consume(stop, log):
                return 0
            if code == 0:
                note(log, "endpoint saiu com código 0; encerrando")
                return 0
            if time.monotonic() - began >= STABLE_SECONDS:
                delay = MIN_DELAY
            note(log, f"{reason}; reinicia em {delay:.1f} s")
            _sleep(delay, stop)
            delay = min(delay * 2, MAX_DELAY)
    finally:
        kernel32.CloseHandle(mutex)
