"""Versão instalada, versão publicada e atualização das CLIs de terminal (Claude Code, Codex, Copilot CLI).

O launcher não troca binário: dispara o `update` que a própria CLI já tem (ou o npm de quem a instalou por ele),
sem esperar pelas sessões abertas (a versão nova vale na próxima abertura), e devolve o resultado para a tela mostrar. VS Code e o app do Copilot ficam de fora.
Desligar: preferência "Atualizar as CLIs automaticamente" no menu ou AGENT_LAUNCHER_NO_CLI_UPDATE=1.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from agent_launcher.domain.files import remove_tree
from agent_launcher.harnesses import installers
from agent_launcher.harnesses.installers import NAMES, NPM_PACKAGES, CliInstallError
from agent_launcher.platform import processes
from agent_launcher.update import updater
from version import data_home

DISABLE_ENV = "AGENT_LAUNCHER_NO_CLI_UPDATE"
UPDATE_TIMEOUT = 900


class UpdateDeferred(CliInstallError):
    """A tentativa de atualizar bateu num arquivo preso (sessão aberta no npm, por exemplo). Não é erro: tenta de novo depois."""


def disabled_by_env() -> bool:
    """AGENT_LAUNCHER_NO_CLI_UPDATE ligada (diferente de vazio e de 0): nenhuma atualização automática das CLIs."""
    return os.environ.get(DISABLE_ENV, "").strip() not in ("", "0")


def parse_version(text: str) -> str | None:
    """Primeira versão x.y.z da saída de `--version` ("2.1.311 (Claude Code)", "codex-cli 0.162.1")."""
    found = re.search(r"\d+\.\d+\.\d+", text or "")
    return found.group(0) if found else None


def installed_version(cli: str, path: str | None = None) -> str | None:
    """`<exe> --version` sem janela. Só isto vale: `npm ls -g` mente para a Copilot CLI, que baixa builds próprias."""
    path = path or installers.find(cli)
    if not path:
        return None
    try:
        result = installers._run([path, "--version"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(result.stdout + "\n" + result.stderr) if result.returncode == 0 else None


CLAUDE_CHANNELS = ("latest", "stable")


def release_tag(cli: str, path: str | None = None) -> str:
    """Dist-tag do npm que o comando de atualização instala. O `claude update` nativo segue `autoUpdatesChannel` do
    settings.json do usuário ("latest" por padrão, ou "stable"); nas outras origens o launcher pede a versão exata."""
    path = path or installers.find(cli)
    if cli != "claude" or not path or source(cli, path) == "npm":
        return "latest"
    settings = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
        channel = data.get("autoUpdatesChannel", "latest")
    except (OSError, ValueError, AttributeError) as error:
        raise updater.CheckError(f"Não foi possível ler o canal de atualização do Claude Code em {settings}: {error}") from error
    if channel not in CLAUDE_CHANNELS:
        raise updater.CheckError(f"Canal de atualização do Claude Code desconhecido em {settings}: {channel!r}")
    return channel


def latest_version(cli: str, get=updater._get, registry: str = updater.REGISTRY) -> str:
    """Última versão publicada no registro npm, no canal que o comando de atualização da CLI instala."""
    url = registry + NPM_PACKAGES[cli].replace("/", "%2F") + "/" + release_tag(cli)
    try:
        version = json.loads(get(url, 10, 2 * 1024 * 1024)).get("version")
    except (OSError, ValueError, AttributeError) as error:
        raise updater.CheckError(f"Não foi possível consultar {url}: {error}") from error
    if not (isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise updater.UpdateError(f"Versão publicada de {NAMES[cli]} inválida: {version!r}")
    return version


def source(cli: str, path: str) -> str:
    """Como a CLI foi instalada: "npm", "winget" ou "native" (instalador oficial). Decide o comando de atualização."""
    lowered = str(path).lower()
    if "node_modules" in lowered or Path(lowered).suffix in (".cmd", ".ps1") or Path(lowered).parent.name == "npm":
        return "npm"
    if cli == "copilot" and "winget" in lowered:
        return "winget"
    return "native"


def command(cli: str, path: str, origin: str, version: str = "latest") -> list[str]:
    """Comando de atualização pela origem: `update` da própria CLI (nativa, winget e Copilot em qualquer origem) ou npm
    na versão pedida (a que a verificação mostrou; o @latest do npm pode vir do cache dele, mais velho), no prefixo da
    instalação que `path` abre. Sem esse prefixo o npm troca a instalação do prefixo configurado nele, que pode não ser
    a do PATH: sai com 0 e a CLI que abre continua velha."""
    if cli == "copilot" or origin != "npm":
        return [path, "update"]
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise CliInstallError("npm não encontrado (Node.js não instalado); atualize pelo comando da própria CLI.")
    prefix = installers.npm_prefix(cli, Path(path))
    if prefix is None:
        installers._fail(cli, "atualização", f"{path} é o {cli} que abre primeiro no PATH, mas não tem o pacote "
                                             f"{NPM_PACKAGES[cli]} do npm ao lado (outro gerenciador o instalou); o npm "
                                             "atualizaria outra instalação. Atualize por quem o instalou ou tire essa "
                                             "pasta do PATH.")
    return [npm, "install", "-g", f"{NPM_PACKAGES[cli]}@{version}", "--prefix", str(prefix),
            *installers.npm_flags(cli, npm)]


def installs(cli: str) -> list[str]:
    """Todo executável da CLI no PATH, na ordem em que o Windows procura (o primeiro é o que abre), e a instalação
    nativa por usuário quando está fora do PATH."""
    extensions = [ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower().split(";") if ext]
    found = {}
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        for ext in extensions if folder and not installers._vscode_shim(folder) else ():
            candidate = Path(folder) / f"{cli}{ext}"
            if candidate.is_file():
                found.setdefault(os.path.normcase(os.path.abspath(candidate)), str(candidate))
    for candidate in installers.native_paths(cli):
        if candidate.is_file():
            found.setdefault(os.path.normcase(os.path.abspath(candidate)), str(candidate))
    return list(found.values())


def _package_version(cli: str, path: str) -> str | None:
    """Versão gravada no package.json do pacote do npm em volta de `path` (o que o npm deixou, rode o binário ou não)."""
    prefix = installers.npm_prefix(cli, Path(path))
    if prefix is None:
        return None
    try:
        manifest = json.loads((prefix / "node_modules" / NPM_PACKAGES[cli] / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest.get("version") if isinstance(manifest, dict) else None


def _not_converged(cli: str, path: str, origin: str, after: str, target: str) -> str:
    """Por que a CLI que abre continua abaixo de `target` depois de um update que saiu com 0: o que o npm gravou na
    instalação dela, se o binário dela está em uso e quais outras instalações o PATH tem (com versão e origem)."""
    causes = []
    written = _package_version(cli, path) if origin == "npm" else None
    if written and updater.parts(written) >= updater.parts(target):
        holders = running(cli, path)
        causes.append(f"o npm gravou {written} no pacote, mas o executável continua respondendo {after}"
                      + (f" ({describe_running(holders)} em execução)" if holders else
                         f"; repare com: {installers.npm_repair(cli, Path(path))}"))
    elif written:
        causes.append(f"o pacote do npm ao lado dele continua {written}")
    first = os.path.normcase(os.path.abspath(path))
    others = [other for other in installs(cli) if os.path.normcase(os.path.abspath(other)) != first]
    if others:
        listed = [f"{other} ({installed_version(cli, other) or 'versão ilegível'}, {source(cli, other)})" for other in others]
        causes.append("outras instalações, atrás dele no PATH ou fora dele: " + "; ".join(listed)
                      + ". Deixe uma só, ou ponha a atualizada antes no PATH")
    return ". ".join(causes) or "nenhuma outra instalação no PATH"


def _held(folder: Path) -> bool:
    """Algum arquivo da pasta não abre para escrita: imagem de um processo em execução (o Windows nega) ou arquivo aberto
    sem compartilhar. O caminho da imagem não serve para isso: depois do rename do npm o processo continua informando o
    caminho de antes, que agora é o do pacote novo."""
    for file in folder.rglob("*"):
        try:
            if file.is_file():
                with open(file, "r+b"):
                    pass
        except OSError:
            return True
    return False


def clean_staging(cli: str, path: str) -> None:
    """Apaga as pastas `.<pacote>-XXXX` que o npm deixa ao lado do pacote quando não consegue apagar a versão velha (sessão
    aberta na hora da atualização), menos as que algum processo ainda segura. Só com a instalação íntegra (npm que saiu
    com 0, ou CLI que respondeu a versão): num npm que falhou no meio e não conseguiu desfazer o rename, a pasta pode ser
    a única cópia da versão velha. O resultado vai para cli-install.log."""
    prefix = installers.npm_prefix(cli, Path(path))
    if prefix is None:
        return
    scope, name = NPM_PACKAGES[cli].split("/")
    for folder in sorted((prefix / "node_modules" / scope).glob(f".{name}-*")):
        if not folder.is_dir():
            continue
        if _held(folder):
            installers.record(f"{installers.label(cli)}: {folder} (versão velha deixada pelo npm) segue em uso ou sem "
                              "permissão de escrita; fica para a próxima verificação.")
            continue
        try:
            remove_tree(folder)
            installers.record(f"{installers.label(cli)}: apagada {folder}, versão velha deixada pelo npm.")
        except OSError as error:
            installers.record(f"{installers.label(cli)}: {folder} (versão velha deixada pelo npm) não foi apagada: {error}")


def _executables(cli: str, path: str) -> list[Path]:
    """Os arquivos que o `update` da CLI troca: o .exe do caminho (e o que ele aponta), o binário da plataforma do
    npm e, no Copilot, as builds que ele guarda em ~/.copilot/pkg."""
    found = [Path(path)] if Path(path).suffix.lower() == ".exe" else []
    try:
        extra = (installers.claude_binary(Path(path)) if cli == "claude"
                 else installers.codex_platform_binary(Path(path)) if cli == "codex" else None)
    except CliInstallError:
        extra = None
    if cli == "copilot":
        found += list((Path.home() / ".copilot" / "pkg").glob("**/copilot.exe"))
    return found + ([extra] if extra else [])


def running(cli: str, path: str) -> list[tuple[int, str]]:
    """Processos em execução cujo executável é um dos arquivos que a atualização troca (sessões abertas, por este
    launcher ou por qualquer outro caminho). Só processo de verdade conta: arquivo aberto por antivírus ou por sonda não."""
    return processes.running_from(_executables(cli, path))


def describe_running(found: list[tuple[int, str]]) -> str:
    return ", ".join(f"{Path(image).name} (PID {pid})" for pid, image in found[:3]) + (", …" if len(found) > 3 else "")


# Sinais inequívocos de arquivo preso; "acesso negado" sozinho (prefixo do npm sem permissão, por exemplo) só adia quando
# há processo da CLI de verdade rodando, senão é falha e vai para o log.
LOCKED = re.compile(r"EBUSY|being used by another process|WinError 32\b|resource busy|text file busy|em uso por outro processo",
                    re.IGNORECASE)
DENIED = re.compile(r"EPERM|EACCES|Access is denied|WinError 5\b|acesso negado", re.IGNORECASE)


def lock_path(cli: str) -> Path:
    return data_home() / f"cli-update-{cli}.lock"


def updating(cli: str) -> bool:
    """Há atualização desta CLI em andamento (por esta ou outra janela do launcher): nenhuma sessão dela deve abrir agora."""
    try:
        return time.time() - lock_path(cli).stat().st_mtime <= updater.LOCK_STALE_S
    except OSError:
        return False


def check(cli: str, latest=latest_version) -> dict | None:
    """{installed, latest, newer, source, running, error} ou None quando a CLI não está instalada. `running` conta os
    processos da CLI em execução: a atualização não espera por eles, a versão nova vale na próxima abertura."""
    path = installers.find(cli)
    if not path:
        return None
    entry = {"installed": installed_version(cli, path), "latest": None, "newer": False, "source": source(cli, path),
             "running": 0, "error": None}
    if entry["installed"] and entry["source"] == "npm" and cli != "copilot":
        clean_staging(cli, path)
    try:
        entry["latest"] = latest(cli)
    except (updater.UpdateError, OSError) as error:
        entry["error"] = str(error)
        return entry
    entry["newer"] = bool(entry["installed"]) and updater.parts(entry["latest"]) > updater.parts(entry["installed"])
    if entry["newer"]:
        entry["running"] = len(running(cli, path))
    return entry


def check_all(latest=latest_version) -> dict:
    """Uma CLI que não responde vira `error` na própria entrada, sem esconder as outras."""
    found = {}
    for cli in NAMES:
        try:
            entry = check(cli, latest)
        except (CliInstallError, OSError, subprocess.SubprocessError) as error:
            entry = {"installed": None, "latest": None, "newer": False, "source": None, "running": 0,
                     "error": str(error)}
        if entry:
            found[cli] = entry
    return found


def update(cli: str, latest=latest_version) -> dict:
    """Atualiza uma CLI sem esperar pelas sessões abertas (o Windows deixa renomear um .exe em execução: a sessão segue na
    versão velha e a nova vale na próxima abertura). {cli, before, after, updated, sessions}. Levanta UpdateDeferred só
    quando a tentativa real bate num arquivo preso, e CliInstallError (texto legível, também em cli-install.log) se o
    comando falhar, se terminar sem chegar à versão publicada ou se a CLI não executar depois; no npm volta para a
    versão anterior."""
    path = installers.find(cli)
    if not path:
        raise CliInstallError(f"{NAMES[cli]} não está instalado.")
    before = installed_version(cli, path)
    target = latest(cli)
    if before and updater.parts(target) <= updater.parts(before):
        return {"cli": cli, "before": before, "after": before, "updated": False}
    lock = lock_path(cli)
    if not updater._lock(lock):
        raise UpdateDeferred(f"Atualização do {NAMES[cli]} adiada: outra janela do launcher já está atualizando.")
    taken = lock.stat().st_mtime_ns
    try:
        return _apply(cli, path, before, target, len(running(cli, path)))
    finally:
        try:
            # Passada a validade (npm + desfazer podem levar mais), outro processo já pode ter tomado a trava: a dele fica.
            if lock.exists() and lock.stat().st_mtime_ns == taken:
                lock.unlink(missing_ok=True)
        except PermissionError:
            installers.record(f"trava {lock} não removida (arquivo em uso); vale como velha depois de "
                              f"{updater.LOCK_STALE_S // 60} min e nesse tempo as sessões de {NAMES[cli]} não abrem")


def _run_logged(cli: str, command: list[str]):
    """installers._run com timeout e erro de sistema registrados em cli-install.log (como as falhas de saída)."""
    try:
        return installers._run(command, timeout=UPDATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        installers._fail(cli, "atualização", f"a atualização do {NAMES[cli]} passou de {UPDATE_TIMEOUT // 60} min e foi encerrada.")
    except (OSError, subprocess.SubprocessError) as error:
        installers._fail(cli, "atualização", f"a atualização do {NAMES[cli]} não iniciou: {error}")


def _signed_targets(cli: str, path: str) -> list[Path]:
    """Binários que o `update` nativo deixou: o .exe do caminho e, no Copilot, o runtime da build mais nova de ~/.copilot/pkg."""
    found = [Path(path)] if Path(path).suffix.lower() == ".exe" and Path(path).is_file() else []
    if cli == "copilot":
        builds = [item for item in (Path.home() / ".copilot" / "pkg" / "win32-x64").glob("*")
                  if re.fullmatch(r"\d+(\.\d+)+", item.name)]
        if builds:
            newest = max(builds, key=lambda item: tuple(int(part) for part in item.name.split(".")))
            found += list(newest.glob("prebuilds/win32-x64/copilot-runtime.exe"))
    return found + (installers.official_auxiliary_executables(cli) if found else [])


def _verify_signature(cli: str, path: str, origin: str) -> None:
    """Assinatura Authenticode do fabricante nos binários que ficaram depois do `update` nativo (como install_official).
    Sem assinatura válida o executável é apagado e a atualização recusada; nunca roda um binário não conferido."""
    targets = _signed_targets(cli, path)
    if not targets:
        if origin != "npm":
            installers._fail(cli, "atualização", f"não achei o executável do {NAMES[cli]} ({path}) para conferir a assinatura "
                                                 "depois da atualização; reinstale pelo launcher.")
        return
    installers.reject_unsigned(cli, targets, "atualização", "atualização", " depois da atualização")


def _apply(cli: str, path: str, before: str | None, target: str, sessions: int = 0) -> dict:
    origin = source(cli, path)
    result = _run_logged(cli, command(cli, path, origin, target))
    if origin == "npm" and cli != "copilot" and not result.returncode:
        clean_staging(cli, path)
    output = result.stdout + "\n" + result.stderr
    holders = running(cli, path) if result.returncode and (LOCKED.search(output) or DENIED.search(output)) else []
    if result.returncode and (holders or LOCKED.search(output)):
        raise UpdateDeferred(f"Atualização do {NAMES[cli]} adiada: "
                             + (f"{describe_running(holders)} está rodando e o arquivo está preso" if holders else
                                "um arquivo da CLI está preso por outro processo")
                             + ". Tento de novo na próxima verificação.")
    if result.returncode:
        installers._fail(cli, "atualização", f"atualização do {NAMES[cli]} ({origin}) falhou: {installers._tail(result)}")
    path = installers.find(cli) or path
    if origin != "npm" or cli == "copilot":  # o `update` nativo troca o binário sem passar pelo npm: confere antes de executá-lo
        _verify_signature(cli, path, origin)
    if origin == "npm" and cli != "copilot":
        try:
            if cli == "claude":
                installers.claude_binary(Path(path))  # repara o stub sem cabeçalho MZ que o npm pode deixar
        except CliInstallError as error:
            installers._fail(cli, "atualização", str(error))
        if cli == "codex" and installers.codex_platform_binary(Path(path)) is None and Path(path).suffix.lower() != ".exe":
            installers._fail(cli, "atualização", f"{NPM_PACKAGES[cli]} atualizado sem o binário da plataforma. "
                                                 f"Repare com: {installers.npm_repair(cli, Path(path))}")
    after = installed_version(cli, path)
    if after is None:
        if origin == "npm" and before and cli != "copilot":
            back = _run_logged(cli, command(cli, path, origin, before))
            if not back.returncode:
                clean_staging(cli, path)
            installers._fail(cli, "atualização", f"o {NAMES[cli]} não executa depois da atualização; "
                                                 f"voltei para {before} ({'ok' if not back.returncode else 'falhou'}).")
        installers._fail(cli, "atualização", f"o {NAMES[cli]} não executa depois da atualização ({origin}); "
                                             "sem volta automática para esta origem: reinstale pelo launcher.")
    if updater.parts(after) < updater.parts(target):
        installers._fail(cli, "atualização", f"a atualização do {NAMES[cli]} ({origin}) terminou sem erro, mas o {cli} "
                                             f"que o launcher abre ({path}) continua {after} e a publicada é {target}. "
                                             f"{_not_converged(cli, path, origin, after, target)}. "
                                             f"Saída do comando: {installers._tail(result)}")
    return {"cli": cli, "before": before, "after": after, "updated": after != before, "sessions": sessions}
