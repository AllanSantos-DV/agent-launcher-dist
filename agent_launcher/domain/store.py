"""Cadastro de projetos e conexões (config.json), preferências, backup diário e recuperação."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from agent_launcher import connectors
from agent_launcher.connectors import ConnectorError, QuotaExhausted
from agent_launcher.domain.catalog import CLIS
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import (atomic_json, clean_text, path_key, project_path, protected_folder,
                                         read_json_state, read_text_retrying, validate_model)
from agent_launcher.domain.vault import Vault
from version import data_home

PROJECT_KEYS = {"id", "name", "path", "last_cli", "last_connection", "models", "last_used"}
CONNECTION_KEYS = {"id", "name", "connector", "options"}
PREFERENCES = {"bypass_default": False, "auto_update": True, "cli_auto_update": True,
               "sessions_secondary_monitor": False}
# Escolhas de texto da interface guardadas junto das preferências; o primeiro valor de cada uma é o padrão.
CHOICES = {"theme": ("light", "dark", "system"), "accent": ("green", "blue", "purple", "graphite")}
# Teto de gasto do mês: número opcional nas preferências do perfil. Fora de PREFERENCES e CHOICES, uma versão que não o
# conhece o preserva sem recusar o arquivo; ausente ou ilegível vale como desligado.
LIMIT_KEY = "monthly_limit"
BACKUP_DAYS = 30
DEFAULT_HOME = Path.home() / ".agent-launcher"
# Destinos novos (Copilot CLI, VS Code, Copilot app) guardam a última escolha em selections.json: o config.json
# continua no formato da 1.0 (last_cli e models só com claude/codex), legível por uma instalação 1.0.x que divida a
# mesma pasta de dados (o orquestrador pode estar numa versão anterior durante a atualização automática).
SELECTIONS = "selections.json"
LIMIT_NOTICE_KEY = "limit_notice"
QUOTA_MESSAGE_MAX = 300


def quota_message(text: str, secrets: list[str]) -> str:
    """Texto do aviso de cota como vai em claro para o config.json: sem valor de campo secreto, numa linha e com teto
    (o texto vem do conector do usuário, que pode ecoar a resposta inteira do serviço)."""
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[omitido]")
    text = " ".join(text.split())
    return text if len(text) <= QUOTA_MESSAGE_MAX else text[:QUOTA_MESSAGE_MAX - 1] + "…"


def valid_limit(value) -> bool:
    """True para um número finito maior que zero (bool não conta como número)."""
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def secret_values(module, options: dict) -> list[str]:
    return [options[item["name"]] for item in module.FIELDS if item.get("secret")]


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
            if self._valid_copy(copy) is None:
                continue
            kept = shutil.copyfile(self.path, self.backups / f"invalid-config-{time.strftime('%Y-%m-%d-%H%M%S')}.json")
            newer = rejected.get("preferences") if isinstance(rejected, dict) else None
            if isinstance(newer, dict):
                self.data["preferences"].update({key: value for key, value in newer.items()
                                                 if isinstance(key, str) and (type(value) is bool or (
                                                     key in CHOICES and isinstance(value, str)) or (
                                                     key == LIMIT_KEY and valid_limit(value)))})
            atomic_json(self.path, self.data)
            self._prune_backups("invalid-config-*.json")
            self.recovered = (f"Configuração inválida ({error}); restaurada a cópia {copy.name} com as preferências "
                              f"atuais. O arquivo recusado ficou em {kept}.")
            return
        raise LauncherError(
            f"Configuração inválida em {self.path} ({error}) e nenhuma cópia válida em {self.backups}. "
            "O arquivo foi preservado; corrija-o antes de continuar."
        ) from error

    def _valid_copy(self, copy: Path) -> dict | None:
        """Conteúdo da cópia se ela lê e passa na validação (e passa a ser self.data); senão None, com self.data como
        estava."""
        previous = self.data
        try:
            self.data = data = json.loads(read_text_retrying(copy))
            self.validate()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, LauncherError):
            self.data = previous
            return None
        return data

    def _prune_backups(self, pattern: str) -> None:
        """Deixa só as BACKUP_DAYS cópias mais recentes de `pattern`. Cópia velha presa por antivírus fica: a gravação
        que já deu certo não falha por isso."""
        for old in sorted(self.backups.glob(pattern), reverse=True)[BACKUP_DAYS:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _recover(self, legacy: list[Path]) -> None:
        copies = sorted(self.backups.glob("config-*.json"), reverse=True)
        previous = self.data
        for copy in copies:
            data = self._valid_copy(copy)
            if data is None:
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
                type(value) is bool for key, value in preferences.items() if key in PREFERENCES) or not all(
                isinstance(value, str) for key, value in preferences.items() if key in CHOICES):
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
            self._prune_backups("config-*.json")

    # Projetos

    @property
    def projects(self) -> list[dict]:
        """Os projetos fixados primeiro e, dentro de cada grupo, o de uso mais recente primeiro."""
        pinned = self.pinned_ids()
        return sorted(self.data["projects"], key=lambda project: (project["id"] not in pinned, -project["last_used"],
                                                                  project["name"].casefold()))

    def pinned_ids(self) -> set[str]:
        """Projetos fixados no topo. Ficam em selections.json: uma instalação mais antiga valida o config.json com
        chaves exatas e recusaria um campo novo no projeto."""
        return {identifier for identifier, chosen in self._selections()["projects"].items()
                if isinstance(chosen, dict) and chosen.get("pinned") is True}

    def is_pinned(self, project: dict) -> bool:
        """True se o projeto está fixado no topo da lista."""
        return project["id"] in self.pinned_ids()

    def set_pinned(self, project: dict, pinned: bool) -> None:
        """Fixa ou desafixa o projeto; o config.json não é tocado."""
        selections = self._selections()
        chosen = selections["projects"].get(project["id"])
        if not isinstance(chosen, dict):
            chosen = selections["projects"][project["id"]] = {}
        if pinned:
            chosen["pinned"] = True
        else:
            chosen.pop("pinned", None)
            if not chosen:
                del selections["projects"][project["id"]]
        atomic_json(self.home / SELECTIONS, selections)

    def _project_label(self, value: str, own: dict | None = None) -> str:
        """Nome de projeto válido: não vazio, até 100 caracteres e diferente do de qualquer outro projeto (`own`, o
        projeto que está sendo renomeado, não conta)."""
        label = clean_text(value, "Nome")
        if len(label) > 100:
            raise LauncherError("Use um nome de projeto com até 100 caracteres.")
        if any(project is not own and project["name"].casefold() == label.casefold() for project in self.data["projects"]):
            raise LauncherError("Já existe um projeto com esse nome. Escolha outro nome.")
        return label

    def add(self, path: str, name: str | None = None, save: bool = True) -> dict:
        resolved = project_path(path)
        for project in self.projects:
            if path_key(project["path"]) == path_key(str(resolved)):
                return project
        label = self._project_label(name or resolved.name or str(resolved))
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

    def find_or_add_here(self, reference: str | None, folder: str, save: bool = True) -> tuple[dict, bool]:
        """`--add-here`: o projeto da referência (sem ela, o da pasta `folder`) quando já está cadastrado; senão cadastra
        `folder`, a pasta atual, com o nome dela; se a pasta já é um projeto (com outro nome), devolve esse projeto sem
        cadastrar. A referência, se vier, tem de ser o nome ou o caminho dessa pasta, para um nome errado nunca virar
        cadastro. Recusa a raiz de um disco, a pasta de usuário e as do sistema. Devolve
        (projeto, recém-cadastrado); `save=False` cadastra só na memória (`--dry-run` não grava)."""
        try:
            return self.find(reference or folder), False
        except LauncherError:
            pass
        here = project_path(folder)
        if reference and reference.casefold() != here.name.casefold() and path_key(reference) != path_key(str(here)):
            raise LauncherError(f"--add-here cadastra a pasta atual ({here}) com o nome dela, e {reference!r} não é esse "
                                f"nome nem esse caminho. Omita --project, use --project {here.name!r} ou cadastre com "
                                f"'projects add CAMINHO --name {reference}'.")
        registered = next((project for project in self.projects if path_key(project["path"]) == path_key(str(here))), None)
        if registered:
            return registered, False
        protected = protected_folder(here)
        if protected:
            raise LauncherError(f"--add-here não cadastra {protected}: {here}. Entre na pasta do projeto e repita, ou "
                                "cadastre com 'projects add CAMINHO'.")
        same_name = next((project for project in self.projects if project["name"].casefold() == here.name.casefold()), None)
        if same_name:
            raise LauncherError(f"Já existe um projeto chamado {same_name['name']!r} em {same_name['path']}, e --add-here "
                                f"não renomeia. Cadastre esta pasta com 'projects add \"{here}\" --name OUTRO NOME'.")
        return self.add(str(here), save=save), True

    def update(self, reference: str, name: str | None = None, path: str | None = None) -> dict:
        """Renomeia o projeto e/ou aponta para outra pasta existente; modelos e histórico de uso continuam."""
        project = self.find(reference)
        label = self._project_label(name if name is not None else project["name"], project)
        resolved = str(project_path(path)) if path is not None else project["path"]
        for other in self.data["projects"]:
            if other is project:
                continue
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

    def restore_removed(self, date: str | None = None) -> list[dict]:
        """Devolve ao cadastro os projetos que constam numa cópia diária e já não estão na lista: a cópia da data
        (AAAA-MM-DD) ou, sem data, a mais recente que ainda tenha algum. Conexões, chaves e preferências não mudam; a
        última conexão do projeto só volta se ela ainda existe. Levanta se não há cópia ou nada a restaurar."""
        pattern = f"config-{date}.json" if date else "config-*.json"
        copies = sorted(self.backups.glob(pattern), reverse=True)
        if not copies:
            raise LauncherError(f"Nenhuma cópia diária de {date} em {self.backups}." if date
                                else f"Nenhuma cópia diária em {self.backups}.")
        known = {identifier["id"] for identifier in self.data["connections"]}
        for copy in copies:
            try:
                saved = json.loads(read_text_retrying(copy))["projects"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            paths = {path_key(project["path"]) for project in self.data["projects"]}
            names = {project["name"].casefold() for project in self.data["projects"]}
            ids = {project["id"] for project in self.data["projects"]}
            restored = []
            for project in saved:
                if path_key(project["path"]) in paths or project["id"] in ids:
                    continue
                project = dict(project)
                if project["name"].casefold() in names:
                    project["name"] = f"{project['name']} (restaurado)"
                if project.get("last_connection") not in known:
                    project["last_connection"] = None
                self.data["projects"].append(project)
                restored.append(project)
            if restored:
                self.save()
                return restored
        raise LauncherError("As cópias diárias não têm nenhum projeto fora do cadastro atual.")

    # Última escolha por projeto: claude/codex no config.json, os outros destinos em selections.json

    def _selections(self) -> dict:
        data = read_json_state(self.home / SELECTIONS,
                               accept=lambda data: isinstance(data, dict) and isinstance(data.get("projects"), dict))
        return data or {"projects": {}}

    def limit_notice(self, month: str, limit: float) -> int:
        """Nível do teto (80 ou 100) já avisado na bandeja em `month` (AAAA-MM) para este `limit`; 0 se nenhum, e
        também quando o aviso guardado foi dado para outro limite. Registro sem limite guardado vale para o limite atual.
        Fica em selections.json: o config.json, validado por chaves exatas nas versões antigas, não muda."""
        saved = self._selections().get(LIMIT_NOTICE_KEY)
        if not (isinstance(saved, dict) and saved.get("month") == month and type(saved.get("level")) is int):
            return 0
        return saved["level"] if saved.get("limit", limit) == limit else 0

    def set_limit_notice(self, month: str, level: int, limit: float) -> None:
        """Guarda o nível avisado do mês para o limite vigente; o registro de outro mês ou limite é substituído, mas os
        níveis por fonte do mesmo mês ficam."""
        selections = self._selections()
        record = {"month": month, "level": level, "limit": limit}
        sources = self._notice_sources(selections.get(LIMIT_NOTICE_KEY), month)
        if sources:
            record["sources"] = sources
        selections[LIMIT_NOTICE_KEY] = record
        atomic_json(self.home / SELECTIONS, selections)

    @staticmethod
    def _notice_sources(saved, month: str) -> dict:
        """Níveis por conexão e fonte do registro de `month` ({conexão: {fonte: {level, limit}}}); vazio se o registro é
        de outro mês, e o que não tem esse formato (inclusive o antigo, só por fonte) é descartado."""
        sources = saved.get("sources") if isinstance(saved, dict) and saved.get("month") == month else None
        if not isinstance(sources, dict):
            return {}
        kept = {connection: {source: entry for source, entry in found.items() if isinstance(entry, dict)}
                for connection, found in sources.items() if isinstance(found, dict)}
        return {connection: found for connection, found in kept.items() if found}

    def source_notice(self, month: str, connection_id: str, source: str, limit: float) -> int:
        """Nível do limite (80 ou 100) já avisado na bandeja em `month` para a `source` desta conexão e este `limit`
        dela; 0 se nenhum, e também quando o aviso guardado foi dado para outro limite."""
        saved = self._notice_sources(self._selections().get(LIMIT_NOTICE_KEY), month)
        entry = saved.get(connection_id, {}).get(source)
        if not (isinstance(entry, dict) and type(entry.get("level")) is int):
            return 0
        return entry["level"] if entry.get("limit") == limit else 0

    def set_source_notice(self, month: str, connection_id: str, source: str, level: int, limit: float) -> None:
        """Guarda o nível avisado da fonte da conexão no registro do mês, sem tocar no nível do teto manual nem nos das
        outras conexões; o registro de outro mês é substituído."""
        selections = self._selections()
        saved = selections.get(LIMIT_NOTICE_KEY)
        record = dict(saved) if isinstance(saved, dict) and saved.get("month") == month else {"month": month}
        sources = self._notice_sources(saved, month)
        sources[connection_id] = {**sources.get(connection_id, {}), source: {"level": level, "limit": limit}}
        record["sources"] = sources
        selections[LIMIT_NOTICE_KEY] = record
        atomic_json(self.home / SELECTIONS, selections)

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

    def export_projects(self, destination: Path) -> int:
        """Grava só nome e pasta de cada projeto, no formato que `import_legacy` lê. Recusa arquivo existente e pasta
        de destino inexistente (nada é criado); devolve quantos projetos foram gravados."""
        if destination.is_dir():
            raise LauncherError(f"{destination} é uma pasta; informe o nome de um arquivo.")
        if not destination.parent.is_dir():
            raise LauncherError(f"A pasta de destino não existe: {destination.parent}. Crie a pasta ou escolha outro caminho.")
        projects = [{"name": project["name"], "path": project["path"]} for project in self.projects]
        try:
            destination.open("x").close()  # a criação exclusiva é o que recusa o arquivo que outro processo criou agora
        except FileExistsError:
            raise LauncherError(f"{destination} já existe e não é sobrescrito. Escolha outro nome ou apague o arquivo antes.") from None
        except OSError as error:
            raise LauncherError(f"Não foi possível gravar {destination}: {error}") from error
        try:
            atomic_json(destination, {"projects": projects})
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise LauncherError(f"Não foi possível gravar {destination}: {error}") from error
        return len(projects)

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
        quota = None
        try:
            connectors.normalize_targets(module.targets(options))
            if verify:
                try:
                    module.verify(options)
                except QuotaExhausted as error:  # login e assinatura valem: grava, com o estado "cota esgotada"
                    quota = {"state": "quota_exhausted", "message": quota_message(str(error), secret_values(module, options)),
                             "at": time.time()}
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
        if quota:
            connection["status"] = quota
        elif verify:
            connection.pop("status", None)  # verificada de verdade: o estado antigo de cota deixa de valer
        for key, value in hidden.items():
            self.vault.set(f"{identifier}:{key}", value)
        self.save()
        return connection

    def mark_quota_exhausted(self, reference: str, message: str, secrets: list[str]) -> None:
        """Grava na conexão o estado "cota esgotada" com a mensagem do provedor (sem valor de campo secreto) e a hora da
        marca (`at`, epoch em segundos): o endpoint local a usa para decidir até quando pula a conexão."""
        self.find_connection(reference)["status"] = {"state": "quota_exhausted", "message": quota_message(message, secrets),
                                                     "at": time.time()}
        self.save()

    def clear_connection_status(self, reference: str) -> None:
        connection = self.find_connection(reference)
        if connection.pop("status", None) is not None:
            self.save()

    def fallback_of(self, connection: dict) -> dict | None:
        """A conexão reserva gravada em `connection` (`fallback_connection`: id ou nome) ou None. Valor ausente, de tipo
        errado, conexão inexistente, a própria conexão ou ciclo valem como sem reserva: o endpoint nunca salta em laço."""
        reference = connection.get("fallback_connection")
        if not isinstance(reference, str):
            return None
        try:
            reserve = self.find_connection(reference)
        except LauncherError:
            return None
        if reserve["id"] == connection["id"] or self._reaches(reserve, connection["id"]):
            return None
        return reserve

    def _reaches(self, start: dict, target_id: str) -> bool:
        """Se seguir as reservas a partir de `start` volta à conexão `target_id` (ou entra num ciclo)."""
        seen = set()
        current = start
        while isinstance(current.get("fallback_connection"), str) and current["id"] not in seen:
            seen.add(current["id"])
            try:
                current = self.find_connection(current["fallback_connection"])
            except LauncherError:
                return False
            if current["id"] == target_id:
                return True
        return False

    def set_fallback(self, reference: str, fallback_reference: str) -> None:
        """Define a reserva de `reference`. LauncherError se a reserva não existe, é a própria conexão ou fecha um ciclo."""
        connection = self.find_connection(reference)
        reserve = self.find_connection(fallback_reference)
        if reserve["id"] == connection["id"]:
            raise LauncherError(f"A conexão reserva de {connection['name']} não pode ser a própria conexão.")
        if self._reaches(reserve, connection["id"]):
            raise LauncherError(f"Ciclo de reservas recusado: {reserve['name']} já tem {connection['name']} como reserva, "
                                "direta ou encadeada.")
        connection["fallback_connection"] = reserve["id"]
        self.save()

    def clear_fallback(self, reference: str) -> None:
        connection = self.find_connection(reference)
        if connection.pop("fallback_connection", None) is not None:
            self.save()

    @staticmethod
    def endpoint_sessions_of(connection: dict) -> bool:
        """A conexão abre as sessões de terminal (Claude Code e Codex) pelo endpoint local? Só `true` liga; ausente ou de
        outro tipo vale como desligado (caminho direto ao host)."""
        return connection.get("endpoint_sessions") is True

    def set_endpoint_sessions(self, reference: str, enabled: bool) -> None:
        """Liga ou desliga, na conexão `reference`, as sessões de terminal pelo endpoint local; desligar tira o campo."""
        connection = self.find_connection(reference)
        if enabled:
            connection["endpoint_sessions"] = True
        elif connection.pop("endpoint_sessions", None) is None:
            return
        self.save()

    def remove_connection(self, reference: str) -> None:
        connection = self.find_connection(reference)
        self.data["connections"].remove(connection)
        for other in self.data["connections"]:
            reference = other.get("fallback_connection")
            if isinstance(reference, str) and reference.casefold() in (connection["id"], connection["name"].casefold()):
                del other["fallback_connection"]
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

    def monthly_limit(self) -> float | None:
        """Teto de gasto do mês deste perfil, na moeda do relatório de uso; None quando desligado."""
        value = self.data["preferences"].get(LIMIT_KEY)
        return float(value) if valid_limit(value) else None

    def set_monthly_limit(self, value: float | None) -> None:
        """Grava o teto de gasto do mês; None desliga e tira o campo do config.json."""
        if value is None:
            self.data["preferences"].pop(LIMIT_KEY, None)
        elif valid_limit(value):
            self.data["preferences"][LIMIT_KEY] = float(value)
        else:
            raise LauncherError(f"Limite mensal inválido: {value!r}. Use um valor maior que zero.")
        self.save()

    def choice(self, key: str) -> str:
        """Escolha de texto da interface (tema, cor de destaque). Valor que esta versão não conhece, vindo de uma
        mais nova que gravou a mesma pasta, vale como o padrão."""
        value = self.data["preferences"].get(key)
        return value if value in CHOICES[key] else CHOICES[key][0]

    def set_choice(self, key: str, value: str) -> None:
        if key not in CHOICES:
            raise LauncherError(f"Escolha desconhecida: {key}")
        if value not in CHOICES[key]:
            raise LauncherError(f"Valor inválido para {key}: {value}")
        self.data["preferences"][key] = value
        self.save()
