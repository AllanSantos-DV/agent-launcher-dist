"""Atualização automática pelo engine-registry: a interface confere a versão publicada a cada abertura e se atualiza sozinha.

Fluxo: manifest do registry → entrada apps[name=agent-launcher] → pacote (.tgz, .sha256, .sig) baixado só de
.../releases/download/agent-launcher-v<versão>/ e conferido por verificar.mjs (sha256 + assinatura Ed25519) contra a chave
fixada em version.REGISTRY_PUBLIC_KEY, nunca só a do manifest → install.py DO PACOTE NOVO roda sobre esta instalação
(cópia por manifesto, backup e rollback; .venv e terminal/ são preservados). Os arquivos trocam no lugar: a janela aberta
segue com o código já carregado e a versão nova vale ao reabrir. Só a cópia instalada se atualiza (tem o manifesto do
instalador); um checkout de desenvolvimento nunca. Toda recusa é erro visível; não há queda para o npm.
Desligar: preferência "Atualizar automaticamente" no menu ou AGENT_LAUNCHER_NO_AUTO_UPDATE=1.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent_launcher import ROOT  # a pasta instalada (onde fica o updater.py da raiz), não a deste módulo
from version import APP_ID, REGISTRY_PUBLIC_KEY, VERSION, data_home

PACKAGE = "@allansantos-dev/agent-launcher"
REGISTRY = "https://registry.npmjs.org/"  # só as CLIs (cli_update); o próprio launcher se atualiza pelo engine-registry
REGISTRY_REPO = "AllanSantos-DV/engine-registry"
MANIFEST_URL = "https://raw.githubusercontent.com/AllanSantos-DV/engine-registry/main/manifest.json"
DOWNLOAD_BASE = f"https://github.com/{REGISTRY_REPO}/releases/download/"
PLATFORM = "win32-x64"
VERIFIER = Path(__file__).with_name("verificar.mjs")
MANIFEST = ".install-manifest.json"  # o mesmo do install.py
DOWNLOAD_LIMIT = 20 * 1024 * 1024
LOCK_STALE_S = 30 * 60
DISABLE_ENV = "AGENT_LAUNCHER_NO_AUTO_UPDATE"
UPDATE_COMMAND = f"npx {PACKAGE}@latest install"


class UpdateError(Exception):
    pass


class CheckError(UpdateError):
    """Não deu para consultar o registry (sem rede, proxy): nada foi tocado."""


def parts(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(value).split("-")[0].split("."))


def installed(root: Path = ROOT) -> bool:
    """Só a cópia feita pelo instalador se atualiza (um checkout de desenvolvimento não tem o manifesto)."""
    return (Path(root) / MANIFEST).is_file()


def disabled_by_env() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() not in ("", "0")


def _get(url: str, timeout: float, limit: int) -> bytes:
    import urllib.request  # só quando há rede
    request = urllib.request.Request(url, headers={"User-Agent": f"agent-launcher/{VERSION}", "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # proxy do sistema: host público
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"Resposta de {url} passou de {limit // (1024 * 1024)} MB; atualização recusada.")
    return data


def latest(manifest_url: str = MANIFEST_URL, key: str = REGISTRY_PUBLIC_KEY, timeout: float = 10) -> dict:
    """{version, tag, asset} da entrada do Agent Launcher no manifest do registry, já conferida contra a chave fixada."""
    try:
        manifest = json.loads(_get(manifest_url, timeout, 2 * 1024 * 1024))
    except (OSError, ValueError) as error:
        raise CheckError(f"Não foi possível consultar {manifest_url}: {error}") from error
    apps = manifest.get("apps") if isinstance(manifest, dict) else None
    entry = next((app for app in apps or [] if isinstance(app, dict) and app.get("name") == APP_ID), None)
    if entry is None:
        raise UpdateError(f"O manifest do registry não tem a entrada {APP_ID}; atualização recusada.")
    version, release = entry.get("version"), entry.get("release")
    if not isinstance(release, dict):
        raise UpdateError("A entrada do manifest não tem release; atualização recusada.")
    if not (isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise UpdateError(f"Versão publicada inválida: {version!r}")
    if parts(version) < parts(VERSION):
        raise UpdateError(f"O registry anuncia a {version}, menor que a instalada ({VERSION}); atualização recusada.")
    if release.get("publicKey") != key:
        raise UpdateError("A chave pública do manifest difere da fixada no Agent Launcher; atualização recusada.")
    if release.get("repo") != REGISTRY_REPO:
        raise UpdateError(f"Release fora de {REGISTRY_REPO} ({release.get('repo')!r}); atualização recusada.")
    tag = str(release.get("tag", "")).replace("{version}", version)
    if tag != f"{APP_ID}-v{version}":
        raise UpdateError(f"Tag da release inesperada ({tag!r}); atualização recusada.")
    asset = next((item for item in release.get("assets") or [] if isinstance(item, dict) and item.get("platform") == PLATFORM), None)
    if asset is None:
        raise UpdateError(f"O manifest não traz asset para {PLATFORM}; atualização recusada.")
    name = str(asset.get("name", "")).replace("{version}", version)
    if name != f"{APP_ID}-{version}-{PLATFORM}.tgz":
        raise UpdateError(f"Asset fora da release ({name!r}); atualização recusada.")
    return {"version": version, "tag": tag, "asset": name}


def newer(meta: dict) -> bool:
    return parts(meta["version"]) > parts(VERSION)


def verify(asset: Path, key: str, node: str | None = None) -> None:
    """Confere sha256 e assinatura Ed25519 do asset (e dos seus .sha256/.sig ao lado) com verificar.mjs; sem node falha alto."""
    executable = node if node is not None else shutil.which("node")
    if not executable:
        raise UpdateError("`node` não encontrado: sem ele não dá para conferir a assinatura do pacote; atualização recusada. "
                          "Instale o Node.js 18 ou mais novo.")
    try:
        result = subprocess.run([executable, str(VERIFIER), str(asset), key], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Não foi possível rodar o verificador com {executable}: {error}; atualização recusada.") from error
    try:
        verdict = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        verdict = {}
    if result.returncode or verdict.get("ok") is not True:
        reason = verdict.get("reason") or (result.stderr or result.stdout).strip()[-300:] or "sem resposta do verificador"
        raise UpdateError(f"Pacote recusado pela conferência de assinatura: {reason}")


def download(meta: dict, folder: Path, timeout: float = 60, download_base: str = DOWNLOAD_BASE,
             key: str = REGISTRY_PUBLIC_KEY, node: str | None = None) -> Path:
    """Baixa asset, .sha256 e .sig só da release anunciada, confere e extrai; devolve a pasta do pacote."""
    import tarfile
    import urllib.error
    prefix = f"{download_base}{meta['tag']}/"
    archive = Path(folder) / meta["asset"]
    for suffix in ("", ".sha256", ".sig"):
        url = prefix + meta["asset"] + suffix
        try:
            data = _get(url, timeout, DOWNLOAD_LIMIT)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise UpdateError(f"{url} não existe (404): a release {meta['version']} não está completa no registry; "
                                  "atualização recusada.") from error
            raise UpdateError(f"Não foi possível baixar {url}: {error}") from error
        except OSError as error:
            raise UpdateError(f"Não foi possível baixar {url}: {error}") from error
        Path(str(archive) + suffix).write_bytes(data)
    verify(archive, key, node)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():  # o pacote só tem arquivos e pastas sob package/
                name = Path(member.name)
                if not (member.isfile() or member.isdir()) or name.is_absolute() or ".." in name.parts:
                    raise UpdateError(f"Entrada inesperada no pacote ({member.name}); atualização recusada.")
            if hasattr(tarfile, "data_filter"):
                tar.extractall(folder, filter="data")
            else:  # Python 3.10 sem o filtro: as entradas já foram conferidas acima
                tar.extractall(folder)
        package = Path(folder) / "package"
        version = re.search(r'^VERSION = "([^"]+)"', (package / "version.py").read_text(encoding="utf-8"), re.M)
    except (OSError, tarfile.TarError) as error:
        raise UpdateError(f"Pacote baixado ilegível ({error}); atualização recusada.") from error
    if not (package / "install.py").is_file() or not version or version.group(1) != meta["version"]:
        raise UpdateError(f"O pacote baixado não é o Agent Launcher {meta['version']}; atualização recusada.")
    return package


def interpreter(root: Path, windowless: bool = False) -> Path:
    """Python que roda o instalador/atualizador: o da venv da instalação se ele de fato executa (o pyvenv.cfg aponta para
    um Python base que ainda existe); senão o que está rodando agora ou o `python` do PATH. Nunca um caminho gravado."""
    scripts = Path(root) / ".venv" / "Scripts"
    python = scripts / ("pythonw.exe" if windowless else "python.exe")
    try:
        config = (Path(root) / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
        home = next(line.split("=", 1)[1].strip() for line in config.splitlines() if line.split("=", 1)[0].strip() == "home")
        base_ok = (Path(home) / "python.exe").is_file()
    except (OSError, StopIteration):
        base_ok = False
    if python.is_file() and base_ok:
        return python
    running = Path(sys.executable)
    if running.is_file() and not running.is_relative_to(Path(root) / ".venv"):
        return running
    found = shutil.which("python")
    if found:
        return Path(found)
    raise UpdateError("Nenhum Python utilizável: a venv da instalação perdeu o Python de origem e não há `python` no PATH. "
                      "Reinstale com: " + UPDATE_COMMAND)


def apply(package: Path, root: Path = ROOT, timeout: float = 900) -> None:
    """Roda o instalador da versão nova sobre esta instalação (sem criar atalhos, Terminal nem CLIs: já existem; os atalhos existentes são regravados)."""
    command = [str(interpreter(root)), str(Path(package) / "install.py"),
               "--target", str(root), "--skip-tests", "--refresh-shortcuts", "--no-terminal", "--no-clis"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Não foi possível rodar o instalador da versão nova ({error}).") from error
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
    except PermissionError:
        return False  # trava velha segura por antivírus ou leitor: não atualiza agora, tenta na próxima verificação
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except (FileExistsError, PermissionError):  # PermissionError: trava com remoção pendente no Windows
        return False


def update(root: Path = ROOT, manifest_url: str = MANIFEST_URL, log=lambda message: None,
           download_base: str = DOWNLOAD_BASE, key: str = REGISTRY_PUBLIC_KEY, node: str | None = None) -> str | None:
    """Atualiza se houver versão nova; devolve a versão instalada, ou None quando não havia o que fazer. Manifest, base
    de download, chave e `node` são parâmetros para o teste usar um servidor local; os padrões são os do registry."""
    if not installed(root):
        log("Atualização automática só vale para a cópia instalada (sem manifesto do instalador aqui).")
        return None
    meta = latest(manifest_url, key)
    if not newer(meta):
        log(f"Agent Launcher {VERSION} já é a versão mais recente.")
        return None
    lock = data_home() / "update.lock"
    if not _lock(lock):
        log("Trava do atualizador ocupada (outra janela atualizando, ou arquivo preso por outro processo).")
        return None
    try:
        log(f"Atualizando o Agent Launcher {VERSION} → {meta['version']}…")
        import tempfile
        # ignore_cleanup_errors: antivírus segurando um .py extraído não pode transformar uma instalação que deu certo em "falhou".
        with tempfile.TemporaryDirectory(prefix="agent-launcher-update-", ignore_cleanup_errors=True) as folder:
            apply(download(meta, Path(folder), download_base=download_base, key=key, node=node), root)
        log(f"Agent Launcher atualizado para {meta['version']}. Reabra para usar a versão nova.")
        return meta["version"]
    finally:
        try:
            lock.unlink(missing_ok=True)
        except PermissionError:
            log(f"Trava {lock} não removida (arquivo em uso); vale como velha depois de {LOCK_STALE_S // 60} min.")


def record(message: str) -> None:
    """Última verificação em ~/.agent-launcher/update.log (o Diagnóstico mostra; falha de rede não vira janela)."""
    try:
        destination = data_home() / "update.log"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n", encoding="utf-8")
    except OSError:
        pass


CHECK_INTERVAL_S = 4 * 60 * 60  # o mesmo intervalo da interface aberta


def check_in_background(root: Path = ROOT, enabled: bool = True, stamp: Path | None = None,
                        spawn=subprocess.Popen) -> bool:
    """Modo texto e sessões abertas por outro programa (sem a interface) também se atualizam: no máximo a cada
    CHECK_INTERVAL_S, dispara este módulo num processo oculto e desligado deste (não atrasa o comando nem abre janela).
    Devolve se disparou. Só a cópia instalada; desligado pela preferência ou por AGENT_LAUNCHER_NO_AUTO_UPDATE."""
    if not enabled or disabled_by_env() or not installed(root):
        return False
    stamp = stamp or data_home() / "update-check"
    try:
        if time.time() - stamp.stat().st_mtime < CHECK_INTERVAL_S:
            return False
    except FileNotFoundError:
        pass
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    try:
        python = interpreter(root, windowless=True)
    except UpdateError as error:
        record(f"Atualização em segundo plano não disparada: {error}")
        return False
    try:
        stderr = (data_home() / "update-erro.log").open("ab")  # exceção antes do try de background_main também deixa rastro
    except OSError:
        stderr = subprocess.DEVNULL
    try:
        spawn([str(python), str(Path(root) / "updater.py"), "--background"],
              cwd=root, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
    finally:
        if stderr is not subprocess.DEVNULL:
            stderr.close()
    return True


def background_main() -> int:
    """updater.py --background da raiz (processo oculto disparado por check_in_background)."""
    try:
        notes: list[str] = []
        installed_version = update(ROOT, log=notes.append)
        record((f"Atualizado em segundo plano para {installed_version}." if installed_version
                else f"Verificação em segundo plano: {VERSION} já é a mais recente (ou outra atualização em andamento).")
               + "".join(f" {note}" for note in notes if "Trava" in note))
    except UpdateError as error:
        record(f"Atualização em segundo plano falhou: {error}")
    except Exception as error:  # processo oculto: sem isto morreria sem deixar rastro no update.log
        record(f"Atualização em segundo plano falhou: {type(error).__name__}: {error}")
    return 0
