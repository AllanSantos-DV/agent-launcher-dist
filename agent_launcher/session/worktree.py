"""Worktree por sessão: uma pasta e uma branch do projeto só para uma sessão, para sessões paralelas no mesmo projeto não
sobrescreverem a edição uma da outra.

Convenção: o worktree de NOME fica na pasta irmã do projeto, `<pai>/<projeto>.worktrees/NOME`, e a branch é
`launcher/NOME`. Fora da pasta do projeto, o `git status` dele não muda. O launcher cria e reaproveita só o que ele mesmo
terminou de criar: depois do `git worktree add` com sucesso grava o marcador MARKER na pasta administrativa do worktree
(`.git/worktrees/<nome>/`, que o git não toca e que não é working tree). Pasta registrada, na branch esperada, mas sem o
marcador (o `git worktree add` estourou o tempo ou foi interrompido, ou o dono a criou à mão) é recusada como incompleta,
com o que conferir. O launcher nunca remove worktree nem branch (o trabalho feito dentro deles é do dono). Para remover:
`git worktree remove <pasta>` e, se quiser, `git branch -d launcher/NOME`.

Só a biblioteca padrão e o git instalado, sem rede."""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import path_key, project_path

BRANCH_PREFIX = "launcher/"
FOLDER_SUFFIX = ".worktrees"
GIT_TIMEOUT_S = 60
MARKER = "launcher-pronto"
# O que seleciona o repositório pelo ambiente do dono: o git roda sempre no projeto pedido.
_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX",
                         "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))})


@dataclass(frozen=True)
class Worktree:
    """O worktree de uma sessão. `exists`: a pasta já é um worktree do projeto na branch esperada; `created`: esta
    chamada o criou."""
    name: str
    branch: str
    path: Path
    project: Path
    exists: bool = False
    created: bool = False

    def as_json(self) -> dict:
        """O worktree como entra no plano da sessão (`worktree` do `--dry-run`)."""
        return {"name": self.name, "branch": self.branch, "path": str(self.path), "project": str(self.project),
                "exists": self.exists}


def auto_name() -> str:
    """Nome quando o pedido não traz um: `sessao-AAAAMMDD-HHMMSS`, da hora local."""
    return time.strftime("sessao-%Y%m%d-%H%M%S")


def validate_name(name: str) -> str:
    """O nome serve de pasta no Windows e de trecho de branch: letras, números, `.`, `-` e `_` (até 64, começando por
    letra ou número), sem `..`, sem terminar em `.` ou `.lock` e sem nome reservado do Windows."""
    if (not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name or name.endswith((".", ".lock"))
            or name.split(".")[0].lower() in _RESERVED):
        raise LauncherError(f"Nome de worktree inválido: {name!r}. Use até 64 letras, números, ponto, hífen ou sublinhado, "
                            "começando por letra ou número, sem '..', sem terminar em '.' ou '.lock' e sem nome "
                            "reservado do Windows (con, nul, com1...).")
    return name


def worktrees_folder(project: Path) -> Path:
    """A pasta irmã do projeto onde ficam os worktrees dele."""
    return project.parent / (project.name + FOLDER_SUFFIX)


def name_of(project: Path, folder) -> str | None:
    """O NOME do worktree quando `folder` é uma pasta da convenção deste projeto; None para qualquer outra."""
    folder = Path(folder)
    if path_key(folder.parent) == path_key(worktrees_folder(Path(project))) and _NAME.fullmatch(folder.name):
        return folder.name
    return None


def _git(folder: Path, *arguments: str, on_timeout: str = "") -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if key not in _REPOSITORY_VARIABLES}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(["git", "-C", str(folder), *arguments], env=environment, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as error:
        raise LauncherError("git não encontrado: --worktree usa o git instalado. Instale o Git for Windows ou abra a "
                            "sessão sem --worktree.") from error
    except subprocess.TimeoutExpired as error:
        raise LauncherError(f"O git demorou mais de {GIT_TIMEOUT_S} s e foi encerrado (git {' '.join(arguments[:2])})."
                            + (f" {on_timeout}" if on_timeout else "")) from error


def current_branch(folder) -> tuple[bool, str | None]:
    """(é repositório git, ramo atual), lidos do HEAD sem chamar o git. O `.git` de um worktree é um arquivo `gitdir: CAMINHO`
    (relativo à pasta ou absoluto) que aponta a pasta administrativa dele. Cabeça solta: (True, None). Sem `.git`, HEAD
    ilegível ou fora do formato: (False, None), nunca um ramo inventado; `ref: refs/heads/.invalid`, o HEAD fixo que o
    git grava com `extensions.refStorage=reftable` (o ramo real fica nas tabelas), conta como ilegível. Só olha a pasta
    pedida, não as de cima."""
    dot_git = Path(folder) / ".git"
    try:
        if dot_git.is_file():
            first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            if not first or not first[0].startswith("gitdir:"):
                return False, None
            admin = Path(first[0][len("gitdir:"):].strip())
            admin = admin if admin.is_absolute() else dot_git.parent / admin
        elif dot_git.is_dir():
            admin = dot_git
        else:
            return False, None
        head = (admin / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False, None
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    if branch and branch != ".invalid":
        return True, branch
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        return True, None
    return False, None


def _repository_root(folder: Path) -> Path:
    found = _git(folder, "rev-parse", "--show-toplevel")
    if found.returncode:
        reason = found.stderr.strip()
        raise LauncherError(f"A pasta do projeto não é um repositório git: {folder}"
                            + (f" ({reason})" if reason else "")
                            + ". --worktree precisa de um repositório com ao menos um commit; abra a sessão sem "
                              "--worktree ou rode 'git init' no projeto.")
    root = Path(found.stdout.strip())
    if path_key(root) != path_key(folder):
        raise LauncherError(f"O projeto {folder} fica dentro de um repositório git cuja raiz é {root}. --worktree isola o "
                            "repositório inteiro: cadastre a raiz do repositório como projeto.")
    return root


def _listed(folder: Path) -> list[tuple[str, str | None]]:
    """[(pasta como o git a escreve, branch completa ou None)] dos worktrees do repositório, o principal inclusive."""
    listed = _git(folder, "worktree", "list", "--porcelain")
    if listed.returncode:
        raise LauncherError(f"git worktree list falhou: {listed.stderr.strip()}")
    found: list[tuple[str, str | None]] = []
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            found.append((line[len("worktree "):], None))
        elif line.startswith("branch ") and found:
            found[-1] = (found[-1][0], line[len("branch "):])
    return found


def _registered(folder: Path) -> dict[str, str | None]:
    """{pasta (path_key): branch completa ou None} dos worktrees do repositório, o principal inclusive."""
    return {path_key(path): branch for path, branch in _listed(folder)}


def _admin_folder(target: Path) -> Path | None:
    """A pasta administrativa do worktree (`.git/worktrees/<nome>`), None se o git não a acha."""
    found = _git(target, "rev-parse", "--absolute-git-dir")
    return Path(found.stdout.strip()) if found.returncode == 0 and found.stdout.strip() else None


def _incomplete(target: Path, branch: str, project: Path) -> LauncherError:
    return LauncherError(f"O worktree {target} está registrado no git e na branch {branch}, mas não foi terminado pelo "
                         f"launcher (falta o marcador {MARKER}: o 'git worktree add' estourou o tempo ou foi interrompido, "
                         "ou a pasta foi criada à mão), então pode estar incompleto. O launcher não o reaproveita nem o "
                         f"apaga. Confira com 'git -C \"{target}\" status'. Para recriar, remova-o você mesmo: "
                         f"'git -C \"{project}\" worktree remove --force \"{target}\"' e "
                         f"'git -C \"{project}\" branch -D {branch}'; depois repita o comando.")


def _mark_ready(found: Worktree) -> None:
    admin = _admin_folder(found.path)
    try:
        if admin is None:
            raise OSError("pasta administrativa do worktree não encontrada")
        (admin / MARKER).write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n", encoding="utf-8")
    except OSError as error:
        raise LauncherError(f"O worktree {found.path} foi criado, mas o launcher não conseguiu gravar o marcador {MARKER} "
                            f"({error}); ele fica sem uso. Remova-o você mesmo: 'git -C \"{found.project}\" worktree "
                            f"remove --force \"{found.path}\"' e 'git -C \"{found.project}\" branch -D {found.branch}'.") from error


def locate(project_folder, name: str | None = None) -> Worktree:
    """Confere e descreve o worktree de `name` (vazio ou None: `auto_name()`) sem gravar nada. Pasta que já é worktree do
    projeto na branch esperada e que o launcher terminou de criar (marcador MARKER) vem com `exists`. Qualquer outra coisa
    no lugar (pasta alheia, worktree em outra branch, worktree sem o marcador, branch sem worktree) é erro com o que
    fazer; nada é sobrescrito nem apagado."""
    folder = project_path(str(project_folder))
    name = validate_name(name) if name else auto_name()
    _repository_root(folder)
    target = (worktrees_folder(folder) / name).resolve()
    branch = BRANCH_PREFIX + name
    registered = _registered(folder)
    key = path_key(target)
    if target.exists():
        if key not in registered:
            raise LauncherError(f"A pasta {target} já existe e não é um worktree de {folder}. Escolha outro nome para "
                                "--worktree ou mova essa pasta; o launcher não mexe nela.")
        current = registered[key]
        if current != f"refs/heads/{branch}":
            shown = current.removeprefix("refs/heads/") if current else "nenhuma branch (cabeça solta)"
            raise LauncherError(f"O worktree {target} está em {shown}, não em {branch}. Escolha outro nome para "
                                f"--worktree, ou volte a pasta para a branch com 'git -C \"{target}\" switch {branch}'.")
        admin = _admin_folder(target)
        if admin is None or not (admin / MARKER).is_file():
            raise _incomplete(target, branch, folder)
        return Worktree(name, branch, target, folder, exists=True)
    if key in registered:
        raise LauncherError(f"O worktree {target} está registrado no git, mas a pasta sumiu. Rode "
                            f"'git -C \"{folder}\" worktree prune' e tente de novo.")
    if _git(folder, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        raise LauncherError(f"A branch {branch} já existe e não tem worktree. Escolha outro nome para --worktree, ou "
                            f"recrie a pasta com 'git -C \"{folder}\" worktree add \"{target}\" {branch}'.")
    if _git(folder, "rev-parse", "--verify", "--quiet", "HEAD").returncode:
        raise LauncherError(f"O repositório {folder} ainda não tem nenhum commit, e o worktree nasce do HEAD dele. Faça o "
                            "primeiro commit e tente de novo.")
    return Worktree(name, branch, target, folder)


def listing(project_folder) -> list[dict]:
    """Os worktrees do projeto na pasta da convenção (`<projeto>.worktrees/NOME`), em ordem de nome, só leitura. Cada item
    traz `name`, `branch` (curta; None com a cabeça solta), `path`, `exists` (a pasta existe) e `ready` (o marcador MARKER
    está na pasta administrativa; None quando a pasta sumiu e o git não tem como dizer). O worktree principal e os que o
    dono criou fora da pasta da convenção não entram."""
    folder = project_path(str(project_folder))
    if _git(folder, "rev-parse", "--git-dir").returncode:
        raise LauncherError(f"A pasta do projeto não é um repositório git: {folder}. Só há worktrees a listar num "
                            "repositório git.")
    found = []
    for raw, branch in _listed(folder):
        target = Path(raw)
        name = name_of(folder, target)
        if name is None:
            continue
        exists = target.is_dir()
        admin = _admin_folder(target) if exists else None
        found.append({"name": name, "branch": branch.removeprefix("refs/heads/") if branch else None,
                      "path": str(target), "exists": exists,
                      "ready": (admin is not None and (admin / MARKER).is_file()) if exists else None})
    return sorted(found, key=lambda item: item["name"].casefold())


def create(found: Worktree) -> Worktree:
    """Cria o worktree de `locate` (branch nova a partir do HEAD do projeto); se ele já existe, devolve-o como está."""
    if found.exists:
        return found
    made = _git(found.project, "worktree", "add", "-b", found.branch, str(found.path),
                on_timeout=f"Pode ter ficado a pasta {found.path} incompleta, registrada no git, e a branch {found.branch}; "
                           "o launcher não apaga nada. Para recomeçar, confira e remova você mesmo: "
                           f"'git -C \"{found.project}\" worktree remove --force \"{found.path}\"' e "
                           f"'git -C \"{found.project}\" branch -D {found.branch}'; depois repita o comando.")
    if made.returncode:
        raise LauncherError(f"git worktree add falhou em {found.project}: {made.stderr.strip() or made.stdout.strip()}")
    _mark_ready(found)
    return replace(found, exists=True, created=True)


def ensure(project_folder, name: str | None = None) -> Worktree:
    """O worktree de `name`, criado se não existir e reaproveitado se o launcher o terminou de criar (na branch esperada)."""
    return create(locate(project_folder, name))
