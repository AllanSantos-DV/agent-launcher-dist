"""VS Code: chat do Copilot com BYOK num perfil dedicado "Agent Launcher", apontando para o endpoint local
da conexão. O perfil do usuário nunca é tocado; o VS Code não recebe chave nem host real.

Como o VS Code cria perfil: `Code.exe --profile "Agent Launcher" <pasta>` cria (se faltar) e abre a pasta com ele; o
perfil ganha uma pasta com nome aleatório em User/profiles/, registrada em User/globalStorage/storage.json
(userDataProfiles). O launcher só LÊ esse registro para achar a pasta do perfil e escreve dentro dela
chatLanguageModels.json e settings.json. O VS Code observa a pasta: os modelos aparecem sem reiniciar."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from agent_launcher.domain.catalog import Catalog, ModelInfo, protocol_for, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, project_path
from agent_launcher.harnesses.base import Harness

PROFILE = "Agent Launcher"
PROVIDER_PREFIX = "Agent Launcher"
API_TYPES = {"messages": "messages", "responses": "responses", "chat_completions": "chat-completions"}
ROUTES = {"messages": "/v1/messages", "responses": "/v1/responses", "chat_completions": "/v1/chat/completions"}
MODELS_LIMIT = 150
DEFAULT_INPUT, DEFAULT_OUTPUT = 128000, 16000


def user_data_dir() -> Path:
    custom = os.environ.get("AGENT_LAUNCHER_VSCODE_USER_DATA")  # testes e instalações portáteis
    if custom:
        return Path(custom)
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Code"


def find_code() -> str | None:
    """Code.exe (a interface; o code.cmd é só um wrapper que o launcher não roda por cmd /c)."""
    candidates = []
    wrapper = shutil.which("code")
    if wrapper:
        candidates.append(Path(wrapper).resolve().parent.parent / "Code.exe")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    candidates += [local / "Programs" / "Microsoft VS Code" / "Code.exe",
                   Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft VS Code" / "Code.exe"]
    return next((str(path) for path in candidates if path.is_file()), None)


def profile_folder(data_dir: Path | None = None) -> Path | None:
    """Pasta do perfil "Agent Launcher" pelo registro do VS Code (só leitura); None se ainda não existe."""
    data_dir = data_dir or user_data_dir()
    try:
        storage = json.loads((data_dir / "User" / "globalStorage" / "storage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for profile in storage.get("userDataProfiles") or []:
        if isinstance(profile, dict) and profile.get("name") == PROFILE and isinstance(profile.get("location"), str):
            location = profile["location"]
            if "/" in location or "\\" in location or location.startswith("."):
                return None  # perfil embutido (builtin/...) ou fora do lugar: não é o nosso
            return data_dir / "User" / "profiles" / location
    return None


def model_entries(gateway, catalog: Catalog, selected: ModelInfo, base: str) -> list[dict]:
    """Modelos do catálogo do launcher para o Custom Endpoint: ID exato do host, URL do endpoint local por protocolo."""
    records = [record for record in catalog.records if record.chat and record.tools is not False
               and protocol_for(record, gateway.protocols, gateway.any_model, VSCODE_CONSUMES)]
    if len(records) > MODELS_LIMIT:
        records = [record for record in records if record.provider == selected.provider] or [selected]
    if selected.id not in {record.id for record in records}:
        records.insert(0, selected)
    entries = []
    for record in records:
        protocol = protocol_for(record, gateway.protocols, gateway.any_model, VSCODE_CONSUMES)
        if protocol is None:
            continue
        entry = {"id": record.id, "name": record.id, "url": base + ROUTES[protocol], "apiType": API_TYPES[protocol],
                 "toolCalling": record.tools is not False, "vision": False,
                 "maxInputTokens": (record.max_input_tokens if catalog.verified and record.max_input_tokens else DEFAULT_INPUT),
                 "maxOutputTokens": (record.max_output_tokens if catalog.verified and record.max_output_tokens else DEFAULT_OUTPUT)}
        if protocol == "responses":
            entry["statefulResponses"] = False  # o endpoint local é sem estado; o histórico vai em cada requisição
        entries.append(entry)
    return entries


VSCODE_CONSUMES = ("messages", "responses", "chat_completions")


def write_profile(folder: Path, provider_name: str, entries: list[dict], selected: str) -> None:
    """chatLanguageModels.json (troca só o provider desta conexão) e settings.json (mescla) do perfil dedicado."""
    folder.mkdir(parents=True, exist_ok=True)
    models_file = folder / "chatLanguageModels.json"
    try:
        current = json.loads(models_file.read_text(encoding="utf-8"))
        current = current if isinstance(current, list) else []
    except (OSError, ValueError):
        current = []
    providers = [item for item in current if not (isinstance(item, dict) and item.get("name") == provider_name)]
    providers.append({"name": provider_name, "vendor": "customendpoint", "apiType": entries[0]["apiType"] if entries else
                      "chat-completions", "models": entries})
    models_file.write_text(json.dumps(providers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settings_file = folder / "settings.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        settings = settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        settings = {}
    # Sem login GitHub, os modelos utilitários (títulos, resumos) também precisam ser da conexão.
    small = next((entry["id"] for entry in entries if "mini" in entry["id"] or "haiku" in entry["id"]), selected)
    settings.update({"chat.utilityModel": f"customendpoint/{selected}", "chat.utilitySmallModel": f"customendpoint/{small}",
                     "chat.defaultModel": f"customendpoint/{selected}"})
    atomic_json(settings_file, settings)


def remove_provider(name: str, folder: Path | None = None) -> bool:
    """Tira o provider da conexão do chatLanguageModels.json do perfil \"Agent Launcher\" e, se chat.defaultModel ou os
    modelos utilitários apontam para um modelo que só ele servia, apaga essas chaves do settings.json. True se mudou."""
    folder = Path(folder) if folder else profile_folder()
    if folder is None:
        return False
    models_file = folder / "chatLanguageModels.json"
    try:
        current = json.loads(models_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:  # ilegível não é "nada a remover": o provider pode continuar lá
        raise LauncherError(f"Não foi possível ler {models_file} para remover o provider \"{name}\": {error}") from error
    if not isinstance(current, list):
        raise LauncherError(f"{models_file} não é uma lista de providers; o provider \"{name}\" não foi removido.")
    removed = [item for item in current if isinstance(item, dict) and item.get("name") == name]
    if not removed:
        return False
    kept = [item for item in current if not (isinstance(item, dict) and item.get("name") == name)]
    models_file.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gone = {model.get("id") for item in removed for model in item.get("models") or [] if isinstance(model, dict)}
    still = {model.get("id") for item in kept if isinstance(item, dict) and item.get("vendor") == "customendpoint"
             for model in item.get("models") or [] if isinstance(model, dict)}
    orphan = {f"customendpoint/{model}" for model in gone - still}
    settings_file = folder / "settings.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if isinstance(settings, dict):
        keys = [key for key in ("chat.defaultModel", "chat.utilityModel", "chat.utilitySmallModel")
                if settings.get(key) in orphan]
        for key in keys:
            del settings[key]
        if keys:
            atomic_json(settings_file, settings)
    return True


class VsCode(Harness):
    ID = "vscode"
    LABEL = "VS Code"
    KIND = "ide"
    CONSUMES = VSCODE_CONSUMES
    CAPABILITIES = frozenset({"context_window"})
    SUBTITLE = "Chat do Copilot, perfil próprio"
    SYMBOL = "editor"
    SESSION_HINT = "Perfil Agent Launcher no VS Code"
    MISSING_HINT = "Instale o VS Code (code.visualstudio.com) e abra de novo."
    EXECUTABLE = "code"

    def find_executable(self) -> str | None:
        return find_code()

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        return (record.chat and record.tools is not False
                and protocol_for(record, catalog.protocols, catalog.any_model, self.CONSUMES) is not None)

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        protocol = protocol_for(record, catalog.protocols, catalog.any_model, self.CONSUMES)
        return (f"Entrada declarada: {token_label(record.max_input_tokens).replace('.', ',')} tokens",
                f"Pelo endpoint local da conexão ({API_TYPES.get(protocol, '?')}); o VS Code não recebe chave nem host.")

    def command(self) -> list[str]:
        code = find_code()
        if not code:
            raise LauncherError(f"{self.LABEL} não encontrado. {self.MISSING_HINT}")
        return [code]

    def plan(self, context):
        raise LauncherError("O VS Code abre pela interface ou por 'launch --cli vscode', não numa janela de terminal.")

    def preview(self, store, project: dict, gateway, model: str, catalog: Catalog) -> dict:
        """O que seria escrito no perfil (--dry-run): sem subir o endpoint, sem abrir nada."""
        from agent_launcher.endpoint import registry as endpoints
        selected = catalog.find(model) or ModelInfo(model)
        base = f"http://127.0.0.1:{endpoints.port_for(store.home, gateway.connection['id'], reserve=False)}"
        entries = model_entries(gateway, catalog, selected, base)
        return {"cli": self.ID, "project": str(project_path(project["path"])), "model": selected.id,
                "connection": gateway.name, "profile": PROFILE, "endpoint": base, "provider": self.provider_name(gateway),
                "models": len(entries), "selected": next((e for e in entries if e["id"] == selected.id), None),
                "profile_folder": str(profile_folder() or "(criada pelo VS Code na primeira abertura)")}

    @staticmethod
    def provider_name(gateway) -> str:
        from agent_launcher.domain import profiles
        profile = profiles.label(gateway.store.home)
        return f"{PROVIDER_PREFIX} — {profile} — {gateway.name}" if profile else f"{PROVIDER_PREFIX} — {gateway.name}"

    def open(self, store, project: dict, gateway, model: str, catalog: Catalog, confirm=lambda question: True,
             wait: float = 30) -> str:
        from agent_launcher.endpoint import registry as endpoints
        code = find_code()
        if not code:
            raise LauncherError(f"{self.LABEL} não encontrado. {self.MISSING_HINT}")
        directory = project_path(project["path"])
        selected = catalog.find(model) or ModelInfo(model)
        port = endpoints.ensure_running(store.home, gateway.connection["id"])
        entries = model_entries(gateway, catalog, selected, f"http://127.0.0.1:{port}")
        folder = profile_folder()
        if folder is not None:
            write_profile(folder, self.provider_name(gateway), entries, selected.id)
        environment = {key: value for key, value in os.environ.items() if key not in ("ELECTRON_RUN_AS_NODE", "VSCODE_DEV")}
        subprocess.Popen([code, "--profile", PROFILE, str(directory)], cwd=directory, env=environment, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if folder is None:
            # Primeira vez: o VS Code cria o perfil ao abrir; os modelos entram assim que a pasta aparece (ele observa
            # a pasta do perfil, então não precisa reiniciar).
            deadline = time.monotonic() + wait
            while folder is None and time.monotonic() < deadline:
                time.sleep(0.5)
                folder = profile_folder()
            if folder is None:
                raise LauncherError("O VS Code abriu, mas o perfil \"Agent Launcher\" não apareceu a tempo. Abra de novo "
                                    "pelo launcher para gravar os modelos.")
            write_profile(folder, self.provider_name(gateway), entries, selected.id)
        return (f"VS Code aberto no perfil \"{PROFILE}\" com {len(entries)} modelos de {gateway.name} "
                f"(endpoint local 127.0.0.1:{port}). Escolha o modelo no chat do Copilot.")


HARNESS = VsCode()
