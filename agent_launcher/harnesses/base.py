"""Contrato dos adaptadores de destino."""
from __future__ import annotations

import datetime
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.domain.catalog import Catalog, ModelInfo
from agent_launcher.domain.errors import LauncherError


HEAD_LINES = 200          # linhas lidas do começo de cada conversa (título, início, mensagens de arquivos pequenos)
COUNT_LIMIT = 4_000_000   # bytes: até aqui as mensagens são contadas linha a linha; acima, varredura por marcador
SCAN_BLOCK = 4 * 1024 * 1024  # bytes lidos por vez na varredura de arquivo grande
SCAN_SECONDS = 2.0        # tempo máximo da varredura de um arquivo grande; estourou = messages=None
TITLE_LENGTH = 80


@dataclass
class Conversation:
    """Conversa de um CLI de terminal numa pasta. started/updated em segundos (epoch);
    messages = mensagens do usuário (None quando o arquivo é grande demais para contar barato)."""
    id: str
    cli: str
    started: float
    updated: float
    title: str
    messages: int | None

    def as_json(self) -> dict:
        def moment(value: float) -> str:
            return datetime.datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
        return {"id": self.id, "cli": self.cli, "started": moment(self.started), "updated": moment(self.updated),
                "title": self.title, "messages": self.messages}


def conversation_title(text) -> str:
    """Primeira mensagem do usuário em uma linha, até 80 caracteres (sem colagem entre colchetes do terminal)."""
    text = re.sub(r"\x1b?\[20[01]~", "", str(text or ""))
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())
    return text if len(text) <= TITLE_LENGTH else text[:TITLE_LENGTH - 1].rstrip() + "…"


def parse_time(value, default: float = 0.0) -> float:
    """ISO 8601 (com Z) dos arquivos dos CLIs -> epoch; inválido = default."""
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    try:
        return datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def head_lines(path: Path, limit: int = HEAD_LINES) -> list[str]:
    """Até `limit` linhas do começo do arquivo ([] se não der para ler)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return [line for _number, line in zip(range(limit), stream)]
    except OSError:
        return []


_COUNTED: dict = {}  # (caminho, tamanho, mtime_ns, marcador) -> contagem de arquivo grande (a lista é pedida várias vezes)


def scan_marked_lines(path: Path, marker: bytes, accept, seconds: float = SCAN_SECONDS) -> int | None:
    """Arquivo grande: lê em blocos binários e só olha as linhas que contêm `marker` (sem quebrar o resto em linhas
    nem decodificar JSON). Cada linha achada passa por `accept`. None se passar de `seconds` ou não der para ler."""
    deadline = time.monotonic() + seconds
    count, tail = 0, b""
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(SCAN_BLOCK)
                if not block:
                    break
                if time.monotonic() > deadline:
                    return None
                block = tail + block
                cut = block.rfind(b"\n") + 1
                block, tail = block[:cut], block[cut:]
                found = block.find(marker)
                while found >= 0:
                    start = block.rfind(b"\n", 0, found) + 1
                    end = block.find(b"\n", found)
                    if accept(block[start:end]):
                        count += 1
                    found = block.find(marker, end)
    except OSError:
        return None
    if marker in tail and accept(tail):
        count += 1
    return count


def count_lines(path: Path, accept, marker: bytes | None = None) -> int | None:
    """Conta as linhas (bytes) que `accept` aceita, sem decodificar JSON. Arquivo acima de COUNT_LIMIT: com `marker`
    (trecho que toda linha aceita contém), varredura por blocos com limite de tempo e cache por tamanho/mtime; sem
    `marker`, None. None também em arquivo ilegível."""
    try:
        status = path.stat()
        if status.st_size <= COUNT_LIMIT:
            with path.open("rb") as stream:
                return sum(1 for line in stream if accept(line))
    except OSError:
        return None
    if not marker:
        return None
    key = (str(path), status.st_size, status.st_mtime_ns, marker)
    if key not in _COUNTED:
        if len(_COUNTED) > 2000:
            _COUNTED.clear()
        result = scan_marked_lines(path, marker, accept)
        if result is None:
            return None  # estourou o tempo: não guarda (outra vez pode caber, com o arquivo no cache do disco)
        _COUNTED[key] = result
    return _COUNTED[key]


def same_folder(value, directory) -> bool:
    return bool(value) and os.path.normcase(os.path.normpath(str(value))) == os.path.normcase(os.path.normpath(str(directory)))


class Harness:
    ID = ""                         # "claude", "codex", "copilot", "vscode", "copilot_app"
    LABEL = ""                      # "Claude Code"
    KIND = "terminal"               # "terminal" (janela de sessão), "ide" ou "app"
    CONSUMES: tuple = ()            # protocolos aceitos, em ordem de preferência
    CAPABILITIES = frozenset()      # "resume", "controlled", "keys", "bypass", "context_window", "families"
    SUBTITLE = ""                   # linha de baixo do botão na interface
    SYMBOL = "terminal"             # ícone do botão (ui/theme.PATHS)
    TAB_COLOR = "#286046"           # cor da aba no Windows Terminal
    BYPASS_RISK = "Ignora as verificações de permissão."
    BYPASS_HINT = "Sem pedir permissão"
    PREVIEW = False                 # destino em prévia (o fabricante ainda chama de preview)
    SESSION_HINT = "Terminal com título do projeto"
    MISSING_HINT = ""               # como instalar, quando o launcher não instala sozinho
    SCRUB = frozenset()             # variáveis herdadas que saem do ambiente da sessão (além das genéricas)
    SCRUB_PREFIXES: tuple = ()
    EXECUTABLE = ""                 # nome no PATH

    # Instalação

    def native_paths(self) -> list[Path]:
        return []

    def find_executable(self) -> str | None:
        """Executável do destino: o do PATH, senão o da instalação nativa por usuário."""
        found = shutil.which(self.EXECUTABLE) if self.EXECUTABLE else None
        if found:
            return found
        return next((str(path) for path in self.native_paths() if path.is_file()), None)

    def install_steps(self) -> list:
        """[(rótulo, função sem argumentos)] em ordem; vazio = o launcher não instala este destino."""
        return []

    # Conexão e modelos

    def available_for(self, protocols: dict) -> bool:
        """O destino aparece para a conexão se ela serve algum protocolo que ele consome."""
        return any(protocols.get(protocol) for protocol in self.CONSUMES)

    def eligible(self, record: ModelInfo, catalog: Catalog) -> bool:
        return record.chat

    def model_id(self, requested: str, selected: ModelInfo, catalog: Catalog) -> str:
        """ID que vai para o destino (o Claude acrescenta [1m]); levanta LauncherError se o pedido não vale."""
        if requested != selected.id:
            raise LauncherError(f"{requested} não é um ID de modelo do {self.LABEL}. Use o ID da lista, sem sufixos.")
        return selected.id

    def describe_model(self, record: ModelInfo, catalog: Catalog) -> tuple[str, str]:
        """(linha de contexto, linha de detalhe) mostradas na interface para o modelo escolhido."""
        return "", "Contexto informado pelo host."

    # Sessão

    def command(self) -> list[str]:
        raise NotImplementedError

    def connection_url(self, gateway) -> str:
        """URL da conexão que este destino usa (a do primeiro protocolo que ele consome e a conexão serve)."""
        return next((gateway.protocols[protocol] for protocol in self.CONSUMES if gateway.protocols.get(protocol)), "")

    def target_url(self, context) -> str:
        """URL do host usada pela sessão (para decidir o NO_PROXY)."""
        return self.connection_url(context.gateway)

    def open(self, store, project: dict, gateway, model: str, catalog, confirm=lambda question: True) -> str:
        """Destinos que não são de terminal (IDE, app): abre e devolve a mensagem para o rodapé da interface.
        confirm(pergunta) -> bool: a interface pergunta ao usuário antes de um efeito que ele precisa autorizar."""
        raise LauncherError(f"O {self.LABEL} abre em janela de terminal.")

    def plan(self, context):
        raise NotImplementedError

    def prepare(self, plan) -> None:
        """Efeitos antes de abrir (confiança da pasta, perfil...). Falha visível, nunca silenciosa."""

    def final_command(self, plan, directory: Path) -> tuple[list[str], str]:
        """Comando final, com arquivos temporários em `directory` se o destino precisar; (comando, aviso)."""
        return list(plan.command), ""

    def run(self, plan, project: dict) -> int:
        from agent_launcher.session.plan import run_plan
        return run_plan(plan)

    def has_previous_conversation(self, directory: Path) -> bool:
        return False

    def conversations(self, directory: Path) -> list[Conversation]:
        """Conversas desta pasta que o destino retoma, mais recente primeiro. Só lê arquivos (não roda o CLI)."""
        return []

    def dry_run_extra(self, plan) -> dict:
        return {}


def node_script(path: Path, package: str) -> list[str] | None:
    """Pacote npm (shim .cmd/.ps1): roda o .js com o node, nunca por cmd /c."""
    script = next((candidate for candidate in (path.parent / "node_modules" / package, path.parent.parent / package)
                   if candidate.is_file()), None)
    if script is None:
        return None
    node = str(path.parent / "node.exe") if (path.parent / "node.exe").is_file() else shutil.which("node.exe")
    if not node:
        raise LauncherError("Node.js não encontrado para executar o CLI instalado via npm.")
    return [node, str(script)]


def native_wrapper(path: Path) -> list[str] | None:
    """Wrapper .cmd de uma linha que só chama um .exe com %*: roda o .exe direto."""
    if path.suffix.lower() not in (".cmd", ".bat"):
        return None
    wrapper = path.read_text(encoding="utf-8-sig")
    native = re.fullmatch(r'\s*@?echo off\s*\n\s*"([^"\r\n]+\.exe)"\s+%\*\s*', wrapper, re.IGNORECASE)
    if native:
        target = Path(native.group(1))
        if target.is_absolute() and target.is_file():
            return [str(target)]
    return None


def unrecognized(path: Path) -> LauncherError:
    return LauncherError(
        f"Wrapper não reconhecido: {path}. Use uma instalação nativa (.exe) ou npm padrão. "
        "Por segurança, o launcher não executa comandos via cmd /c."
    )


def is_exe(path: Path) -> bool:
    return os.name != "nt" or path.suffix.lower() == ".exe"
