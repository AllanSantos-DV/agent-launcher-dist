"""Cadastro de projetos e conexões (config.json), preferências, backup diário e recuperação."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from agent_launcher import connectors
from agent_launcher.connectors import ConnectorError
from agent_launcher.domain.catalog import CLIS
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import (atomic_json, clean_text, path_key, project_path, read_json_state,
                                         read_text_retrying, validate_model)
from agent_launcher.domain.vault import Vault
from version import data_home

PROJECT_KEYS = {"id", "name", "path", "last_cli", "last_connection", "models", "last_used"}
CONNECTION_KEYS = {"id", "name", "connector", "options"}
PREFERENCES = {"bypass_default": False, "auto_update": True, "cli_auto_update": True,
               "sessions_secondary_monitor": False}
BACKUP_DAYS = 30
DEFAULT_HOME = Path.home() / ".agent-launcher"
# Destinos novos (Copilot CLI, VS Code, Copilot app) guardam a última escolha em selections.json: o config.json
# continua no formato da 1.0 (last_cli e models só com claude/codex), legível por uma instalação 1.0.x que divida a
# mesma pasta de dados (o orquestrador pode estar numa versão anterior durante a atualização automática).
SELECTIONS = "selections.json"


def legacy_configs() -> list[Path]:
    """config.json da instalação anterior (launcher-v2), que guardava os projetos em .data/ dentro da pasta instalada."""
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    candidates = sorted((local / "Programs").glob("*/launcher-v2/.data/config.json"))
    if os.environ.get("LAUNCHER_V2_HOME"):
        candidates.insert(0, Path(os.environ["LAUNCHER_V2_HOME"]).expanduser() / "config.json")
    return candidates


class Store:
    """config.json em data_home(), fora da pasta instalada: atualizar ou reinstalar não toca nos projetos.

    Cada gravação com projetos guarda uma cópia do dia em backups/ (BACKUP_DAYS mais recentes). Ao abrir sem
    config.json, ou com a lista vazia sem que o usuário a tenha esvaziado, volta a cópia mais recente; sem cópia
    nenhuma, importa os projetos da instalação anterior (launcher-v2, que guardava tudo dentro da pasta instalada).
    O que foi recuperado fica em self.recovered para a interface e o modo texto avisarem."""

    def __init__(self, home: Path | None = None, legacy: list[Path] | None = None):
        self.home = Path(home) if home else data_home()
        self.path = self.home / "config.json"
        self.backups = self.home / "backups"
        self.vault = Vault(self.home)
        self.recovered = None
        self.data = {"schema": 2, "projects": [], "last_project": None, "connections": [],
                     "default_connection": None, "preferences": dict(PREFERENCES)}
        if self.path.exists():
            try:
                self.data = json.loads(read_text_retrying(self.path))
                self.validate()
            except (ValueError, TypeError, KeyError, AttributeError, LauncherError) as error:
                self._restore_from_backup(error)
        if not self.data["projects"]:
            self._recover(legacy_configs() if legacy is None and self.home == DEFAULT_HOME else legacy or [])

    def _restore_from_backup(self, error: Exception) -> None:
        """config.json inválido (corrompido, ou de uma versão com campos que esta não entende): guarda o arquivo ao lado
        das cópias, volta a cópia válida mais recente com as preferências do arquivo que ainda valem e avisa em
        self.recovered. Sem nenhuma cópia válida, levanta (nada é apagado)."""
        rejected = self.data
        try:
            rejected = json.loads(read_text_retrying(self.path))
        except (OSError, ValueError):
            rejected = None
        for copy in sorted(self.backups.glob("config-*.json"), reverse=True):
            try:
                self.data = json.loads(read_text_retrying(copy))
                self.validate()
            except (OSError, ValueError, TypeError, KeyError, AttributeError, LauncherError):
                continue
            kept = shutil.copyfile(self.path, self.backups / f"invalid-config-{time.strftime('%Y-%m-%d-%H%M%S')}.json")
            newer = rejected.get("preferences") if isinstance(rejected, dict) else None
            if isinstance(newer, dict):
                self.data["preferences"].update({key: value for key, value in newer.items()
                                                 if isinstance(key, str) and type(value) is bool})
            atomic_json(self.path, self.data)
            for old in sorted(self.backups.glob("invalid-config-*.json"), reverse=True)[BACKUP_DAYS:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            self.recovered = (f"Configuração inválida ({error}); restaurada a cópia {copy.name} com as preferências "
                              f"atuais. O arquivo recusado ficou em {kept}.")
            return
        raise LauncherError(
            f"Configuração inválida em {self.path} ({error}) e nenhuma cópia válida em {self.backups}. "
            "O arquivo foi preservado; corrija-o antes de continuar."
        ) from error

    def _recover(self, legacy: list[Path]) -> None:
        copies = sorted(self.backups.glob("config-*.json"), reverse=True)
        previous = self.data
        for copy in copies:
            try:
                self.data = data = json.loads(read_text_retrying(copy))
                self.validate()
            except (OSError, ValueError, TypeError, KeyError, AttributeError, LauncherError):
                self.data = previous
                continue  # cópia corrompida: tenta a anterior
            if not data["projects"]:
                self.data = previous  # a última cópia válida é de quando o usuário esvaziou a lista
                return
            atomic_json(self.path, self.data)
            self.recovered = f"{len(data['projects'])} projeto(s) restaurado(s) da cópia {copy}."
            return
        if copies:
            return
        for source in legacy:
            if source.is_file():
                added, _skipped = self.import_legacy(source)
                if added:
                    self.recovered = f"{added} projeto(s) importado(s) da instalação anterior ({source})."
                    return

    def validate(self) -> None:
        if not isinstance(self.data, dict) or self.data.get("schema") != 2:
            raise ValueError("schema")
        if {"schema", "projects", "last_project", "connections", "default_connection", "preferences"} - set(self.data):
            raise ValueError("campos")
        # Campo e preferência de versão mais nova não derrubam esta: ficam no arquivo e voltam na próxima gravação.
        preferences = self.data["preferences"]
        if not isinstance(preferences, dict) or not all(isinstance(key, str) for key in preferences) or not all(
                type(value) is bool for key, value in preferences.items() if key in PREFERENCES):
            raise ValueError("preferências")
        connections = set()
        connection_names = set()
        for connection in self.data["connections"]:
            if not isinstance(connection, dict) or CONNECTION_KEYS - set(connection):
                raise ValueError("conexão")
            identifier = str(uuid.UUID(connection["id"]))
            name = clean_text(connection["name"], "Nome da conexão")
            if identifier in connections or name.casefold() in connection_names or len(name) > 60:
                raise ValueError("conexão duplicada")
            if not isinstance(connection["connector"], str) or not re.fullmatch(r"[\w-]{1,40}", connection["connector"]):
                raise ValueError("conector")
            options = connection["options"]
            if not isinstance(options, dict) or not all(isinstance(key, str) and type(value) in (str, bool, int, float)
                                                        for key, value in options.items()):
                raise ValueError("opções")
            connections.add(identifier)
            connection_names.add(name.casefold())
        if self.data["default_connection"] not in connections | {None}:
            raise ValueError("conexão padrão")
        if not isinstance(self.data["projects"], list):
            raise ValueError("projetos")
        identifiers = set()
        paths = set()
        names = set()
        for project in self.data["projects"]:
            if not isinstance(project, dict) or PROJECT_KEYS - set(project):
                raise ValueError("projeto")
            identifier = str(uuid.UUID(project["id"]))
            name = clean_text(project["name"], "Nome")
            path = clean_text(project["path"], "Caminho")
            if not Path(path).is_absolute() or len(name) > 100:
                raise ValueError("caminho/nome")
            if identifier in identifiers or path_key(path) in paths or name.casefold() in names:
                raise ValueError("duplicado")
            identifiers.add(identifier)
            paths.add(path_key(path))
            names.add(name.casefold())
            if project["last_cli"] not in CLIS or project["last_connection"] not in connections | {None}:
                raise ValueError("CLI/conexão")
            if not isinstance(project["models"], dict) or set(project["models"]) - connections:
                raise ValueError("modelos")
            for chosen in project["models"].values():
                if not isinstance(chosen, dict) or not all(isinstance(key, str) for key in chosen):
                    raise ValueError("modelos")
                for key, model in chosen.items():
                    if key in CLIS:
                        validate_model(model)
            if not isinstance(project["last_used"], (int, float)) or not 0 <= project["last_used"] < 1e12:
                raise ValueError("data")
        if self.data["last_project"] is not None and self.data["last_project"] not in identifiers:
            raise ValueError("último projeto")

    def save(self, backup: bool = False) -> None:
        """Grava config.json e, se há projetos (ou backup=True, quando o usuário esvazia a lista), a cópia do dia."""
        self.validate()
        atomic_json(self.path, self.data)
        if self.data["projects"] or backup:
            atomic_json(self.backups / f"config-{time.strftime('%Y-%m-%d')}.json", self.data)
            for old in sorted(self.backups.glob("config-*.json"), reverse=True)[BACKUP_DAYS:]:
                try:
                    old.unlink()
                except OSError:
                    pass  # cópia velha presa por antivírus: a gravação que já deu certo não falha por isso

    # Projetos

    @property
    def projects(self) -> list[dict]:
        return sorted(self.data["projects"], key=lambda project: (-project["last_used"], project["name"].casefold()))

    def add(self, path: str, name: str | None = None, save: bool = True) -> dict:
        resolved = project_path(path)
        for project in self.projects:
            if path_key(project["path"]) == path_key(str(resolved)):
                return project
        label = clean_text(name or resolved.name or str(resolved), "Nome")
        if len(label) > 100:
            raise LauncherError("Use um nome de projeto com até 100 caracteres.")
        if any(project["name"].casefold() == label.casefold() for project in self.projects):
            raise LauncherError("Já existe um projeto com esse nome. Escolha outro nome.")
        project = {"id": str(uuid.uuid4()), "name": label, "path": str(resolved), "last_cli": "claude",
                   "last_connection": None, "models": {}, "last_used": 0}
        self.data["projects"].append(project)
        if save:
            self.save()
        return project

    def find(self, reference: str) -> dict:
        for project in self.projects:
            if project["id"] == reference or project["name"].casefold() == reference.casefold():
                return project
        for project in self.projects:
            if path_key(project["path"]) == path_key(reference):
                return project
        raise LauncherError("Projeto não cadastrado. Use 'projects add CAMINHO' ou a interface.")

    def update(self, reference: str, name: str | None = None, path: str | None = None) -> dict:
        """Renomeia o projeto e/ou aponta para outra pasta existente; modelos e histórico de uso continuam."""
        project = self.find(reference)
        label = clean_text(name if name is not None else project["name"], "Nome")
        if len(label) > 100:
            raise LauncherError("Use um nome de projeto com até 100 caracteres.")
        resolved = str(project_path(path)) if path is not None else project["path"]
        for other in self.data["projects"]:
            if other is project:
                continue
            if other["name"].casefold() == label.casefold():
                raise LauncherError("Já existe um projeto com esse nome. Escolha outro nome.")
            if path_key(other["path"]) == path_key(resolved):
                raise LauncherError(f"Essa pasta já está cadastrada como {other['name']}.")
        project.update(name=label, path=resolved)
        self.save()
        return project

    def remove(self, reference: str) -> None:
        project = self.find(reference)
        self.data["projects"].remove(project)
        if self.data["last_project"] == project["id"]:
            self.data["last_project"] = None
        self.save(backup=True)
        selections = self._selections()
        if selections["projects"].pop(project["id"], None) is not None:
            atomic_json(self.home / SELECTIONS, selections)

    # Última escolha por projeto: claude/codex no config.json, os outros destinos em selections.json

    def _selections(self) -> dict:
        data = read_json_state(self.home / SELECTIONS,
                               accept=lambda data: isinstance(data, dict) and isinstance(data.get("projects"), dict))
        return data or {"projects": {}}

    def last_cli(self, project: dict) -> str:
        """Destino da última abertura do projeto (inclusive os que não cabem no config.json da 1.0)."""
        chosen = self._selections()["projects"].get(project["id"], {})
        last = chosen.get("last_cli") if isinstance(chosen, dict) else None
        return last if isinstance(last, str) and last else project["last_cli"]

    def last_model(self, project: dict, connection: dict, cli: str) -> str:
        if cli in CLIS:
            return project["models"].get(connection["id"], {}).get(cli, "")
        chosen = self._selections()["projects"].get(project["id"], {})
        models = chosen.get("models", {}) if isinstance(chosen, dict) else {}
        model = models.get(connection["id"], {}).get(cli, "") if isinstance(models.get(connection["id"]), dict) else ""
        return model if isinstance(model, str) else ""

    def remember(self, project: dict, connection: dict, cli: str, model: str) -> None:
        if cli in CLIS:
            project["last_cli"] = cli
            project["models"].setdefault(connection["id"], {})[cli] = model
        project["last_connection"] = connection["id"]
        project["last_used"] = time.time()
        self.data["last_project"] = project["id"]
        self.save()
        selections = self._selections()
        chosen = selections["projects"].setdefault(project["id"], {})
        if not isinstance(chosen, dict):
            chosen = selections["projects"][project["id"]] = {}
        chosen["last_cli"] = cli
        if cli not in CLIS:
            validate_model(model)
            chosen.setdefault("models", {}).setdefault(connection["id"], {})[cli] = model
        atomic_json(self.home / SELECTIONS, selections)

    def import_legacy(self, source: Path) -> tuple[int, int]:
        """Importa só nomes e caminhos de outro config.json com {"projects": [{"name", "path"}]}."""
        if source.resolve() == self.path.resolve():
            raise LauncherError("Escolha o config.json de outra instalação, não o desta.")
        try:
            legacy = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise LauncherError("Não foi possível ler o config.json escolhido.") from error
        if not isinstance(legacy, dict) or not isinstance(legacy.get("projects"), list):
            raise LauncherError("O arquivo não contém uma lista de projetos válida.")
        added = 0
        skipped = 0
        for entry in legacy["projects"]:
            before = len(self.projects)
            try:
                if not isinstance(entry, dict):
                    raise LauncherError("Registro inválido.")
                self.add(entry.get("path", ""), entry.get("name"), save=False)
            except (LauncherError, OSError, ValueError):
                skipped += 1
                continue
            if len(self.projects) > before:
                added += 1
            else:
                skipped += 1
        if added:
            self.save()
        return added, skipped

    # Conexões

    @property
    def connections(self) -> list[dict]:
        return sorted(self.data["connections"], key=lambda connection: connection["name"].casefold())

    def connectors(self) -> tuple[dict, list[str]]:
        # Conectores do usuário são globais: um perfil extra usa os da raiz, como o padrão.
        from agent_launcher.domain.profiles import base_of
        return connectors.available(base_of(self.home) / "connectors")

    def find_connection(self, reference: str) -> dict:
        for connection in self.data["connections"]:
            if connection["id"] == reference or connection["name"].casefold() == str(reference).casefold():
                return connection
        raise LauncherError(f"Conexão não encontrada: {reference}. Veja 'connections' ou a interface.")

    def connection_for(self, project: dict | None = None, reference: str | None = None) -> dict:
        """Conexão explícita; senão a última do projeto; senão a padrão; senão a única cadastrada."""
        if reference:
            return self.find_connection(reference)
        identifiers = {connection["id"] for connection in self.data["connections"]}
        for candidate in ((project or {}).get("last_connection"), self.data["default_connection"]):
            if candidate in identifiers:
                return self.find_connection(candidate)
        if len(self.data["connections"]) == 1:
            return self.data["connections"][0]
        if not self.data["connections"]:
            raise LauncherError("Nenhuma conexão cadastrada. Adicione um provedor em Conexões (ou 'connections add').")
        raise LauncherError("Há várias conexões e nenhuma padrão. Escolha uma com --connection ou defina a padrão.")

    def connection_options(self, connection: dict) -> dict:
        modules, _errors = self.connectors()
        module = modules.get(connection["connector"])
        options = dict(connection["options"])
        for item in getattr(module, "FIELDS", ()):
            if item.get("secret"):
                options[item["name"]] = self.vault.get(f"{connection['id']}:{item['name']}")
        return options

    def save_connection(self, name: str, connector: str, values: dict, reference: str | None = None,
                        verify: bool = True) -> dict:
        """Cria (reference None) ou edita uma conexão. Testa o host antes de gravar. Campo secreto ausente ou None
        mantém o valor salvo; "" apaga."""
        modules, errors = self.connectors()
        module = modules.get(connector)
        if module is None:
            raise LauncherError(f"Conector {connector!r} não existe." + (f" Erros de carga: {'; '.join(errors)}" if errors else ""))
        name = clean_text(name, "Nome da conexão")
        if len(name) > 60:
            raise LauncherError("Use um nome de conexão com até 60 caracteres.")
        existing = self.find_connection(reference) if reference else None
        if any(connection["name"].casefold() == name.casefold() and connection is not existing
               for connection in self.data["connections"]):
            raise LauncherError("Já existe uma conexão com esse nome.")
        identifier = existing["id"] if existing else str(uuid.uuid4())
        plain, hidden, options = {}, {}, {}
        for item in module.FIELDS:
            key = item["name"]
            value = values.get(key)
            if item.get("secret"):
                if value is None:
                    value = self.vault.get(f"{identifier}:{key}") if existing and existing["connector"] == connector else ""
                else:
                    value = hidden[key] = str(value).strip()
            elif item.get("type") == "bool":
                value = plain[key] = bool(value if value is not None else item.get("default", False))
            else:
                value = plain[key] = str(value if value is not None else item.get("default", "")).strip()
            if item.get("required") and not value:
                raise LauncherError(f"Preencha {item['label']}.")
            options[key] = value
        try:
            connectors.normalize_targets(module.targets(options))
            if verify:
                module.verify(options)
        except ConnectorError as error:
            raise LauncherError(f"{module.LABEL}: {error}") from error
        if existing:
            if existing["connector"] != connector:
                self.vault.forget(identifier + ":")
            existing.update(name=name, connector=connector, options=plain)
            connection = existing
        else:
            connection = {"id": identifier, "name": name, "connector": connector, "options": plain}
            self.data["connections"].append(connection)
            if self.data["default_connection"] is None:
                self.data["default_connection"] = identifier
        for key, value in hidden.items():
            self.vault.set(f"{identifier}:{key}", value)
        self.save()
        return connection

    def remove_connection(self, reference: str) -> None:
        connection = self.find_connection(reference)
        self.data["connections"].remove(connection)
        for project in self.data["projects"]:
            project["models"].pop(connection["id"], None)
            if project["last_connection"] == connection["id"]:
                project["last_connection"] = None
        if self.data["default_connection"] == connection["id"]:
            self.data["default_connection"] = self.connections[0]["id"] if self.connections else None
        self.save()
        self.vault.forget(connection["id"] + ":")
        (self.home / "cache" / f"models-{connection['id']}.json").unlink(missing_ok=True)

    def set_default_connection(self, reference: str) -> None:
        self.data["default_connection"] = self.find_connection(reference)["id"]
        self.save()

    def preference(self, key: str) -> bool:
        return self.data["preferences"].get(key, PREFERENCES[key])

    def set_preference(self, key: str, value: bool) -> None:
        if key not in PREFERENCES:
            raise LauncherError(f"Preferência desconhecida: {key}")
        self.data["preferences"][key] = bool(value)
        self.save()
