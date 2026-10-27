"""Perfis de trabalho: cada perfil é uma pasta de dados completa (config.json, secrets.json, cache...).

O padrão é a raiz (~/.agent-launcher ou AGENT_LAUNCHER_HOME), id "default", e nunca aparece em profiles.json. Os extras
ficam em <raiz>/profiles/<id>/. Conectores e adaptadores do usuário, portas do endpoint local, logs e preferências de
app ficam na raiz e valem para todos os perfis.

`Store()` sem argumento continua sendo o padrão (o orquestrador depende disso): quem abre um perfil passa
`Store(home_of(id))`."""
from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, clean_text, path_key, read_json_state, remove_tree

DEFAULT = "default"
FILE = "profiles.json"
FOLDER = "profiles"
BACKUPS = "backups"
BACKUP_DAYS = 30
COLORS = ("#5b8def", "#d9822b", "#2f9e77", "#b05fd0", "#d64545", "#3aa0b8", "#a08a2a", "#7a7f8c")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")


def root() -> Path:
    from version import data_home
    return data_home()


def _read(base: Path | None = None) -> dict:
    base = base or root()
    data = read_json_state(base / FILE, backups=base / BACKUPS, prefix="profiles-") or {}
    profiles = [item for item in data.get("profiles") or [] if isinstance(item, dict)
                and isinstance(item.get("id"), str) and ID_PATTERN.match(item["id"]) and item["id"] != DEFAULT
                and isinstance(item.get("name"), str) and item["name"].strip()]
    active = data.get("active") if data.get("active") in {DEFAULT, *(item["id"] for item in profiles)} else DEFAULT
    return {"schema": 1, "active": active, "profiles": profiles}


def _write(data: dict, base: Path | None = None) -> None:
    base = base or root()
    atomic_json(base / FILE, data)
    folder = base / BACKUPS
    atomic_json(folder / f"profiles-{time.strftime('%Y-%m-%d')}.json", data)
    for old in sorted(folder.glob("profiles-*.json"), reverse=True)[BACKUP_DAYS:]:
        try:
            old.unlink()
        except OSError:
            pass


def default_profile() -> dict:
    return {"id": DEFAULT, "name": "Padrão", "color": None}


def list_profiles(base: Path | None = None) -> list[dict]:
    """O padrão primeiro, depois os extras na ordem de criação."""
    return [default_profile(), *(dict(item) for item in _read(base)["profiles"])]


def find(reference: str, base: Path | None = None) -> dict:
    """Perfil por id ou nome (sem diferenciar maiúsculas)."""
    wanted = str(reference or "").strip().casefold()
    for item in list_profiles(base):
        if item["id"] == reference or item["name"].casefold() == wanted:
            return item
    raise LauncherError(f"Perfil não encontrado: {reference}")


def home_of(profile_id: str, base: Path | None = None) -> Path:
    base = base or root()
    if profile_id == DEFAULT:
        return base
    if not any(item["id"] == profile_id for item in _read(base)["profiles"]):
        raise LauncherError(f"Perfil não encontrado: {profile_id}")
    return base / FOLDER / profile_id


def profile_of_home(home: Path, base: Path | None = None) -> dict:
    """Perfil dono de uma pasta de dados. Pasta fora da raiz (testes, AGENT_LAUNCHER_HOME de outro lugar) = padrão."""
    base = (base or root()).resolve()
    home = Path(home).resolve()
    if home.parent == base / FOLDER:
        for item in _read(base)["profiles"]:
            if item["id"] == home.name:
                return dict(item)
    return default_profile()


def active(base: Path | None = None) -> dict:
    data = _read(base)
    return next((dict(item) for item in data["profiles"] if item["id"] == data["active"]), default_profile())


def set_active(profile_id: str, base: Path | None = None) -> None:
    data = _read(base)
    if profile_id != DEFAULT and not any(item["id"] == profile_id for item in data["profiles"]):
        raise LauncherError(f"Perfil não encontrado: {profile_id}")
    data["active"] = profile_id
    _write(data, base)


def _name(name: str) -> str:
    name = clean_text(name, "Nome do perfil")
    if len(name) > 60:
        raise LauncherError("Nome do perfil com mais de 60 caracteres.")
    return name


def _slug(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:30] or "perfil"


def create(name: str, base: Path | None = None) -> dict:
    base = base or root()
    name = _name(name)
    data = _read(base)
    if any(item["name"].casefold() == name.casefold() for item in list_profiles(base)):
        raise LauncherError(f"Já existe um perfil chamado {name}.")
    profile = {"id": f"{_slug(name)}-{uuid.uuid4().hex[:4]}", "name": name,
               "color": COLORS[len(data["profiles"]) % len(COLORS)]}
    (base / FOLDER / profile["id"]).mkdir(parents=True, exist_ok=True)
    data["profiles"].append(profile)
    _write(data, base)
    return dict(profile)


def rename(profile_id: str, name: str, base: Path | None = None) -> dict:
    name = _name(name)
    if profile_id == DEFAULT:
        raise LauncherError("O perfil padrão não pode ser renomeado.")
    data = _read(base)
    if any(item["name"].casefold() == name.casefold() and item["id"] != profile_id for item in list_profiles(base)):
        raise LauncherError(f"Já existe um perfil chamado {name}.")
    for item in data["profiles"]:
        if item["id"] == profile_id:
            item["name"] = name
            _write(data, base)
            return dict(item)
    raise LauncherError(f"Perfil não encontrado: {profile_id}")


def set_color(profile_id: str, color: str, base: Path | None = None) -> dict:
    """Troca a cor do perfil (faixa da janela e ícone do seletor). Só #RRGGBB; o padrão não tem cor."""
    if not isinstance(color, str) or not COLOR_PATTERN.fullmatch(color.strip()):
        raise LauncherError(f"Cor inválida {color!r}: use #RRGGBB.")
    if profile_id == DEFAULT:
        raise LauncherError("O perfil padrão não tem cor.")
    data = _read(base)
    for item in data["profiles"]:
        if item["id"] == profile_id:
            item["color"] = color.strip()
            _write(data, base)
            return dict(item)
    raise LauncherError(f"Perfil não encontrado: {profile_id}")


def remove(profile_id: str, delete_data: bool = False, base: Path | None = None) -> None:
    """Tira o perfil da lista; com delete_data, apaga a pasta dele (projetos e conexões do perfil, nunca as pastas
    dos projetos)."""
    base = base or root()
    if profile_id == DEFAULT:
        raise LauncherError("O perfil padrão não pode ser removido.")
    data = _read(base)
    if not any(item["id"] == profile_id for item in data["profiles"]):
        raise LauncherError(f"Perfil não encontrado: {profile_id}")
    data["profiles"] = [item for item in data["profiles"] if item["id"] != profile_id]
    if data["active"] == profile_id:
        data["active"] = DEFAULT
    if delete_data:
        # Antes de tirar da lista: se a pasta (com as chaves cifradas) não puder ser apagada, o perfil continua listado e
        # o pedido pode ser repetido, em vez de a pasta ficar órfã no disco sem aviso.
        folder = base / FOLDER / profile_id
        try:
            remove_tree(folder)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise LauncherError(f"Não foi possível apagar os dados do perfil ({error}); o perfil continua na lista. "
                                f"Feche o que usa {folder} e tente de novo.") from error
    _write(data, base)


def base_of(home: Path) -> Path:
    """Raiz dos perfis a que uma pasta de dados pertence: a avó para <raiz>/profiles/<id>, a própria pasta senão."""
    home = Path(home)
    parent = home.parent
    if parent.name == FOLDER and (parent.parent / FILE).is_file():
        return parent.parent
    return home


def label(home: Path) -> str | None:
    """Nome do perfil para títulos e nomes de provider; None no padrão (nada muda para quem não usa perfis)."""
    profile = profile_of_home(home, base_of(home))
    return None if profile["id"] == DEFAULT else profile["name"]


def profiles_with_path(path: str, base: Path | None = None) -> list[dict]:
    """Perfis que têm a pasta cadastrada como projeto (o histórico dos CLIs é por pasta, então é compartilhado)."""
    base = base or root()
    wanted = path_key(str(path))
    found = []
    for profile in list_profiles(base):
        try:
            data = json.loads((home_of(profile["id"], base) / "config.json").read_text(encoding="utf-8-sig"))
            projects = data.get("projects") or []
        except (OSError, ValueError, AttributeError, LauncherError):
            continue
        if any(isinstance(item, dict) and isinstance(item.get("path"), str) and path_key(item["path"]) == wanted
               for item in projects):
            found.append(profile)
    return found


def other_profiles_with_path(path: str, home: Path) -> list[dict]:
    """Os outros perfis (fora o dono de `home`) que também têm a pasta cadastrada."""
    base = base_of(home)
    current = profile_of_home(home, base)["id"]
    return [item for item in profiles_with_path(path, base) if item["id"] != current]


def shared_warning(path: str, home: Path) -> str | None:
    """Aviso de pasta repetida em outro perfil (None quando a pasta só está neste)."""
    others = other_profiles_with_path(path, home)
    if not others:
        return None
    names = ", ".join(item["name"] for item in others)
    plural = "nos perfis" if len(others) > 1 else "no perfil"
    return f"Esta pasta também está {plural} {names}; o histórico dos CLIs é compartilhado."
