"""Arquivos temporários do produto: uma única pasta estável por versão, %LOCALAPPDATA%\\AgentLauncher\\cache\\<versão>.

Nunca um nome novo por execução: o hash da entrada só nomeia a subpasta que a mesma entrada reencontra, então repetir a
execução não cria arquivo. Quem grava ali o faz sob a trava da subpasta (`keyed_folder`) e por temporário na mesma pasta
seguido de rename (`atomic_text`). As pastas de outras versões se apagam na partida (`prune_old_versions`)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

from agent_launcher.domain.files import LockBusy, file_lock
from version import VERSION

APP_FOLDER = "AgentLauncher"
VERSION_NAME = re.compile(r"\d+(\.\d+)+")
LOCK_TIMEOUT_S = 60.0  # cobre o `codex debug models` (20 s) de uma abertura concorrente com a mesma entrada


def cache_root(base: Path | str | None = None) -> Path:
    """%LOCALAPPDATA%\\AgentLauncher\\cache; `base` (testes) substitui o %LOCALAPPDATA%."""
    local = Path(base) if base else Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / APP_FOLDER / "cache"


def cache_dir(version: str = VERSION, base: Path | str | None = None) -> Path:
    """A pasta da versão, criada se faltar."""
    folder = cache_root(base) / version
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def short_hash(key: object) -> str:
    """12 hex da entrada (qualquer valor serializável em JSON)."""
    text = json.dumps(key, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@contextlib.contextmanager
def keyed_folder(prefix: str, key: object, version: str = VERSION, base: Path | str | None = None):
    """A subpasta `<prefix>-<hash da entrada>` da pasta da versão, criada se faltar e sob trava: duas execuções com a
    mesma entrada se alternam em vez de gravar ao mesmo tempo, e a segunda acha o que a primeira gravou."""
    folder = cache_dir(version, base) / f"{prefix}-{short_hash(key)}"
    with file_lock(folder, LOCK_TIMEOUT_S):
        folder.mkdir(exist_ok=True)
        yield folder


def _make_writable_and_retry(function, path, error) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def empty_folder(folder: Path) -> None:
    """Apaga o conteúdo de `folder` e mantém a pasta; arquivo somente leitura (a pasta .git) também sai. O que o
    sistema recusar apagar levanta."""
    for item in folder.iterdir():
        if item.is_dir() and not item.is_symlink() and not item.is_junction():
            shutil.rmtree(item, onexc=_make_writable_and_retry)
        else:
            item.unlink()


@contextlib.contextmanager
def scratch_folder(prefix: str, key: object = None, busy: type[Exception] | None = None, version: str = VERSION,
                   base: Path | str | None = None):
    """Pasta de trabalho fixa para o que não é determinístico (download, extração, clone): a mesma pasta de `keyed_folder`
    a cada execução, esvaziada ao entrar (sobra de execução interrompida) e ao sair (o que foi gravado, chave de deploy
    inclusive, não fica no disco). Trava ocupada levanta `busy(mensagem)` quando dado, senão `LockBusy`."""
    with contextlib.ExitStack() as stack:
        try:
            folder = stack.enter_context(keyed_folder(prefix, key, version, base))
        except LockBusy as error:
            if busy is None:
                raise
            raise busy(str(error)) from error
        empty_folder(folder)
        try:
            yield folder
        finally:
            with contextlib.suppress(OSError):  # antivírus segurando um arquivo: a próxima entrada apaga, e falha se ainda preso
                empty_folder(folder)


def prune_old_versions(version: str = VERSION, base: Path | str | None = None,
                       keep: Iterable[Path | str] = ()) -> list[str]:
    """Apaga as pastas de outras versões (nome de versão: 1.2.3), uma por vez, só quando o caminho resolvido é filho
    direto da pasta base: atalho ou junção para fora nunca é seguido. A pasta atual, outras pastas e arquivos ficam, e
    também a pasta de versão que contém algum caminho de `keep` (o que está em uso). Devolve os nomes apagados; o que
    não deu para apagar (arquivo preso) vira aviso na saída de erro."""
    root = cache_root(base)
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    in_use = [Path(path).resolve() for path in keep]
    removed = []
    for entry in sorted(root.iterdir()):
        if entry.name == version or not VERSION_NAME.fullmatch(entry.name) or not entry.is_dir():
            continue
        if entry.resolve().parent != resolved_root or entry.is_junction() or entry.is_symlink():
            continue
        if any(path.is_relative_to(entry.resolve()) for path in in_use):
            continue
        try:
            shutil.rmtree(entry, onexc=_make_writable_and_retry)
            removed.append(entry.name)
        except OSError as error:
            print(f"Aviso: o cache da versão {entry.name} não foi apagado ({error}).", file=sys.stderr)
    return removed
