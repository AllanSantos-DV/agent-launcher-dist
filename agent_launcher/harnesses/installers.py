"""Ferramentas (CLIs de terminal, VS Code, Copilot app): onde ficam as instalações por usuário e como instalá-las sem admin.

Ordem (nenhuma exige admin):
  1. Instalador oficial do fabricante, por usuário; o script confere o SHA-256 do binário e o launcher confere de novo a
     assinatura Authenticode do executável que ficou (SIGNERS), apagando o que não for do fabricante:
     Claude Code: https://claude.ai/install.ps1 -> ~/.local/bin/claude.exe (code.claude.com/docs/en/setup)
     Codex: https://chatgpt.com/codex/install.ps1 -> %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe
  2. npm install -g (quem instalou o launcher pelo npm tem Node; o pacote do Claude exige Node 22+).
  3. winget, por usuário, para quem o publica (GitHub Copilot CLI: GitHub.Copilot).
Os instaladores oficiais acrescentam a pasta ao PATH do usuário, mas o processo atual não vê a mudança: por isso
native_paths() é consultado além do PATH.

VS Code e GitHub Copilot app (DOWNLOADS): o launcher baixa o instalador oficial por usuário (progresso real do download),
confere o SHA-256 publicado pelo fabricante e a assinatura Authenticode, e roda em silêncio; winget por usuário depois.
  VS Code: update.code.visualstudio.com/api/update/win32-x64-user/stable/latest (URL e sha256hash), Inno Setup
  /VERYSILENT /NORESTART /MERGETASKS=!runcode -> %LOCALAPPDATA%\\Programs\\Microsoft VS Code
  Copilot app: api.github.com/repos/github/app/releases/latest (asset GitHub-Copilot-windows-x64-setup.exe e o digest
  sha256 do asset), NSIS /S -> %LOCALAPPDATA%\\Programs\\GitHub Copilot
Nada é instalado sem pedido: só pelo cartão da ferramenta, por cli --install ou por install.py --with-clis.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from agent_launcher.domain.downloads import restricted_redirects
from agent_launcher.domain.files import append_capped, replace_retrying
from agent_launcher.platform import terminal
from version import data_home

NAMES = {"claude": "Claude Code", "codex": "Codex", "copilot": "GitHub Copilot CLI"}
OFFICIAL = {"claude": "https://claude.ai/install.ps1", "codex": "https://chatgpt.com/codex/install.ps1"}
# Hosts por onde o script oficial pode redirecionar (medido: claude.ai e chatgpt.com respondem 302 para o segundo host).
OFFICIAL_HOSTS = {"claude": ("downloads.claude.ai",), "codex": ("releases.openai.com",)}
# Início do Subject do certificado Authenticode dos binários nativos de cada fabricante (lidos dos pacotes oficiais).
SIGNERS = {"claude": 'CN="Anthropic, PBC"', "codex": 'CN="OpenAI OpCo, LLC"',
           "copilot": 'CN="GitHub, Inc."', "vscode": "CN=Microsoft Corporation", "copilot_app": 'CN="GitHub, Inc."'}
NPM_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "copilot": "@github/copilot"}
NODE_MINIMUM = {"claude": 22, "codex": 18, "copilot": 22}
WINGET = {"copilot": "GitHub.Copilot", "vscode": "Microsoft.VisualStudioCode", "copilot_app": "GitHub.CopilotApp"}
MANUAL = {"claude": OFFICIAL["claude"], "codex": OFFICIAL["codex"], "copilot": "https://github.com/github/copilot-cli",
          "vscode": "https://code.visualstudio.com/download", "copilot_app": "https://github.com/github/app/releases/latest"}
# Instaladores por usuário que o próprio launcher baixa: (onde perguntar a versão corrente, hosts do download, argumentos).
DOWNLOADS = {
    "vscode": ("https://update.code.visualstudio.com/api/update/win32-{arch}-user/stable/latest",
               ("update.code.visualstudio.com", "vscode.download.prss.microsoft.com"),
               ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/MERGETASKS=!runcode"]),
    "copilot_app": ("https://api.github.com/repos/github/app/releases/latest",
                    ("api.github.com", "github.com", "objects.githubusercontent.com",
                     "release-assets.githubusercontent.com"),
                    ["/S"]),
}
DOWNLOAD_LIMIT = 600 * 1024 * 1024  # instaladores de ~320 MB (Copilot app) e ~260 MB (VS Code)
# O npm 11 não roda o postinstall de dependências sem --allow-scripts: o Claude Code deixa bin/claude.exe como script de
# texto e o Windows recusa abri-lo. O Codex não tem postinstall; o binário vem do pacote opcional da plataforma.
NPM_FLAGS = {"claude": ["--include=optional", "--allow-scripts=@anthropic-ai/claude-code"], "codex": ["--include=optional"]}
NPM_REPAIR = {cli: f"npm install -g {NPM_PACKAGES[cli]}@latest {' '.join(flags)}" for cli, flags in NPM_FLAGS.items()}
# npm 11.16.0 (27/05/2026) criou o allow-scripts; o npm anterior avisa "Unknown cli config" e roda os scripts mesmo assim.
ALLOW_SCRIPTS_SINCE = (11, 16, 0)
CLAUDE_PLATFORM = Path("node_modules") / "@anthropic-ai" / "claude-code-win32-x64" / "claude.exe"
CODEX_PLATFORM = (Path("node_modules") / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
LOG_FILE = data_home() / "cli-install.log"
LOG_MAX_BYTES = 1_000_000  # passando disso, fica a metade mais recente


class CliInstallError(Exception):
    pass


def _harness(tool: str):
    from agent_launcher.harnesses import registry
    return registry.get(tool)


def label(tool: str) -> str:
    return NAMES.get(tool) or _harness(tool).LABEL


def locate(tool: str) -> str | None:
    """Executável da ferramenta: os CLIs por `find` (que os testes substituem); os outros pelo próprio adaptador."""
    return find(tool) if tool in NAMES else _harness(tool).find_executable()


def installable() -> tuple[str, ...]:
    """IDs das ferramentas que o launcher sabe instalar, na ordem do registro."""
    from agent_launcher.harnesses import registry
    return tuple(harness.ID for harness in registry.all() if harness.install_steps())


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


def _vscode_shim(entry: str) -> bool:
    """Pasta `copilot-shim` do VS Code (estável ou Insiders) no PATH: o executável dela é do VS Code (assinado pela Microsoft), não o Copilot CLI."""
    parts = [part.lower() for part in re.split(r"[\\/]+", entry.strip().strip('"'))]
    return "copilot-shim" in parts and any(part.startswith("microsoft vs code") for part in parts)


def _path_without_vscode_shim() -> str:
    return os.pathsep.join(entry for entry in os.environ.get("PATH", "").split(os.pathsep) if not _vscode_shim(entry))


def _inside_profile(path: Path) -> bool:
    profile = os.path.normcase(os.path.realpath(os.environ.get("USERPROFILE") or Path.home()))
    return os.path.normcase(os.path.realpath(path)).startswith(profile + os.sep)


def find(cli: str) -> str | None:
    """Executável do CLI: o do PATH (sem o atalho `copilot-shim` do VS Code), senão o da instalação nativa por usuário."""
    found = shutil.which(cli, path=_path_without_vscode_shim())
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
        append_capped(LOG_FILE, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n", LOG_MAX_BYTES)
    except OSError:
        pass  # o texto segue na exceção e no log da tela; o arquivo é só o registro


def _fail(cli: str, step: str, message: str):
    """Registra a falha em LOG_FILE (o texto também sobe na exceção) e a levanta."""
    record(f"{label(cli)} ({step}): {message}")
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
                              f"Repare com: {npm_repair('claude', path)}")
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


def npm_prefix(cli: str, path: Path) -> Path | None:
    """Prefixo global do npm que contém o pacote da CLI aberta por `path` (o shim do PATH ou o próprio bin/), ou None sem
    pacote do npm em volta. `npm install -g --prefix` nele troca exatamente essa instalação, qualquer que seja o prefixo
    configurado no npm que está no PATH."""
    return next((root.parents[2] for root in _package_roots(path, NPM_PACKAGES[cli])
                 if root.parents[1].name.lower() == "node_modules"), None)


def npm_repair(cli: str, path: Path) -> str:
    """Comando de reparo mostrado ao usuário, com o `--prefix` da instalação que `path` abre quando ela é do npm: sem ele
    o npm reinstalaria no prefixo configurado, que pode ser outra instalação."""
    prefix = npm_prefix(cli, path)
    return NPM_REPAIR[cli] + (f' --prefix "{prefix}"' if prefix else "")


def npm_version(npm: str) -> tuple[int, int, int]:
    """Versão do npm (`npm --version`); erro com a saída quando ele não responde com X.Y.Z."""
    result = _run([npm, "--version"], timeout=60)
    found = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\S*\s*", result.stdout)
    if result.returncode or not found:
        raise CliInstallError(f"{npm} --version não informou a versão do npm: {_tail(result)}")
    return tuple(int(part) for part in found.groups())


def npm_flags(cli: str, npm: str) -> list[str]:
    """NPM_FLAGS da CLI sem o --allow-scripts quando o npm é anterior a ALLOW_SCRIPTS_SINCE (lá ele não existe)."""
    flags = NPM_FLAGS.get(cli, [])
    if any(flag.startswith("--allow-scripts") for flag in flags) and npm_version(npm) < ALLOW_SCRIPTS_SINCE:
        return [flag for flag in flags if not flag.startswith("--allow-scripts")]
    return list(flags)


def codex_platform_binary(path: Path) -> Path | None:
    """Codex.exe do pacote da plataforma instalado pelo npm (None se o pacote opcional não veio)."""
    return next((root / CODEX_PLATFORM for root in _package_roots(path, "@openai/codex")
                 if is_native(root / CODEX_PLATFORM)), None)


class SignatureUnchecked(Exception):
    """O PowerShell não conseguiu conferir a assinatura (travou, não iniciou ou falhou): diferente de "sem assinatura"."""


def _signed(path: Path, signer: str) -> bool:
    try:
        return terminal.signature_ok(path, signer)
    except (OSError, subprocess.SubprocessError, terminal.TerminalError) as error:
        raise SignatureUnchecked(str(error)) from error


def reject_unsigned(cli: str, paths, step: str, noun: str, after: str = "") -> None:
    """Todo executável de `paths` precisa da assinatura do fabricante. O que não tem, e o que não deu para conferir, é
    apagado (fica onde `find` o acharia) e o passo `step` é recusado com a causa certa; nunca roda um binário não conferido.
    Fora do perfil do usuário nada é apagado (não foi o launcher que instalou): o executável é só recusado."""
    unsigned, unchecked = [], {}
    for path in dict.fromkeys(paths):
        try:
            if not _signed(path, SIGNERS[cli]):
                unsigned.append(path)
        except SignatureUnchecked as error:
            unchecked[path] = error
    foreign = [path for path in [*unsigned, *unchecked] if not _inside_profile(path)]
    unsigned = [path for path in unsigned if path not in foreign]
    unchecked = {path: error for path, error in unchecked.items() if path not in foreign}
    for path in [*unsigned, *unchecked]:
        try:
            path.unlink()
        except OSError as error:
            _fail(cli, step, f"{path} sem assinatura válida de {SIGNERS[cli]} e não pôde ser apagado ({error}); "
                             "apague-o à mão.")
    if foreign:
        _fail(cli, step, f"{', '.join(map(str, foreign))} sem assinatura de {SIGNERS[cli]} e fora do perfil do usuário: "
                         f"não foi instalado pelo launcher e não foi apagado; a {noun} foi recusada.")
    if unsigned:
        _fail(cli, step, f"{', '.join(map(str, unsigned))} sem assinatura Authenticode válida de {SIGNERS[cli]}{after}; "
                         f"o executável foi apagado e a {noun} recusada.")
    if unchecked:
        _fail(cli, step, f"não foi possível conferir a assinatura de {', '.join(map(str, unchecked))}{after}: "
                         f"{next(iter(unchecked.values()))}; o executável foi apagado e a {noun} recusada.")


def official_auxiliary_executables(cli: str) -> list[Path]:
    """Executáveis que o instalador oficial do Codex deixa ao lado do codex.exe na release corrente (command-runner,
    sandbox, hosts), todos assinados pela OpenAI; o rg.exe é do ripgrep e vem sem assinatura da OpenAI."""
    if cli != "codex":
        return []
    home = Path(os.environ.get("CODEX_HOME") or Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex")
    try:
        release = (home / "packages" / "standalone" / "current").resolve(strict=True)
    except OSError:
        return []
    return sorted(path for path in release.rglob("*.exe") if path.name.lower() != "rg.exe")


def _resolve_official(cli: str) -> str:
    """URL final do script oficial, conferida: só o host dele e OFFICIAL_HOSTS; o PowerShell baixa exatamente essa URL."""
    import urllib.parse
    import urllib.request
    url = OFFICIAL[cli]
    host = urllib.parse.urlsplit(url).hostname
    opener = urllib.request.build_opener(restricted_redirects(url, CliInstallError, (host, *OFFICIAL_HOSTS[cli])))
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "agent-launcher"}), timeout=30) as response:
            final = response.geturl()
    except OSError as error:
        raise CliInstallError(f"não foi possível consultar o instalador oficial ({url}): {error}") from error
    if urllib.parse.urlsplit(final).hostname not in (host, *OFFICIAL_HOSTS[cli]) or not final.startswith("https://"):
        raise CliInstallError(f"o instalador oficial ({url}) resolveu para {final}, fora dos hosts esperados; recusado.")
    return final


def install_official(cli: str) -> None:
    # O script do fabricante roda como texto (irm | iex); -ExecutionPolicy Bypass só vale onde a política da empresa
    # não está fixada por GPO, e o erro volta legível nesse caso.
    script = f"$ProgressPreference = 'SilentlyContinue'; irm '{_resolve_official(cli)}' -MaximumRedirection 0 | iex"
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                  timeout=900,
                  environment={"CODEX_NON_INTERACTIVE": "1", "PSModulePath": terminal.powershell_env()["PSModulePath"]})
    installed = [path for path in native_paths(cli) if path.is_file()]
    if not installed:
        _fail(cli, "instalador oficial", f"instalador oficial ({OFFICIAL[cli]}) não deixou o executável: {_tail(result)}")
    reject_unsigned(cli, [*installed, *official_auxiliary_executables(cli)], "instalador oficial", "instalação")


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
    result = _run([npm, "install", "-g", NPM_PACKAGES[cli], *npm_flags(cli, npm)], timeout=900)
    installed = find(cli)
    if result.returncode:
        _fail(cli, "npm", f"npm install -g {NPM_PACKAGES[cli]} falhou: {_tail(result)}")
    if not installed:
        prefix = _run([npm, "prefix", "-g"], timeout=60)
        where = prefix.stdout.strip() if not prefix.returncode and prefix.stdout.strip() else \
            f"um prefixo que `npm prefix -g` não informou ({_tail(prefix)})"
        _fail(cli, "npm", f"npm install -g {NPM_PACKAGES[cli]} terminou sem erro, mas o {cli} não está no PATH: o npm "
                          f"instalou em {where}. Ponha essa pasta no PATH e abra o launcher de novo.")
    try:
        usable = claude_binary(Path(installed)) if cli == "claude" else codex_platform_binary(Path(installed))
    except CliInstallError as error:
        _fail(cli, "npm", str(error))
    if usable is None and cli == "codex" and Path(installed).suffix.lower() != ".exe":
        _fail(cli, "npm", f"{NPM_PACKAGES[cli]} instalado sem o binário da plataforma. Repare com: {npm_repair(cli, Path(installed))}")


def install_winget(cli: str) -> None:
    winget = shutil.which("winget")
    if not winget:
        raise CliInstallError("winget não encontrado.")
    result = _run([winget, "install", "--id", WINGET[cli], "-e", "--scope", "user", "--silent",
                   "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
    if not locate(cli):
        raise CliInstallError(f"winget install {WINGET[cli]} não deixou o executável: {_tail(result)}")


def _open(url: str, hosts: tuple[str, ...], timeout: int = 60):
    import urllib.request
    opener = urllib.request.build_opener(restricted_redirects(url, CliInstallError, hosts))
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "agent-launcher"}), timeout=timeout)


def release_asset(tool: str) -> tuple[str, str, int]:
    """(URL, SHA-256, tamanho em bytes ou 0) do instalador oficial corrente para esta arquitetura."""
    query, hosts, _ = DOWNLOADS[tool]
    arm = platform.machine().lower() in ("arm64", "aarch64")
    try:
        with _open(query.format(arch="arm64" if arm else "x64"), hosts, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise CliInstallError(f"não foi possível consultar a versão corrente do {label(tool)}: {error}") from error
    if tool == "vscode":
        url, sha256, size = release.get("url"), release.get("sha256hash"), 0
    else:
        name = f"GitHub-Copilot-windows-{'arm64' if arm else 'x64'}-setup.exe"
        asset = next((item for item in release.get("assets") or [] if item.get("name") == name), {})
        url, size = asset.get("browser_download_url"), int(asset.get("size") or 0)
        sha256 = (asset.get("digest") or "").removeprefix("sha256:")
    if not url or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise CliInstallError(f"a versão corrente do {label(tool)} veio sem instalador ou sem SHA-256 publicado; recusado.")
    return url, sha256.lower(), size


def download(tool: str, url: str, destination: Path, expected_sha256: str, size: int, progress) -> None:
    """Baixa o instalador para `destination` relatando a fração baixada; só segue redirecionamento para DOWNLOADS."""
    digest, received = hashlib.sha256(), 0
    try:
        with _open(url, DOWNLOADS[tool][1], timeout=120) as response, open(destination, "wb") as stream:
            total = int(response.headers.get("Content-Length") or 0) or size
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > DOWNLOAD_LIMIT:
                    raise CliInstallError(f"download do {label(tool)} acima do limite esperado; interrompido.")
                digest.update(chunk)
                stream.write(chunk)
                if total:
                    progress(min(received / total, 1.0), f"Baixando {min(received * 100 // total, 100)}%")
    except OSError as error:
        raise CliInstallError(f"download do {label(tool)} falhou ({url}): {error}") from error
    if digest.hexdigest() != expected_sha256:
        raise CliInstallError(f"SHA-256 do instalador do {label(tool)} não confere com o publicado; arquivo descartado.")


def install_download(tool: str, progress=lambda fraction, text: None) -> None:
    """Instalador oficial por usuário baixado pelo launcher: SHA-256 e assinatura conferidos antes de rodar, em silêncio."""
    url, sha256, size = release_asset(tool)
    with tempfile.TemporaryDirectory(prefix="agent-launcher-instalador-") as work:
        setup = Path(work) / url.rsplit("/", 1)[-1].split("?")[0]
        download(tool, url, setup, sha256, size, progress)
        progress(None, "Conferindo assinatura")
        try:
            signed = _signed(setup, SIGNERS[tool])
        except SignatureUnchecked as error:
            _fail(tool, "instalador oficial", f"não foi possível conferir a assinatura do instalador: {error}; recusado.")
        if not signed:
            _fail(tool, "instalador oficial", f"instalador sem assinatura Authenticode válida de {SIGNERS[tool]}; recusado.")
        progress(None, "Instalando")
        result = _run([str(setup), *DOWNLOADS[tool][2]], timeout=900)
    if result.returncode:
        _fail(tool, "instalador oficial", f"o instalador do {label(tool)} terminou com erro: {_tail(result)}")


def steps(tool: str) -> list:
    """[(rótulo, função(progress))] na ordem em que se tenta instalar a ferramenta. `progress(fração ou None, texto)`:
    None = sem como medir (o instalador do fabricante roda em silêncio)."""
    found = []
    if tool in OFFICIAL:
        found.append(("instalador oficial", lambda progress: install_official(tool)))
    if tool in DOWNLOADS:
        found.append(("instalador oficial", lambda progress: install_download(tool, progress)))
    if tool in NPM_PACKAGES:
        found.append(("npm", lambda progress: install_npm(tool)))
    if tool in WINGET:
        found.append(("winget", lambda progress: install_winget(tool)))
    return found


def ensure(tool: str, log=print, progress=None) -> str | None:
    """Garante a ferramenta; devolve o executável ou None (com o motivo de cada tentativa no log). Nunca pede admin.
    `progress(fração ou None, texto)` acompanha a instalação; 1.0 só depois de o executável aparecer."""
    report = progress or (lambda fraction, text: None)
    found = locate(tool)
    if found:
        log(f"{label(tool)}: instalado ({found})")
        return found
    errors = []
    for step_label, step in (steps(tool) if tool in NAMES else _harness(tool).install_steps()):
        try:
            log(f"Instalando {label(tool)} pelo {step_label}…")
            report(None, "Instalando")
            step(report)
            found = locate(tool)
            if found:
                report(1.0, "Instalado")
                log(f"{label(tool)} instalado ({found}).")
                return found
            errors.append(f"{step_label}: terminou sem deixar o executável")
        except (CliInstallError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{step_label}: {error}")
    for error in errors:
        log(f"  [falhou] {error}")
    log(f"{label(tool)} não foi instalado. Instale manualmente ({MANUAL.get(tool) or _harness(tool).MISSING_HINT}) "
        f"ou peça à TI; depois rode: launcher.py cli --install {tool}")
    return None
