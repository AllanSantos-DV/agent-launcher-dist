"""Pacote de perfil `.alprofile`: um zip que a TI distribui para criar um perfil pronto.

    perfil.json      {"schema": 1, "name", "color", "connections": [{"name", "connector", "options"}],
                      "default_connection"}
    connectors/*.py  conectores do usuário que as conexões do perfil usam (opcional)

Nunca contém chave: campo secreto do conector (FIELDS com "secret") ou opção com nome de chave (api_key, key, token,
secret...) é recusado na exportação e na importação. A importação valida tudo antes de gravar e desfaz o que gravou
se algo falhar no meio (perfil criado e conectores novos ou substituídos)."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from agent_launcher import connectors
from agent_launcher.domain import profiles
from agent_launcher.domain.files import replace_retrying
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.store import Store

SCHEMA = 1
EXTENSION = ".alprofile"
MANIFEST = "perfil.json"
MAX_TOTAL = 5 * 1024 * 1024
MAX_FILES = 200
CONNECTOR_FILE = re.compile(r"connectors/([A-Za-z0-9_-]{1,60})\.py")
SECRET_NAMES = {"api_key", "apikey", "key", "token", "secret", "password", "senha", "access_token", "bearer"}
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
SECRET_MESSAGE = "O pacote traz uma chave ({}); peça à TI um pacote sem chave."


def _no_ask(kind: str, *args):
    return None  # replace_connector -> mantém o instalado; secret -> sem chave


def _secret_fields(module) -> set[str]:
    return {item["name"] for item in getattr(module, "FIELDS", ()) if item.get("secret")}


def _suspicious(name: str) -> bool:
    return str(name).strip().casefold() in SECRET_NAMES


def _user_connector_files(base: Path) -> dict[str, Path]:
    """{NAME: arquivo .py} dos conectores do usuário (na raiz, globais) que carregam sem erro."""
    folder = base / "connectors"
    modules, _errors = connectors.available(folder)
    builtin = {module.NAME for module in connectors.BUILTIN}
    found = {}
    for name, module in modules.items():
        origin = getattr(module, "__file__", None)
        if name not in builtin and origin and Path(origin).parent.resolve() == folder.resolve():
            found[name] = Path(origin)
    return found


def export_profile(base: Path | None, profile_id: str, out_path: Path, include_connectors: bool = True) -> dict:
    """Grava o .alprofile do perfil (sem chaves). Devolve {"path", "connections", "connectors"}."""
    base = Path(base) if base else profiles.root()
    profile = profiles.find(profile_id, base)
    store = Store(profiles.home_of(profile["id"], base))
    modules, _errors = store.connectors()
    entries = []
    for connection in store.connections:
        hidden = _secret_fields(modules.get(connection["connector"]))
        options = {key: value for key, value in connection["options"].items()
                   if key not in hidden and not _suspicious(key)}
        entries.append({"name": connection["name"], "connector": connection["connector"], "options": options})
    default = next((c["name"] for c in store.connections if c["id"] == store.data["default_connection"]), None)
    manifest = {"schema": SCHEMA, "name": profile["name"], "color": profile.get("color"), "connections": entries,
                "default_connection": default}
    files = {}
    if include_connectors:
        user = _user_connector_files(base)
        for name in sorted({entry["connector"] for entry in entries}):
            if name in user:
                files[f"connectors/{user[name].name}"] = user[name].read_bytes()
    out_path = Path(out_path)
    if out_path.suffix.lower() != EXTENSION:
        out_path = out_path.with_name(out_path.name + EXTENSION)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + ".tmp")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, data in files.items():
            archive.writestr(name, data)
    replace_retrying(partial, out_path)
    return {"path": str(out_path), "connections": [entry["name"] for entry in entries],
            "connectors": sorted(files)}


def read_package(path: Path) -> tuple[dict, dict[str, bytes]]:
    """Abre e valida o pacote sem gravar nada: (perfil.json, {arquivo do conector: conteúdo})."""
    try:
        archive = zipfile.ZipFile(Path(path))
    except (OSError, zipfile.BadZipFile) as error:
        raise LauncherError(f"Não foi possível abrir o pacote de perfil {path}: não é um .alprofile válido.") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise LauncherError("Pacote de perfil com arquivos demais.")
        if sum(info.file_size for info in infos) > MAX_TOTAL:
            raise LauncherError("Pacote de perfil acima de 5 MB.")
        manifest_bytes, files = None, {}
        for info in infos:
            name = info.filename
            if "\\" in name or name.startswith("/") or ".." in name.split("/") or ":" in name:
                raise LauncherError(f"Pacote de perfil com caminho inválido: {name!r}.")
            if info.is_dir():
                if name.rstrip("/") != "connectors":
                    raise LauncherError(f"Pacote de perfil com pasta inesperada: {name!r}.")
                continue
            if name == MANIFEST:
                manifest_bytes = archive.read(info)
            elif CONNECTOR_FILE.fullmatch(name):
                files[name] = archive.read(info)
            else:
                raise LauncherError(f"Pacote de perfil com arquivo inesperado: {name!r} (só {MANIFEST} e connectors/*.py).")
        if sum(len(data) for data in files.values()) + len(manifest_bytes or b"") > MAX_TOTAL:
            raise LauncherError("Pacote de perfil acima de 5 MB.")
    if manifest_bytes is None:
        raise LauncherError(f"Pacote de perfil sem {MANIFEST}.")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise LauncherError(f"{MANIFEST} do pacote não é JSON válido.") from error
    _validate(manifest)
    return manifest, files


def _validate(manifest) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise LauncherError(f"{MANIFEST}: formato não suportado (schema {SCHEMA} esperado).")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise LauncherError(f"{MANIFEST}: falta o nome do perfil.")
    color = manifest.get("color")
    if color is not None and not (isinstance(color, str) and COLOR.fullmatch(color)):
        raise LauncherError(f"{MANIFEST}: cor inválida {color!r}.")
    entries = manifest.get("connections", [])
    if not isinstance(entries, list):
        raise LauncherError(f"{MANIFEST}: connections precisa ser uma lista.")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip() \
                or not isinstance(entry.get("connector"), str) or not re.fullmatch(r"[\w-]{1,40}", entry["connector"]):
            raise LauncherError(f"{MANIFEST}: conexão inválida {entry!r}.")
        options = entry.get("options", {})
        if not isinstance(options, dict) or not all(isinstance(key, str) and type(value) in (str, bool, int, float)
                                                    for key, value in options.items()):
            raise LauncherError(f"{MANIFEST}: opções inválidas na conexão {entry['name']}.")
        for key in options:
            if _suspicious(key):
                raise LauncherError(SECRET_MESSAGE.format(f"{entry['name']}: {key}"))
        if entry["name"].casefold() in names:
            raise LauncherError(f"{MANIFEST}: conexão repetida {entry['name']}.")
        names.add(entry["name"].casefold())
    default = manifest.get("default_connection")
    if default is not None and (not isinstance(default, str) or default.casefold() not in names):
        raise LauncherError(f"{MANIFEST}: conexão padrão {default!r} não está na lista.")


def _free_name(name: str, base: Path) -> str:
    taken = {item["name"].casefold() for item in profiles.list_profiles(base)}
    name = name.strip()[:55]
    if name.casefold() not in taken:
        return name
    number = 2
    while f"{name} ({number})".casefold() in taken:
        number += 1
    return f"{name} ({number})"


def _needs_key(error: Exception) -> bool:
    text = str(error)
    return any(marker in text for marker in ("HTTP 401", "HTTP 403", "recusou a chave", "exige uma chave"))


def import_profile(base: Path | None, path: Path, ask=None, verify: bool = True) -> dict:
    """Cria o perfil do pacote. `ask(kind, *args)`:
      ask("replace_connector", nome_do_arquivo) -> True substitui o conector instalado diferente; senão mantém;
      ask("secret", conexão, campo) -> a chave digitada, ou None (conexão fica sem chave e sem teste, com aviso).
    Devolve {"profile": {...}, "connections": [nomes], "warnings": [...]}. Falha no meio desfaz tudo."""
    base = Path(base) if base else profiles.root()
    ask = ask or _no_ask
    manifest, files = read_package(path)
    folder = base / "connectors"
    warnings = []
    # Os conectores do pacote carregam numa pasta temporária antes de qualquer gravação (erro de carga = recusa).
    with tempfile.TemporaryDirectory(prefix="al-pacote-") as temp:
        for name, data in files.items():
            (Path(temp) / Path(name).name).write_bytes(data)
        loaded, errors = connectors.available(Path(temp))
        if errors:
            raise LauncherError("Conector do pacote não carrega: " + "; ".join(errors))
    packaged = {module.NAME: module for module in loaded.values() if module not in connectors.BUILTIN}
    builtin = {module.NAME: module for module in connectors.BUILTIN}
    installed, _errors = connectors.available(folder)
    for entry in manifest.get("connections", []):
        module = packaged.get(entry["connector"]) or installed.get(entry["connector"]) or builtin.get(entry["connector"])
        if module is None:
            raise LauncherError(f"A conexão {entry['name']} usa o conector {entry['connector']!r}, que não veio no "
                                "pacote nem está instalado.")
        for key in entry.get("options", {}):
            if key in _secret_fields(module):
                raise LauncherError(SECRET_MESSAGE.format(f"{entry['name']}: {key}"))
    # Decide o que gravar na raiz: novo, igual (nada a fazer) ou diferente (pergunta).
    writes = {}
    for name, data in files.items():
        destination = folder / Path(name).name
        if destination.is_file():
            if destination.read_bytes() == data:
                continue
            if not ask("replace_connector", destination.stem):
                warnings.append(f"Conector {destination.stem} mantido como já estava instalado (o do pacote é diferente).")
                continue
        writes[destination] = data
    created_profile = None
    previous = {}
    try:
        for destination, data in writes.items():
            previous[destination] = destination.read_bytes() if destination.is_file() else None
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        created_profile = profiles.create(_free_name(manifest["name"], base), base)
        if manifest.get("color"):
            created_profile = profiles.set_color(created_profile["id"], manifest["color"], base)
        store = Store(profiles.home_of(created_profile["id"], base))
        modules, load_errors = store.connectors()
        names = []
        for entry in manifest.get("connections", []):
            module = modules.get(entry["connector"])
            if module is None:
                raise LauncherError(f"Conector {entry['connector']!r} não carregou: {'; '.join(load_errors)}")
            names.append(_create_connection(store, module, entry, ask, verify, warnings)["name"])
        if manifest.get("default_connection"):
            store.set_default_connection(manifest["default_connection"])
    except BaseException:
        if created_profile is not None:
            try:
                profiles.remove(created_profile["id"], True, base)
            except (LauncherError, OSError):
                shutil.rmtree(base / profiles.FOLDER / created_profile["id"], ignore_errors=True)
        for destination, data in previous.items():
            try:
                if data is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.write_bytes(data)
                for compiled in (destination.parent / "__pycache__").glob(f"{destination.stem}.*.pyc"):
                    compiled.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {"profile": created_profile, "connections": names, "warnings": warnings}


def _create_connection(store: Store, module, entry: dict, ask, verify: bool, warnings: list) -> dict:
    name, values = entry["name"], dict(entry.get("options", {}))
    secrets = [item for item in module.FIELDS if item.get("secret")]
    required = [item for item in secrets if item.get("required")]
    if required:
        for item in required:
            key = ask("secret", name, item["label"])
            if not key:
                warnings.append(f"Conexão {name} criada sem chave ({item['label']}): edite-a em Conexões para "
                                "informar a chave.")
                return _without_key(store, module, name, entry["connector"], values)
            values[item["name"]] = str(key)
        return store.save_connection(name, entry["connector"], values, verify=verify)
    try:
        return store.save_connection(name, entry["connector"], values, verify=verify)
    except LauncherError as error:
        if not secrets or not _needs_key(error):
            raise
    item = next((field for field in secrets if field["name"] == "api_key"), secrets[0])
    key = ask("secret", name, item["label"])
    if not key:
        warnings.append(f"Conexão {name} criada sem chave e sem teste: o host pediu uma chave ({item['label']}). "
                        "Edite-a em Conexões para informar.")
        return store.save_connection(name, entry["connector"], values, verify=False)
    values[item["name"]] = str(key)
    return store.save_connection(name, entry["connector"], values, verify=verify)


def _without_key(store: Store, module, name: str, connector: str, values: dict) -> dict:
    """Conexão cujo campo secreto é obrigatório, criada sem a chave (o usuário preenche depois em Conexões)."""
    import uuid
    plain = {}
    for item in module.FIELDS:
        if item.get("secret"):
            continue
        value = values.get(item["name"])
        if item.get("type") == "bool":
            plain[item["name"]] = bool(value if value is not None else item.get("default", False))
        else:
            plain[item["name"]] = str(value if value is not None else item.get("default", "")).strip()
    if any(c["name"].casefold() == name.casefold() for c in store.data["connections"]):
        raise LauncherError("Já existe uma conexão com esse nome.")
    connection = {"id": str(uuid.uuid4()), "name": name, "connector": connector, "options": plain}
    store.data["connections"].append(connection)
    if store.data["default_connection"] is None:
        store.data["default_connection"] = connection["id"]
    store.save()
    return connection


def describe(result: dict) -> str:
    lines = [f"Perfil importado: {result['profile']['name']} ({len(result['connections'])} conexão(ões))."]
    lines += [f"Aviso: {warning}" for warning in result["warnings"]]
    return "\n".join(lines)


__all__ = ["export_profile", "import_profile", "read_package", "describe", "EXTENSION"]
