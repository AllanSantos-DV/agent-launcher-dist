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

from agent_launcher.harnesses import installers
from agent_launcher.harnesses.installers import NAMES, NPM_FLAGS, NPM_PACKAGES, CliInstallError
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


def latest_version(cli: str, get=updater._get, registry: str = updater.REGISTRY) -> str:
    """Última versão publicada no registro npm (o mesmo que o launcher usa para se atualizar)."""
    url = registry + NPM_PACKAGES[cli].replace("/", "%2F") + "/latest"
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


def command(cli: str, path: str, origin: str) -> list[str]:
    """Comando de atualização pela origem: `update` da própria CLI (nativa, winget e Copilot em qualquer origem) ou npm @latest."""
    if cli == "copilot" or origin != "npm":
        return [path, "update"]
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise CliInstallError("npm não encontrado (Node.js não instalado); atualize pelo comando da própria CLI.")
    return [npm, "install", "-g", f"{NPM_PACKAGES[cli]}@latest", *NPM_FLAGS.get(cli, [])]


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
    comando falhar ou a CLI não executar depois; no npm volta para a versão anterior."""
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
        return _apply(cli, path, before, len(running(cli, path)))
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


def _apply(cli: str, path: str, before: str | None, sessions: int = 0) -> dict:
    origin = source(cli, path)
    result = _run_logged(cli, command(cli, path, origin))
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
                                                 f"Repare com: {installers.NPM_REPAIR[cli]}")
    after = installed_version(cli, path)
    if after is None:
        if origin == "npm" and before and cli != "copilot":
            back = _run_logged(cli, [shutil.which("npm.cmd") or shutil.which("npm") or "npm", "install", "-g",
                                     f"{NPM_PACKAGES[cli]}@{before}", *NPM_FLAGS.get(cli, [])])
            installers._fail(cli, "atualização", f"o {NAMES[cli]} não executa depois da atualização; "
                                                 f"voltei para {before} ({'ok' if not back.returncode else 'falhou'}).")
        installers._fail(cli, "atualização", f"o {NAMES[cli]} não executa depois da atualização ({origin}); "
                                             "sem volta automática para esta origem: reinstale pelo launcher.")
    return {"cli": cli, "before": before, "after": after, "updated": after != before, "sessions": sessions}
