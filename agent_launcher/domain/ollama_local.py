"""Achar o Ollama deste computador e cadastrar a conexão. Só roda quando o usuário pede (botão ou `connections detect`);
nada sonda a rede por conta própria. Reaproveita o conector `ollama` (`inspect`: lista de modelos mais `/api/show` de cada um), então a conferência é a
mesma que a conexão cadastrada faz."""
from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit

from agent_launcher.connectors import ConnectorError, ollama
from agent_launcher.domain.catalog import ModelInfo, token_label
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.store import Store

CONNECTION_NAME = "Ollama (local)"


class Detection(NamedTuple):
    """Resultado da procura: `error` vazio só quando o Ollama respondeu e tem ao menos um modelo; `notes` diz o que o
    host informou (e o que não informou) sobre ferramentas e janela de contexto."""
    address: str
    models: tuple[ModelInfo, ...]
    notes: tuple[str, ...]
    error: str

    @property
    def found(self) -> bool:
        """True quando o Ollama respondeu e tem modelo, ou seja, a conexão pode ser cadastrada."""
        return not self.error


def _notes(records: list[ModelInfo], context_confirmed: bool, windows: dict[str, int],
           failed: list[str] = ()) -> tuple[str, ...]:
    count = len(records)
    chat = [record for record in records if record.mode != "other"]
    chat_count = len(chat)
    with_tools = sum(1 for record in chat if record.tools)
    informed_tools = any(record.tools is not None for record in chat)
    vision = sum(1 for record in chat if record.vision)
    chat_windows = {name: value for name, value in windows.items() if name in {record.id for record in chat}}
    return (
        f"{count} {'modelo' if count == 1 else 'modelos'} disponíveis",
        f"ferramentas: {with_tools} de {chat_count} {'modelo' if chat_count == 1 else 'modelos'}" if informed_tools
        else "ferramentas: o Ollama não informa",
        *((f"imagem: {vision} {'modelo' if vision == 1 else 'modelos'}",) if vision else ()),
        *((ollama.unanswered_note(failed).replace("Atualize o catálogo (r)", "Procure de novo"),) if failed else ()),
        _window_note(chat_windows) if chat_windows
        else ("janela de contexto: informada pelo host" if context_confirmed
              else "janela de contexto: não informada; o Aider pede 16384 (num_ctx)"),
        "streaming: não conferido; só uma chamada de modelo prova",
    )


def _window_note(windows: dict[str, int]) -> str:
    """Máximo dos modelos, com o aviso de que o Ollama carrega menos por padrão (4096 no 0.32.3): quem passa o
    `num_ctx` (o Aider) controla a janela; os demais herdam a do servidor (`OLLAMA_CONTEXT_LENGTH`)."""
    low, high = (token_label(value // 1000 * 1000) for value in (min(windows.values()), max(windows.values())))
    return (f"janela máxima dos modelos: {low} a {high}; "
            "o Ollama carrega menos por padrão (ajuste OLLAMA_CONTEXT_LENGTH); o Aider pede 16384 (num_ctx)")


def detect(address: str = ollama.EXAMPLE) -> Detection:
    """Procura o Ollama em `address` (vazio = endereço padrão). Nunca levanta: a ausência, a falha e o Ollama sem modelo
    voltam em `error`, com o motivo e o que fazer."""
    typed = (address or "").strip()
    if "@" in typed:
        return Detection("", (), (), "O endereço do Ollama é só http://servidor:porta, sem usuário, senha nem parâmetros: "
                                     "o Ollama local não usa chave.")
    options = ollama._root({"base_url": typed or ollama.EXAMPLE})
    root = options["base_url"]
    try:
        parts = urlsplit(root)
        credentials = parts.username is not None or any(mark in root for mark in "?#;")
    except ValueError:
        return Detection("", (), (), "Endereço inválido: use a forma http://127.0.0.1:11434.")
    if credentials or parts.query or parts.fragment:
        return Detection("", (), (), "O endereço do Ollama é só http://servidor:porta, sem usuário, senha nem parâmetros: "
                                     "o Ollama local não usa chave.")
    try:
        records, context_confirmed, windows, failed = ollama.inspect(options)
    except ConnectorError as error:
        return Detection(root, (), (), f"O Ollama não respondeu em {root}: {error}\n"
                                       "O launcher segue funcionando com as outras conexões. Para usar modelos locais, instale "
                                       "o Ollama (ollama.com), deixe-o aberto e procure de novo, ou informe outro endereço.")
    if not records:
        return Detection(root, (), (), f"O Ollama responde em {root}, mas não tem nenhum modelo baixado. Baixe um, por "
                                       "exemplo `ollama pull qwen3:4b`, e procure de novo.")
    return Detection(root, tuple(records), _notes(records, context_confirmed, windows, failed), "")


def register(store: Store, detection: Detection, name: str = CONNECTION_NAME) -> tuple[dict, bool]:
    """Cadastra a conexão `ollama` do endereço achado (testa antes de gravar). O nome que já existe é atualizado.
    Devolve (conexão, atualizada)."""
    if not detection.found:
        raise LauncherError(detection.error)
    same_name = next((item for item in store.connections if item["name"].casefold() == name.casefold()), None)
    if same_name and same_name["connector"] != ollama.NAME:
        raise LauncherError(f"Já existe uma conexão chamada \"{same_name['name']}\" de outro conector "
                            f"({same_name['connector']}). Escolha outro nome com --name ou renomeie a existente.")
    existing = same_name["id"] if same_name else None
    kept = same_name["options"] if same_name else {}
    connection = store.save_connection(name, ollama.NAME, {**kept, "base_url": detection.address}, existing)
    return connection, existing is not None
