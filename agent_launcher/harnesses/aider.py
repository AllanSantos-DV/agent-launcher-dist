"""Aider: agente de código que edita por texto, sem chamada de ferramenta nativa, e por isso funciona com modelo pequeno e
local. Instalado num ambiente virtual próprio (uv e Python 3.12; o Aider exige Python abaixo de 3.13).

Medido com qwen3:4b num Ollama local (RTX 5060 de 8 GB), 3 tarefas pequenas: o Aider fez 3 de 3 e enviou cerca de mil
tokens por tarefa; o OpenCode fez 1 de 3 com cerca de 33 mil. Por isso o Aider entra e o OpenCode não.

Conexão `ollama`: rota nativa (`ollama_chat/`), com a janela de contexto pedida em `num_ctx` (sem ela o Ollama usa a
dele, bem menor); nunca vai pelo endpoint local, que só serve `/v1/*`. Qualquer outra conexão que sirva Chat Completions:
rota OpenAI-compatível (`openai/`), que a preferência da conexão pode levar ao endpoint local.
O Aider não guarda conversas por sessão que o launcher liste; ele grava `.aider.*` na pasta do projeto, e retomar é
`--restore-chat-history` sobre o `.aider.chat.history.md` dessa pasta.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

from agent_launcher.connectors._http import NO_KEY
from agent_launcher.domain.catalog import Catalog, ModelInfo, protocol_for
from agent_launcher.harnesses import installers
from agent_launcher.harnesses.base import Harness, is_exe
from agent_launcher.session.plan import LaunchPlan
from version import data_home

AIDER_VERSION = "0.86.2"
PYTHON_VERSION = "3.12"
OLLAMA_CONTEXT = 16384
OLLAMA_PREFIX = "ollama_chat/"
OPENAI_PREFIX = "openai/"
UV_WINGET_ID = "astral-sh.uv"
SETTINGS_FILE = "model-settings.yml"
HISTORY_FILE = ".aider.chat.history.md"


def tools_home() -> Path:
    """Pasta do ambiente virtual do Aider, dentro da pasta de dados do launcher."""
    return data_home() / "tools" / "aider"


def venv_executable() -> Path:
    return tools_home() / "Scripts" / "aider.exe"


def find_uv() -> str | None:
    """O `uv` do PATH ou de uma das pastas por usuário onde o instalador e o winget o deixam."""
    found = shutil.which("uv")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    return next((str(path) for path in (home / ".local" / "bin" / "uv.exe",
                                        local / "Microsoft" / "WinGet" / "Links" / "uv.exe") if path.is_file()), None)


def _install_uv() -> str:
    winget = shutil.which("winget")
    if not winget:
        raise installers.CliInstallError(f"uv não encontrado e o winget também não: instale o uv (winget install --id "
                                         f"{UV_WINGET_ID}) e repita.")
    result = installers._run([winget, "install", "--id", UV_WINGET_ID, "-e", "--scope", "user", "--silent",
                              "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
    found = find_uv()
    if not found:
        raise installers.CliInstallError(f"winget install {UV_WINGET_ID} não deixou o uv: {installers._tail(result)}")
    return found


def install_aider(progress) -> None:
    """Cria o ambiente virtual próprio com o Python 3.12 (o uv baixa o interpretador se faltar) e instala a versão
    fixada do Aider; confere a versão que ficou."""
    uv = find_uv() or _install_uv()
    venv = tools_home()
    progress(None, "Criando o ambiente do Aider")
    result = installers._run([uv, "venv", "--clear", "--python", PYTHON_VERSION, str(venv)], timeout=900)
    if result.returncode:
        installers._fail("aider", "uv", f"uv venv com o Python {PYTHON_VERSION} falhou: {installers._tail(result)}")
    progress(None, f"Instalando o Aider {AIDER_VERSION}")
    result = installers._run([uv, "pip", "install", "--python", str(venv / "Scripts" / "python.exe"),
                              f"aider-chat=={AIDER_VERSION}"], timeout=1800)
    if result.returncode:
        installers._fail("aider", "uv", f"uv pip install aider-chat=={AIDER_VERSION} falhou: {installers._tail(result)}")
    result = installers._run([str(venv_executable()), "--version"], timeout=120)
    if result.returncode or AIDER_VERSION not in result.stdout:
        installers._fail("aider", "uv", f"o Aider instalado não respondeu a versão {AIDER_VERSION}: {installers._tail(result)}")


def _git_root(directory: Path) -> Path:
    """Onde o Aider grava o histórico: a raiz do repositório git que contém a pasta, ou a própria pasta fora de um."""
    return next((parent for parent in (directory, *directory.parents) if (parent / ".git").exists()), directory)


def _flag(command: list[str], name: str) -> str:
    return command[command.index(name) + 1] if name in command else ""


class Aider(Harness):
    ID = "aider"
    LABEL = "Aider"
    KIND = "terminal"
    CONSUMES = ("chat_completions",)
    CAPABILITIES = frozenset({"resume", "bypass"})
    SUBTITLE = "Agente local por edição em texto"
    SYMBOL = "terminal"
    TAB_COLOR = "#14B814"
    BYPASS_RISK = "Aceita sozinho toda alteração de arquivo e todo comando que o Aider propõe (--yes-always)."
    BYPASS_HINT = "Sem pedir confirmação"
    EXECUTABLE = "aider"
    ENDPOINT_SESSIONS = True
    SCRUB_PREFIXES = ("AIDER_", "OLLAMA_")
    MISSING_HINT = f"uv venv --python {PYTHON_VERSION} e uv pip install aider-chat=={AIDER_VERSION}"

    def find_executable(self) -> str | None:
        own = venv_executable()
        return str(own) if own.is_file() else shutil.which(self.EXECUTABLE)

    def install_steps(self) -> list:
        return [(f"uv e Python {PYTHON_VERSION}", install_aider)]

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        """Modelo de texto que o host serve por Chat Completions; o Aider edita em texto e não exige ferramentas, então
        `tools=False` não o tira da lista (embedding, imagem e áudio continuam fora)."""
        return replace(record, tools=None).chat and protocol_for(record, catalog.protocols, catalog.any_model, self.CONSUMES) is not None

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        return "", "Edição em texto, sem chamada de ferramenta nativa: serve a modelos pequenos e locais."

    def command_for(self, path: Path, repair: bool = True) -> list[str]:
        if not is_exe(path):
            raise installers.CliInstallError(f"{path} não é o executável do Aider.")
        return [str(path)]

    def plan(self, context) -> LaunchPlan:
        gateway, model = context.gateway, context.model
        url = gateway.protocols["chat_completions"]
        if gateway.connection["connector"] == "ollama":
            overrides = {"OLLAMA_API_BASE": url.removesuffix("/v1")}
            model_name = OLLAMA_PREFIX + model
        else:
            key = gateway.key or NO_KEY
            if context.endpoint:
                from agent_launcher.endpoint.registry import PLACEHOLDER_KEY
                key, url = PLACEHOLDER_KEY, context.endpoint.url + "/v1"
            overrides = {"OPENAI_API_BASE": url, "OPENAI_API_KEY": key}
            model_name = OPENAI_PREFIX + model
        command = [*context.command, "--model", model_name, "--no-analytics", "--no-check-update",
                   "--no-show-model-warnings"]
        for name, value in overrides.items():
            if name.endswith("_API_BASE"):
                command += ["--set-env", f"{name}={value}"]
        if context.bypass_permissions:
            command.append("--yes-always")
        if context.resume:
            command.append("--restore-chat-history")
        return LaunchPlan(self.ID, model, context.directory, command, context.environment, overrides, context.input_limit,
                          None, None, {}, context.warnings, False, gateway.name,
                          () if context.endpoint else ("OPENAI_API_KEY",))

    def endpoint_serves(self, gateway) -> bool:
        """O Ollama usa a rota nativa `/api/chat`, que o endpoint não serve (só `/v1/*`): fica direto."""
        return gateway.connection["connector"] != "ollama"

    def prepare(self, plan) -> None:
        self.ensure_endpoint(plan)

    def has_previous_conversation(self, directory: Path) -> bool:
        return (_git_root(directory) / HISTORY_FILE).is_file()

    def session_files_key(self, plan) -> object | None:
        """Só a rota do Ollama grava arquivo: os ajustes do modelo, que dependem do nome e da janela pedida."""
        model = _flag(plan.command, "--model")
        return {"model": model, "num_ctx": OLLAMA_CONTEXT} if model.startswith(OLLAMA_PREFIX) else None

    def final_command(self, plan, directory: Path | None) -> tuple[list[str], str]:
        """Comando com `--model-settings-file`, que pede ao Ollama a janela de contexto (`num_ctx`) para o modelo."""
        if directory is None:
            return list(plan.command), ""
        settings = directory / SETTINGS_FILE
        if not settings.is_file():
            entry = [{"name": _flag(plan.command, "--model"), "extra_params": {"num_ctx": OLLAMA_CONTEXT}}]
            settings.write_text(json.dumps(entry), encoding="utf-8")
        return [*plan.command, "--model-settings-file", str(settings)], ""


HARNESS = Aider()
