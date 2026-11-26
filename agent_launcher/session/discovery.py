"""Descoberta de pastas de projeto no histórico de conversas dos CLIs de terminal (`launcher projects discover`).

Cada adaptador diz de que pasta é cada conversa do seu histórico (`Harness.history`): o Codex e o Copilot CLI gravam o `cwd`;
o Claude Code guarda por pasta cujo nome troca todo caractere não alfanumérico por `-`, então o caminho real só vale quando uma
linha do transcript traz um `cwd` que, codificado, dá exatamente esse nome. Pasta sem caminho confirmado aparece como "não
recuperado" e nunca é cadastrada. Só leitura; `register` é o único que grava, e só o que `discover` marcou como cadastrável."""
from __future__ import annotations

from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import path_key, project_path, protected_folder
from agent_launcher.domain.store import Store
from agent_launcher.harnesses import registry
from agent_launcher.session import worktree

UNRECOVERED = ("caminho não recuperado: nenhuma linha das conversas traz o caminho real que confirme o nome da pasta do "
               "histórico; cadastre com 'projects add CAMINHO'")


def _resolved(folder: str) -> tuple[str, bool]:
    """(o caminho como o cadastro o guarda, a pasta existe) para um `cwd` do histórico."""
    try:
        return str(project_path(folder)), True
    except LauncherError:
        return folder, False


def _group(cli: str | None) -> dict[str, dict]:
    """Uma entrada por pasta (ou, sem caminho confirmado, por pasta do histórico): CLIs, nº de conversas e a mais recente."""
    groups: dict[str, dict] = {}
    for harness in registry.terminals():
        if cli is not None and harness.ID != cli:
            continue
        for item in harness.history():
            path, exists = _resolved(item.folder) if item.folder else (None, False)
            key = path_key(path) if path else f"{harness.ID}:{item.label}"
            group = groups.setdefault(key, {"path": path, "exists": exists, "clis": [], "count": 0, "last": None,
                                            "history_folder": None if path else item.label})
            if harness.ID not in group["clis"]:
                group["clis"].append(harness.ID)
            group["count"] += 1
            if group["last"] is None or item.conversation.updated > group["last"].updated:
                group["last"] = item.conversation
    return groups


def _refusal(path: str | None, exists: bool) -> str | None:
    """Por que a pasta não é cadastrada por `--add` (mesmas recusas do `--add-here`); None quando serve."""
    if path is None:
        return UNRECOVERED
    if not exists:
        return "a pasta não existe mais no disco"
    protected = protected_folder(Path(path))
    if protected:
        return f"não é cadastrada sozinha: é {protected}; cadastre com 'projects add CAMINHO' se for isso mesmo"
    if Path(path).parent.name.endswith(worktree.FOLDER_SUFFIX):
        return "worktree do launcher: as conversas dele pertencem ao projeto de origem, que é o que se cadastra"
    return None


def discover(store: Store, cli: str | None = None) -> list[dict]:
    """As pastas do histórico de conversas (de `cli`, ou dos três CLIs), a mais recente primeiro. Cada item: `name` (o nome da
    pasta, sugestão de nome do projeto), `path`, `clis`, `conversations`, `last_conversation`, `registered`, `addable` (o
    `--add` o cadastraria) e `reason` (por que não, ou None); `history_folder` só quando o caminho não foi recuperado (aí
    `name` e `path` são None). Nome igual ao de projeto já cadastrado, ou ao de uma pasta mais recente da mesma lista, torna
    a pasta não cadastrável com o que fazer. Não grava nada."""
    registered = {path_key(project["path"]) for project in store.data["projects"]}
    taken = {project["name"].casefold() for project in store.data["projects"]}
    groups = sorted(_group(cli).values(), key=lambda group: group["last"].updated, reverse=True)
    rows = []
    for group in groups:
        path, name = group["path"], (Path(group["path"]).name or group["path"]) if group["path"] else None
        is_registered = bool(path) and path_key(path) in registered
        reason = None if is_registered else _refusal(path, group["exists"])
        if reason is None and not is_registered and name.casefold() in taken:
            reason = (f"já existe um projeto chamado {name!r}; cadastre esta pasta com "
                      f"'projects add \"{path}\" --name OUTRO NOME'")
        addable = path is not None and not is_registered and reason is None
        if addable:
            taken.add(name.casefold())
        row = {"name": name, "path": path, "clis": [harness.ID for harness in registry.terminals()
                                                    if harness.ID in group["clis"]],
               "conversations": group["count"], "last_conversation": group["last"].as_json()["updated"],
               "registered": is_registered, "addable": addable, "reason": reason}
        if group["history_folder"]:
            row["history_folder"] = group["history_folder"]
        rows.append(row)
    return rows


def register(store: Store, rows: list[dict]) -> int:
    """Cadastra, com o nome da pasta, as pastas que `discover` marcou como cadastráveis e grava uma vez; cada item ganha
    `added`. Recusa do cadastro (nome inválido, por exemplo) vira `reason` do item e não interrompe os outros. Devolve quantos
    foram cadastrados; sem nenhum, o config.json nem é regravado."""
    added = 0
    for row in rows:
        row["added"] = False
        if not row["addable"]:
            continue
        try:
            store.add(row["path"], save=False)
        except LauncherError as error:
            row["addable"], row["reason"] = False, str(error)
            continue
        row["added"] = True
        added += 1
    if added:
        store.save()
    return added
