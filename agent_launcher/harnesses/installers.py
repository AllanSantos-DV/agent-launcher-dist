"""CLIs de terminal: onde ficam as instalações por usuário e como instalá-los sem admin.

Ordem (nenhuma exige admin):
  1. Instalador oficial do fabricante, por usuário; o script confere o SHA-256 do binário e o launcher confere de novo a
     assinatura Authenticode do executável que ficou (SIGNERS), apagando o que não for do fabricante:
     Claude Code: https://claude.ai/install.ps1 -> ~/.local/bin/claude.exe (code.claude.com/docs/en/setup)
     Codex: https://chatgpt.com/codex/install.ps1 -> %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe
  2. npm install -g (quem instalou o launcher pelo npm tem Node; o pacote do Claude exige Node 22+).
  3. winget, por usuário, para quem o publica (GitHub Copilot CLI: GitHub.Copilot).
Os instaladores oficiais acrescentam a pasta ao PATH do usuário, mas o processo atual não vê a mudança: por isso
native_paths() é consultado além do PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from agent_launcher.domain.files import replace_retrying
from agent_launcher.platform import terminal

NAMES = {"claude": "Claude Code", "codex": "Codex", "copilot": "GitHub Copilot CLI"}
OFFICIAL = {"claude": "https://claude.ai/install.ps1", "codex": "https://chatgpt.com/codex/install.ps1"}
# Início do Subject do certificado Authenticode dos binários nativos de cada fabricante (lidos dos pacotes oficiais).
SIGNERS = {"claude": 'CN="Anthropic, PBC"', "codex": 'CN="OpenAI OpCo, LLC"'}
NPM_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "copilot": "@github/copilot"}
NODE_MINIMUM = {"claude": 22, "codex": 18, "copilot": 22}
WINGET = {"copilot": "GitHub.Copilot"}
MANUAL = {"claude": OFFICIAL["claude"], "codex": OFFICIAL["codex"], "copilot": "https://github.com/github/copilot-cli"}
# O npm 11 não roda o postinstall de dependências sem --allow-scripts: o Claude Code deixa bin/claude.exe como script de
# texto e o Windows recusa abri-lo. O Codex não tem postinstall; o binário vem do pacote opcional da plataforma.
NPM_FLAGS = {"claude": ["--include=optional", "--allow-scripts=@anthropic-ai/claude-code"], "codex": ["--include=optional"]}
NPM_REPAIR = {cli: f"npm install -g {NPM_PACKAGES[cli]}@latest {' '.join(flags)}" for cli, flags in NPM_FLAGS.items()}
CLAUDE_PLATFORM = Path("node_modules") / "@anthropic-ai" / "claude-code-win32-x64" / "claude.exe"
CODEX_PLATFORM = (Path("node_modules") / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
LOG_FILE = Path(os.environ.get("USERPROFILE") or Path.home()) / ".agent-launcher" / "cli-install.log"


class CliInstallError(Exception):
    pass


def native_paths(cli: str) -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if cli == "claude":
        return [home / ".local" / "bin" / "claude.exe"]
    if cli == "codex":
        custom = os.environ.get("CODEX_INSTALL_DIR")  # o instalador oficial aceita essa pasta alternativa
        return ([Path(custom) / "codex.exe"] if custom else []) + [local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"]
    if cli == "copilot":
        return [local / "Microsoft" / "WinGet" / "Links" / "copilot.exe"]
    raise ValueError(cli)


def find(cli: str) -> str | None:
    """Executável do CLI: o do PATH, senão o da instalação nativa por usuário."""
    found = shutil.which(cli)
    if found:
        return found
    return next((str(path) for path in native_paths(cli) if path.is_file()), None)


def _run(command: list[str], timeout: int, environment: dict | None = None) -> subprocess.CompletedProcess:
    """subprocess.run sem janela. No timeout mata a árvore inteira: o node que o npm.cmd deixa vivo segura os pipes e
    o `run` comum nunca voltaria do kill."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", env={**os.environ, **(environment or {})}, creationflags=flags)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, creationflags=flags)
        process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _tail(result: subprocess.CompletedProcess, count: int = 8) -> str:
    lines = [line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()]
    return f"código de saída {result.returncode}; " + (" | ".join(lines[-count:]) or "sem saída")


def record(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass  # o texto segue na exceção e no log da tela; o arquivo é só o registro


def _fail(cli: str, label: str, message: str):
    """Registra a falha em LOG_FILE (o texto também sobe na exceção) e a levanta."""
    record(f"{NAMES[cli]} ({label}): {message}")
    raise CliInstallError(message)


def is_native(path: Path) -> bool:
    """Executável do Windows de verdade (cabeçalho MZ); o npm deixa script de texto com o nome .exe."""
    try:
        with path.open("rb") as stream:
            return stream.read(2) == b"MZ"
    except OSError:
        return False


def _package_roots(path: Path, package: str) -> list[Path]:
    """Pastas do pacote npm de um CLI a partir do que `find` devolveu (shim .cmd da pasta global, ou o próprio bin/)."""
    scope, name = package.split("/")
    roots = [path.parent / "node_modules" / scope / name, path.parent.parent / scope / name]
    if path.parent.name == "bin" and path.parent.parent.name == name:
        roots.append(path.parent.parent)
    return [root for root in dict.fromkeys(roots) if root.is_dir()]


def claude_binary(path: Path) -> Path | None:
    """Binário nativo do Claude Code instalado pelo npm, ou None sem nenhum pacote do npm em volta de `path`.
    Pula o que não tem cabeçalho MZ, prefere o pacote da plataforma quando bin/claude.exe é o provisório e o repara
    (copia o binário da plataforma por cima). Sem binário válido, levanta com o comando de reparo."""
    roots = _package_roots(path, "@anthropic-ai/claude-code")
    if not roots:
        return None
    provisional = [path] if path.suffix.lower() == ".exe" and path.is_file() else []
    candidates = provisional + [root / "bin" / "claude.exe" for root in roots] + [root / CLAUDE_PLATFORM for root in roots]
    found = next((candidate for candidate in candidates if is_native(candidate)), None)
    if found is None:
        invalid = [str(candidate) for candidate in dict.fromkeys(candidates) if candidate.is_file()]
        raise CliInstallError((f"{', '.join(invalid)} não é um executável do Windows (cabeçalho MZ ausente). "
                               if invalid else "Binário nativo do Claude Code não encontrado no pacote do npm. ") +
                              f"Repare com: {NPM_REPAIR['claude']}")
    for stub in (candidate for candidate in candidates if candidate.is_file() and candidate != found
                 and candidate.parent.name == "bin" and not is_native(candidate)):
        try:
            descriptor, partial = tempfile.mkstemp(prefix=stub.name + ".", suffix=".reparo", dir=stub.parent)
            os.close(descriptor)
            shutil.copyfile(found, partial)
            replace_retrying(partial, stub)
        except OSError as error:
            print(f"  Aviso: não foi possível reparar {stub} ({error}); usando {found}.")
    return found


def codex_platform_binary(path: Path) -> Path | None:
    """Codex.exe do pacote da plataforma instalado pelo npm (None se o pacote opcional não veio)."""
    return next((root / CODEX_PLATFORM for root in _package_roots(path, "@openai/codex")
                 if is_native(root / CODEX_PLATFORM)), None)


def _signed(path: Path, signer: str) -> bool:
    """signature_ok sem exceção: PowerShell travado ou ausente conta como não assinado (o binário é apagado, nunca aceito)."""
    try:
        return terminal.signature_ok(path, signer)
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_official(cli: str) -> None:
    # O script do fabricante roda como texto (irm | iex); -ExecutionPolicy Bypass só vale onde a política da empresa
    # não está fixada por GPO, e o erro volta legível nesse caso.
    script = f"$ProgressPreference = 'SilentlyContinue'; irm '{OFFICIAL[cli]}' | iex"
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                  timeout=900, environment={"CODEX_NON_INTERACTIVE": "1"})
    installed = [path for path in native_paths(cli) if path.is_file()]
    if not installed:
        _fail(cli, "instalador oficial", f"instalador oficial ({OFFICIAL[cli]}) não deixou o executável: {_tail(result)}")
    unsigned = [path for path in installed if not _signed(path, SIGNERS[cli])]
    for path in unsigned:  # fica no lugar onde `find` o acharia depois: apaga antes de recusar
        try:
            path.unlink()
        except OSError as error:
            _fail(cli, "instalador oficial", f"{path} sem assinatura válida de {SIGNERS[cli]} e não pôde ser apagado ({error}); "
                                              "apague-o à mão.")
    if unsigned:
        _fail(cli, "instalador oficial", f"{', '.join(map(str, unsigned))} sem assinatura Authenticode válida de "
                                         f"{SIGNERS[cli]}; o executável foi apagado e a instalação recusada.")


def _node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    match = re.match(r"v(\d+)", _run([node, "--version"], timeout=30).stdout.strip())
    return int(match.group(1)) if match else None


def install_npm(cli: str) -> None:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise CliInstallError("npm não encontrado (Node.js não instalado).")
    major = _node_major()
    if major is None or major < NODE_MINIMUM[cli]:
        raise CliInstallError(f"{NPM_PACKAGES[cli]} exige Node {NODE_MINIMUM[cli]}+; encontrado: "
                              f"{'nenhum' if major is None else major}.")
    result = _run([npm, "install", "-g", NPM_PACKAGES[cli], *NPM_FLAGS[cli]], timeout=900)
    installed = find(cli)
    if result.returncode or not installed:
        _fail(cli, "npm", f"npm install -g {NPM_PACKAGES[cli]} falhou: {_tail(result)}")
    try:
        usable = claude_binary(Path(installed)) if cli == "claude" else codex_platform_binary(Path(installed))
    except CliInstallError as error:
        _fail(cli, "npm", str(error))
    if usable is None and cli == "codex" and Path(installed).suffix.lower() != ".exe":
        _fail(cli, "npm", f"{NPM_PACKAGES[cli]} instalado sem o binário da plataforma. Repare com: {NPM_REPAIR[cli]}")


def install_winget(cli: str) -> None:
    winget = shutil.which("winget")
    if not winget:
        raise CliInstallError("winget não encontrado.")
    result = _run([winget, "install", "--id", WINGET[cli], "-e", "--scope", "user", "--silent",
                   "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
    if not find(cli):
        raise CliInstallError(f"winget install {WINGET[cli]} não deixou o executável: {_tail(result)}")


def steps(cli: str) -> list:
    """[(rótulo, função)] na ordem em que se tenta instalar o CLI."""
    found = []
    if cli in OFFICIAL:
        found.append(("instalador oficial", lambda: install_official(cli)))
    if cli in NPM_PACKAGES:
        found.append(("npm", lambda: install_npm(cli)))
    if cli in WINGET:
        found.append(("winget", lambda: install_winget(cli)))
    return found


def ensure(cli: str, log=print) -> str | None:
    """Garante o CLI; devolve o executável ou None (com o motivo de cada tentativa no log). Nunca pede admin."""
    found = find(cli)
    if found:
        log(f"{NAMES[cli]}: instalado ({found})")
        return found
    errors = []
    order = [("instalador oficial", install_official)] if cli in OFFICIAL else []
    order += [("npm", install_npm)] if cli in NPM_PACKAGES else []
    order += [("winget", install_winget)] if cli in WINGET else []
    for label, step in order:
        try:
            log(f"Instalando {NAMES[cli]} pelo {label}…")
            step(cli)
            found = find(cli)
            if found:
                log(f"{NAMES[cli]} instalado ({found}).")
                return found
            errors.append(f"{label}: terminou sem deixar o executável")
        except (CliInstallError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{label}: {error}")
    for error in errors:
        log(f"  [falhou] {error}")
    log(f"{NAMES[cli]} não foi instalado. Instale manualmente ({MANUAL[cli]}) ou peça à TI; "
        f"depois rode: launcher.py cli --install {cli}")
    return None
