"""Atualização automática pelo npm: a interface confere a versão publicada a cada abertura e se atualiza sozinha.

Fluxo: /latest do registro → pacote baixado só do próprio registro, conferido pelo sha512 publicado (dist.integrity) →
install.py DO PACOTE NOVO roda sobre esta instalação (cópia por manifesto, backup e rollback; .venv e terminal/ são
preservados). Os arquivos trocam no lugar: a janela aberta segue com o código já carregado e a versão nova vale ao
reabrir. Só a cópia instalada se atualiza (tem o manifesto do instalador); um checkout de desenvolvimento nunca.
Desligar: preferência "Atualizar automaticamente" no menu ou AGENT_LAUNCHER_NO_AUTO_UPDATE=1.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from version import VERSION, data_home

ROOT = Path(__file__).resolve().parent
PACKAGE = "@allansantos-dev/agent-launcher"
REGISTRY = "https://registry.npmjs.org/"
MANIFEST = ".install-manifest.json"  # o mesmo do install.py
DOWNLOAD_LIMIT = 20 * 1024 * 1024
LOCK_STALE_S = 30 * 60
DISABLE_ENV = "AGENT_LAUNCHER_NO_AUTO_UPDATE"
UPDATE_COMMAND = f"npx {PACKAGE}@latest install"


class UpdateError(Exception):
    pass


class CheckError(UpdateError):
    """Não deu para consultar o registro (sem rede, proxy): nada foi tocado."""


def parts(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(value).split("-")[0].split("."))


def installed(root: Path = ROOT) -> bool:
    """Só a cópia feita pelo instalador se atualiza (um checkout de desenvolvimento não tem o manifesto)."""
    return (Path(root) / MANIFEST).is_file()


def disabled_by_env() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() not in ("", "0")


def _get(url: str, timeout: float, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"agent-launcher/{VERSION}", "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # proxy do sistema: host público
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"Resposta de {url} passou de {limit // (1024 * 1024)} MB; atualização recusada.")
    return data


def latest(registry: str = REGISTRY, timeout: float = 10) -> dict:
    """{version, tarball, integrity} da última versão publicada."""
    url = registry + PACKAGE.replace("/", "%2F") + "/latest"
    try:
        meta = json.loads(_get(url, timeout, 2 * 1024 * 1024))
    except (OSError, ValueError) as error:
        raise CheckError(f"Não foi possível consultar {url}: {error}") from error
    dist = meta.get("dist") or {}
    version, tarball, integrity = meta.get("version"), dist.get("tarball"), dist.get("integrity")
    if not (isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise UpdateError(f"Versão publicada inválida: {version!r}")
    if not (isinstance(tarball, str) and tarball.startswith(registry)):
        raise UpdateError(f"Pacote fora do registro ({tarball!r}); atualização recusada.")
    if not (isinstance(integrity, str) and integrity.startswith("sha512-")):
        raise UpdateError("O registro não publicou o sha512 do pacote; atualização recusada.")
    return {"version": version, "tarball": tarball, "integrity": integrity}


def newer(meta: dict) -> bool:
    return parts(meta["version"]) > parts(VERSION)


def download(meta: dict, folder: Path, timeout: float = 60) -> Path:
    """Baixa, confere o sha512 e extrai; devolve a pasta do pacote (com install.py da versão anunciada)."""
    try:
        data = _get(meta["tarball"], timeout, DOWNLOAD_LIMIT)
    except urllib.error.HTTPError as error:
        if error.code == 404:  # logo após publicar, /latest já anuncia a versão e a CDN ainda não tem o pacote
            raise CheckError(f"Pacote {meta['version']} ainda propagando no npm; tenta de novo na próxima abertura.") from error
        raise UpdateError(f"Não foi possível baixar {meta['tarball']}: {error}") from error
    except OSError as error:
        raise UpdateError(f"Não foi possível baixar {meta['tarball']}: {error}") from error
    expected = meta["integrity"].removeprefix("sha512-")
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != expected:
        raise UpdateError("O pacote baixado não confere com o sha512 publicado no registro; atualização recusada.")
    archive = Path(folder) / "package.tgz"
    archive.write_bytes(data)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():  # o pacote npm só tem arquivos e pastas sob package/
            name = Path(member.name)
            if not (member.isfile() or member.isdir()) or name.is_absolute() or ".." in name.parts:
                raise UpdateError(f"Entrada inesperada no pacote ({member.name}); atualização recusada.")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(folder, filter="data")
        else:  # Python 3.10 sem o filtro: as entradas já foram conferidas acima
            tar.extractall(folder)
    package = Path(folder) / "package"
    version = re.search(r'^VERSION = "([^"]+)"', (package / "version.py").read_text(encoding="utf-8"), re.M)
    if not (package / "install.py").is_file() or not version or version.group(1) != meta["version"]:
        raise UpdateError(f"O pacote baixado não é o Agent Launcher {meta['version']}; atualização recusada.")
    return package


def apply(package: Path, root: Path = ROOT, timeout: float = 900) -> None:
    """Roda o instalador da versão nova sobre esta instalação (sem atalhos, Terminal nem CLIs: já existem)."""
    python = Path(root) / ".venv" / "Scripts" / "python.exe"
    command = [str(python if python.is_file() else Path(sys.executable)), str(Path(package) / "install.py"),
               "--target", str(root), "--skip-tests", "--no-shortcuts", "--no-terminal", "--no-clis"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise UpdateError("O instalador da versão nova falhou e restaurou a anterior: "
                          + (result.stderr or result.stdout).strip()[-600:])


def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - path.stat().st_mtime > LOCK_STALE_S:
            path.unlink()  # atualização anterior morreu no meio
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def update(root: Path = ROOT, registry: str = REGISTRY, log=lambda message: None) -> str | None:
    """Atualiza se houver versão nova; devolve a versão instalada, ou None quando não havia o que fazer."""
    if not installed(root):
        log("Atualização automática só vale para a cópia instalada (sem manifesto do instalador aqui).")
        return None
    meta = latest(registry)
    if not newer(meta):
        log(f"Agent Launcher {VERSION} já é a versão mais recente.")
        return None
    lock = data_home() / "update.lock"
    if not _lock(lock):
        log("Outra janela já está atualizando o Agent Launcher.")
        return None
    try:
        log(f"Atualizando o Agent Launcher {VERSION} → {meta['version']}…")
        with tempfile.TemporaryDirectory(prefix="agent-launcher-update-") as folder:
            apply(download(meta, Path(folder)), root)
        log(f"Agent Launcher atualizado para {meta['version']}. Reabra para usar a versão nova.")
        return meta["version"]
    finally:
        lock.unlink(missing_ok=True)


def record(message: str) -> None:
    """Última verificação em ~/.agent-launcher/update.log (o Diagnóstico mostra; falha de rede não vira janela)."""
    try:
        destination = data_home() / "update.log"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n", encoding="utf-8")
    except OSError:
        pass
