"""Windows Terminal para as sessões: encontra, instala por usuário sem admin e explica bloqueios corporativos.

Ordem de instalação (nenhuma exige admin):
  1. Windows Terminal já instalado (alias wt.exe do pacote da Store/MSIX).
  2. Distribuição ZIP oficial (github.com/microsoft/terminal, estável desde 1.17) extraída em <instalação>/terminal
     com o arquivo .portable: não usa Store, MSIX nem winget; exige Windows 10 2004 (build 19041) ou superior.
     O download é conferido pelo SHA-256 publicado na release e pela assinatura Authenticode da Microsoft.
  3. winget, só quando a política não o desativa (instala o pacote MSIX; bloqueado onde MSIX por usuário é proibido).
Ambiente sem acesso ao GitHub: a TI entrega o ZIP e o instalador recebe --terminal-zip.
Sem Terminal, as sessões abrem em consoles separados (Ctrl+J continua quebrando linha nos CLIs).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agent_launcher.domain.files import replace_retrying

RELEASE_API = "https://api.github.com/repos/microsoft/terminal/releases/latest"
MIN_BUILD = 19041
DOWNLOAD_LIMIT = 80 * 1024 * 1024
SIGNER = "CN=Microsoft Corporation"
VERSION_FILE = ".version"
UPDATE_CHECK_INTERVAL_S = 4 * 60 * 60  # a API anônima do GitHub aceita 60 consultas por hora por IP

# (descrição, chave sob HKLM/HKCU, valor, valor que bloqueia)
POLICIES = (
    ("msix", "Instalação de pacotes MSIX por usuário comum bloqueada (BlockNonAdminUserInstall)",
     r"SOFTWARE\Policies\Microsoft\Windows\Appx", "BlockNonAdminUserInstall", 1),
    ("msix", "Instalação de pacotes fora da Store bloqueada (AllowAllTrustedApps=0)",
     r"SOFTWARE\Policies\Microsoft\Windows\Appx", "AllowAllTrustedApps", 0),
    ("store", "Microsoft Store removida (RemoveWindowsStore)",
     r"SOFTWARE\Policies\Microsoft\WindowsStore", "RemoveWindowsStore", 1),
    ("store", "Apps da Microsoft Store desativados (DisableStoreApps)",
     r"SOFTWARE\Policies\Microsoft\WindowsStore", "DisableStoreApps", 1),
    ("store", "Somente a loja privada da empresa (RequirePrivateStoreOnly)",
     r"SOFTWARE\Policies\Microsoft\WindowsStore", "RequirePrivateStoreOnly", 1),
    ("winget", "winget/App Installer desativado (EnableAppInstaller=0)",
     r"SOFTWARE\Policies\Microsoft\Windows\AppInstaller", "EnableAppInstaller", 0),
    ("winget", "Fonte padrão do winget desativada (EnableDefaultSource=0)",
     r"SOFTWARE\Policies\Microsoft\Windows\AppInstaller", "EnableDefaultSource", 0),
    ("exec", "Software Restriction Policies em modo bloqueio (Safer DefaultLevel=0)",
     r"SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers", "DefaultLevel", 0),
)
APPLOCKER_EXE = r"SOFTWARE\Policies\Microsoft\Windows\SrpV2\Exe"


class TerminalError(Exception):
    pass


def portable_dir(root: Path) -> Path:
    return Path(root) / "terminal"


def store_alias() -> Path:
    """Alias de execução que o pacote da Store/MSIX cria para wt.exe (fica na pasta do usuário, fora de Program Files)."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "Microsoft" / "WindowsApps" / "wt.exe"


def find(root: Path | None = None) -> str | None:
    """wt.exe a usar: o do Windows Terminal instalado; senão a cópia portátil desta instalação.

    Não depende só do PATH: um processo aberto por outro com PATH reduzido (serviço, agendador, outro app) não vê
    a pasta WindowsApps e concluía que o Terminal faltava, abrindo as sessões em consoles separados.
    """
    installed = shutil.which("wt.exe")
    if installed:
        return installed
    alias = store_alias()
    if os.path.lexists(alias):  # o alias é um reparse point; lexists não depende de segui-lo
        return str(alias)
    if root is not None:
        portable = portable_dir(root) / "wt.exe"
        if portable.is_file():
            return str(portable)
    return None


def _registry_reader():
    import winreg

    def read(hive: str, key: str, value: str | None):
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, key) as handle:
                if value is None:  # só existência de subchaves (regras do AppLocker)
                    return winreg.QueryInfoKey(handle)[0]
                return winreg.QueryValueEx(handle, value)[0]
        except OSError:
            return None
    return read


def _joined() -> str:
    """'domínio', 'Entra ID' ou '' (dsregcmd existe no Windows 10/11 e não exige admin)."""
    try:
        output = subprocess.run(["dsregcmd", "/status"], capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired):
        output = ""
    flags = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
             for line in output.splitlines() if ":" in line}
    joined = [label for flag, label in (("DomainJoined", "domínio"), ("AzureAdJoined", "Entra ID"))
              if flags.get(flag, "").upper() == "YES"]
    return " + ".join(joined)


def environment(read=None, joined=None) -> dict:
    """Bloqueios por política (HKLM e HKCU) e se a máquina é gerenciada. `read`/`joined` são injetáveis nos testes."""
    read = read or _registry_reader()
    blocks = {"msix": [], "store": [], "winget": [], "exec": []}
    for kind, message, key, value, blocking in POLICIES:
        if any(read(hive, key, value) == blocking for hive in ("HKLM", "HKCU")):
            blocks[kind].append(message)
    if any(read(hive, APPLOCKER_EXE, None) for hive in ("HKLM", "HKCU")):
        blocks["exec"].append("AppLocker com regras de executáveis: pode barrar programas em pastas do usuário")
    managed = joined if joined is not None else _joined()
    windows = sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else None
    return {"managed": managed, "blocks": blocks, "build": windows.build if windows else 0,
            "portable_supported": bool(windows and windows.build >= MIN_BUILD)}


def describe(env: dict) -> list[str]:
    lines = [f"Máquina gerenciada ({env['managed']})" if env["managed"] else "Máquina não ingressada em domínio/Entra ID"]
    lines += [message for messages in env["blocks"].values() for message in messages]
    if not env["portable_supported"]:
        lines.append(f"Windows build {env['build']}: o Terminal portátil exige {MIN_BUILD} (Windows 10 2004) ou superior")
    return lines


def _arch() -> str:
    machine = platform.machine().upper()
    return {"AMD64": "x64", "X86_64": "x64", "ARM64": "arm64", "X86": "x86", "I386": "x86"}.get(machine, "x64")


def _open(url: str, timeout: int = 60):
    import urllib.request  # só quando baixa o Terminal portátil
    request = urllib.request.Request(url, headers={"User-Agent": "agent-launcher", "Accept": "application/json"})
    return urllib.request.urlopen(request, timeout=timeout)  # proxy do sistema: hosts públicos (GitHub)


def latest_zip() -> tuple[str, str, str]:
    """(nome, URL, sha256) do ZIP da última release estável para esta arquitetura."""
    try:
        with _open(RELEASE_API, timeout=20) as response:
            release = json.load(response)
    except (OSError, ValueError) as error:
        raise TerminalError(f"Não foi possível consultar a release do Windows Terminal no GitHub: {error}") from error
    suffix = f"_{_arch()}.zip"
    for asset in release.get("assets", []):
        if str(asset.get("name", "")).endswith(suffix):
            digest = str(asset.get("digest") or "")
            if not digest.startswith("sha256:"):
                raise TerminalError(f"A release {release.get('tag_name')} não publica SHA-256 de {asset['name']}; "
                                    "download recusado. Use --terminal-zip com um ZIP conferido pela TI.")
            return asset["name"], asset["browser_download_url"], digest.split(":", 1)[1]
    raise TerminalError(f"A release {release.get('tag_name')} não tem o ZIP {suffix}.")


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with _open(url, timeout=120) as response, open(destination, "wb") as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > DOWNLOAD_LIMIT:
                    raise TerminalError("Download do Windows Terminal acima do limite esperado; interrompido.")
                digest.update(chunk)
                stream.write(chunk)
    except OSError as error:
        raise TerminalError(f"Download do Windows Terminal falhou ({url}): {error}") from error
    if digest.hexdigest() != expected_sha256.lower():
        raise TerminalError("SHA-256 do ZIP baixado não confere com o publicado na release; arquivo descartado.")


def signature_ok(executable: Path) -> bool:
    """Assinatura Authenticode válida da Microsoft (Get-AuthenticodeSignature não exige admin)."""
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:AL_FILE; "
              "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -like ($env:AL_SIGNER + ',*')) { exit 0 }; exit 1")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, "AL_FILE": str(executable), "AL_SIGNER": SIGNER},
                            capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode == 0


def zip_version(name: str) -> tuple[int, ...] | None:
    """Versão do nome do asset (Microsoft.WindowsTerminal_1.24.10921.0_x64.zip); None se o nome não traz uma."""
    found = re.search(r"_(\d+(?:\.\d+)+)_", name)
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def portable_version(root: Path) -> tuple[int, ...] | None:
    """Versão gravada na cópia portátil ao instalar; None numa cópia anterior a esse registro."""
    try:
        return tuple(int(part) for part in (portable_dir(root) / VERSION_FILE).read_text(encoding="utf-8").strip().split("."))
    except (OSError, ValueError):
        return None


def _free_previous(target: Path) -> Path:
    """Pasta para onde vai a cópia atual durante a troca. Restos de trocas anteriores (antivírus ou um leitor seguram
    arquivos de terminal.old) são varridos sem exigir sucesso; se o nome padrão continuar ocupado, a troca usa um nome
    próprio em vez de falhar e baixar o ZIP de novo a cada abertura."""
    for leftover in target.parent.glob("terminal.old*"):
        shutil.rmtree(leftover, ignore_errors=True)
    previous = target.with_name("terminal.old")
    return target.with_name(f"terminal.old-{uuid.uuid4().hex[:8]}") if previous.exists() else previous


def install_portable(root: Path, zip_path: Path | None = None, verify=signature_ok) -> Path:
    """Extrai o Terminal em <root>/terminal (modo portátil). Substitui uma cópia anterior só depois de validar a nova;
    com o Terminal portátil em uso o Windows recusa renomear a pasta (PermissionError) e a cópia anterior fica intacta."""
    target = portable_dir(root)
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory(prefix="agent-launcher-terminal-", dir=str(Path(root))) as work:
        work = Path(work)
        if zip_path is None:
            name, url, sha256 = latest_zip()
            zip_path = work / name
            _download(url, zip_path, sha256)
        try:
            with zipfile.ZipFile(zip_path) as archive:
                for member in archive.namelist():
                    if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                        raise TerminalError(f"ZIP com caminho inseguro: {member}")
                archive.extractall(work / "x")
        except zipfile.BadZipFile as error:
            raise TerminalError(f"Arquivo não é um ZIP válido: {zip_path}") from error
        folders = [path for path in (work / "x").iterdir() if path.is_dir()]
        extracted = folders[0] if len(folders) == 1 else work / "x"
        for required in ("wt.exe", "WindowsTerminal.exe"):
            if not (extracted / required).is_file():
                raise TerminalError(f"O ZIP não contém {required}; não parece a distribuição do Windows Terminal.")
            if not verify(extracted / required):
                raise TerminalError(f"{required} sem assinatura válida da Microsoft; instalação recusada.")
        (extracted / ".portable").touch()
        version = zip_version(zip_path.name)
        if version:
            (extracted / VERSION_FILE).write_text(".".join(map(str, version)), encoding="utf-8")
        previous = _free_previous(target)
        if target.exists():
            replace_retrying(target, previous)
        try:
            shutil.move(str(extracted), str(target))
        except OSError:
            if previous.exists() and not target.exists():
                replace_retrying(previous, target)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    return target / "wt.exe"


def portable_in_use(root: Path) -> bool:
    """Algum executável da cópia portátil está rodando? O Windows deixa renomear a pasta com o Terminal aberto (a
    troca largaria arquivos da cópia velha e o Terminal em uso continuaria carregando DLLs da pasta renomeada), mas
    não deixa abrir um .exe em execução para escrita: é essa recusa que denuncia o uso."""
    for executable in portable_dir(root).rglob("*.exe"):
        try:
            executable.open("ab").close()
        except PermissionError:
            return True
    return False


def update_portable(root: Path, check=latest_zip, install=install_portable, stamp: Path | None = None,
                    now=time.time) -> str | None:
    """Atualiza a cópia portátil (a única que o launcher mantém: a da Store ou do winget o Windows atualiza). Devolve o
    que fez ("atualizado para X", "adiada: em uso") ou None quando não havia o que fazer. No máximo uma consulta por
    UPDATE_CHECK_INTERVAL_S; consulta ou download que falham levantam TerminalError (a cópia atual segue intacta)."""
    if not (portable_dir(root) / "wt.exe").is_file() or shutil.which("wt.exe") or os.path.lexists(store_alias()):
        return None
    if stamp is None:
        from version import data_home
        stamp = data_home() / "terminal-update-check"
    try:
        if now() - stamp.stat().st_mtime < UPDATE_CHECK_INTERVAL_S:
            return None
    except FileNotFoundError:
        pass
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    name = check()[0]
    latest, current = zip_version(name), portable_version(root)
    if latest and current and latest <= current:
        return None
    try:
        if portable_in_use(root):
            raise PermissionError("Windows Terminal portátil em uso")
        install(root)
    except PermissionError:
        stamp.unlink(missing_ok=True)  # em uso: tenta de novo na próxima verificação
        return "adiada: o Windows Terminal portátil está em uso"
    return f"Windows Terminal portátil atualizado para {'.'.join(map(str, latest)) if latest else name}"


def install_winget() -> None:
    winget = shutil.which("winget.exe")
    if not winget:
        raise TerminalError("winget não está disponível nesta máquina.")
    result = subprocess.run([winget, "install", "--id", "Microsoft.WindowsTerminal", "-e", "--source", "winget",
                             "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
                             "--silent"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not shutil.which("wt.exe"):
        tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
        raise TerminalError("winget não instalou o Windows Terminal: " + " | ".join(tail))


def ensure(root: Path, zip_path: Path | None = None, env: dict | None = None, log=print) -> str | None:
    """Garante um wt.exe; devolve o caminho ou None (sessões em consoles separados). Nunca pede admin."""
    found = find(root)
    if found and zip_path is None:
        log(f"Windows Terminal: {'instalado' if found == shutil.which('wt.exe') else 'portátil'} ({found})")
        return found
    env = env or environment()
    for line in describe(env):
        log(f"  ambiente: {line}")
    errors = []
    if env["portable_supported"]:
        try:
            path = install_portable(root, zip_path)
            log(f"Windows Terminal portátil instalado em {path.parent} (sem Store, MSIX ou admin).")
            return str(path)
        except (TerminalError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"ZIP portátil: {error}")
    if env["blocks"]["winget"] or env["blocks"]["msix"]:
        errors.append("winget: não tentado (política bloqueia winget ou pacotes MSIX por usuário)")
    elif zip_path is None:
        try:
            install_winget()
            log("Windows Terminal instalado pelo winget.")
            return shutil.which("wt.exe")
        except (TerminalError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"winget: {error}")
    for error in errors:
        log(f"  [falhou] {error}")
    log("Windows Terminal indisponível: as sessões abrirão em consoles separados (Ctrl+J quebra linha nos CLIs). "
        "Para instalar depois: launcher.py terminal --install [--zip CAMINHO_DO_ZIP].")
    return None
