"""Gravação atômica e validação de nomes, caminhos e IDs de modelo."""
from __future__ import annotations

import contextlib
import json
import msvcrt
import os
import random
import shutil
import time
from pathlib import Path

from agent_launcher.domain.catalog import MODEL_PATTERN
from agent_launcher.domain.errors import LauncherError


def clean_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LauncherError(f"{label} não pode ficar vazio.")
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise LauncherError(f"{label} contém caracteres de controle.")
    return value.strip()


def project_path(value: str) -> Path:
    path = Path(clean_text(value, "Caminho")).expanduser().resolve()
    if not path.is_dir():
        raise LauncherError(f"Pasta de projeto não encontrada: {path}")
    return path


def path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
        raise LauncherError("ID de modelo inválido. Use o ID completo do catálogo, incluindo o provider.")
    return model


REPLACE_ATTEMPTS = 10
REPLACE_TIMEOUT = 5.0   # s: o Windows recusa enquanto qualquer processo/thread mantém o arquivo aberto sem FILE_SHARE_DELETE
                        # (o open do Python nunca o passa) ou o antivírus o varre; sob carga isso passa de 1 s


def _give_up(attempt: int, attempts: int, started: float, timeout: float) -> bool:
    """Depois de uma recusa: desiste só quando as tentativas mínimas E o prazo acabaram; senão espera (crescente, com
    variação, no máximo 0,25 s) para a próxima."""
    if attempt >= attempts and time.monotonic() - started >= timeout:
        return True
    time.sleep(min(0.015 * attempt, 0.25) * (0.5 + random.random()))
    return False


def replace_retrying(source, destination, attempts: int = REPLACE_ATTEMPTS, timeout: float = REPLACE_TIMEOUT) -> None:
    """os.replace que repete quando o Windows recusa por um leitor ou outro escritor com o destino aberto (EPERM,
    EACCES e EBUSY chegam como PermissionError). Repete por no mínimo `attempts` tentativas e por `timeout` segundos;
    esgotados os dois, levanta."""
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if _give_up(attempt, attempts, started, timeout):
                raise


def read_text_retrying(path, attempts: int = REPLACE_ATTEMPTS, encoding: str = "utf-8-sig", errors: str = "strict",
                       timeout: float = REPLACE_TIMEOUT) -> str:
    """read_text que repete quando o Windows recusa a abertura por um replace em andamento no mesmo arquivo (a leitura
    concorrente com o rename atômico chega como PermissionError); mesmo prazo de replace_retrying, depois levanta."""
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return Path(path).read_text(encoding=encoding, errors=errors)
        except PermissionError:
            if _give_up(attempt, attempts, started, timeout):
                raise


def note_state_recovery(folder: Path, message: str) -> None:
    """Uma linha em state-recovery.log da pasta: o que foi preservado ou restaurado de um arquivo de estado."""
    try:
        with (Path(folder) / "state-recovery.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def read_json_state(path, accept=lambda data: isinstance(data, dict), backups: Path | None = None, prefix: str = ""):
    """Lê um arquivo de estado JSON. None se não existe. Ilegível ou fora do formato: guarda uma cópia ao lado
    (<nome>.invalid-<data>) para a próxima gravação não apagar o que sobrou, tenta a cópia mais recente de `backups`
    (<prefix>*.json) e devolve None só se nenhuma serve. Leitura recusada por gravação em andamento repete; recusa
    persistente levanta (nunca vira "vazio")."""
    path = Path(path)
    try:
        text = read_text_retrying(path)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        if accept(data):
            return data
    except ValueError:
        pass
    kept = path.with_name(f"{path.name}.invalid-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copyfile(path, kept)
    except OSError:
        kept = None
    note_state_recovery(path.parent, f"{path.name} ilegível ou fora do formato; cópia em {kept}")
    for copy in sorted(Path(backups).glob(f"{prefix}*.json"), reverse=True) if backups else []:
        try:
            data = json.loads(read_text_retrying(copy))
        except (OSError, ValueError):
            continue
        if accept(data):
            atomic_json(path, data)
            note_state_recovery(path.parent, f"{path.name} restaurado da cópia {copy.name}")
            return data
    return None


def remove_tree(path, attempts: int = 5) -> None:
    """shutil.rmtree que repete quando o Windows recusa por arquivo em uso (um pythonw da venv ainda carregado, o
    antivírus varrendo); esgotadas as tentativas, levanta o último erro."""
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(0.5 * attempt)


class LockBusy(LauncherError):
    """O bloqueio de `file_lock` continua com outro processo depois do prazo."""


@contextlib.contextmanager
def file_lock(path, timeout: float = 10.0):
    """Exclusão mútua entre processos para ler-alterar-gravar `path`: bloqueio do sistema num arquivo ao lado
    (<nome>.lock), solto sozinho se o processo morre, então nunca fica velho. Passado `timeout`, levanta."""
    lock = Path(path).with_name(Path(path).name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(f"{Path(path).name} está em uso por outro processo do launcher; tente de novo.") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, data: dict) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_text(path: Path, text: str) -> None:
    """Grava por temporário de nome próprio (vários processos escrevem o mesmo arquivo) e rename com retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        replace_retrying(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # temporário preso por antivírus: a exceção do replace (ou o sucesso) não pode ser mascarada
