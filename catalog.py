from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,199}(?:\[1m\])?\Z")
CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")
CLIS = ("claude", "codex")
NON_CHAT = re.compile(r"(?:^|[-./:])(?:image|realtime|audio|transcribe|tts|embedding|embed|rerank|moderation|search)(?:[-./:]|$)")
VENDOR = re.compile(r"(?:^|[./])(?:claude-|gpt-|o[134](?:[-.]|$)|codex)", re.IGNORECASE)


def base_model(model: str) -> str:
    return model.removesuffix("[1m]")


def native_cli(model: str) -> str:
    """CLI de origem do modelo pelo nome: claude-* é do Claude Code, gpt-*/o*/codex do Codex; o resto só via roteador."""
    lowered = model.lower()
    if re.search(r"(?:^|[./])claude-", lowered):
        return "claude"
    if re.search(r"(?:^|[./])(?:gpt-|o[134](?:[-.]|$)|codex)", lowered):
        return "codex"
    return ""


def positive_integer(value: object) -> int | None:
    if type(value) is int and 0 < value <= 100_000_000:
        return value
    return None


def token_label(value: int | None) -> str:
    if value is None:
        return "não informada"
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1000:
        return f"{value / 1000:g}k"
    return str(value)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    mode: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    tools: bool | None = None
    responses: bool | None = None

    @classmethod
    def from_cache(cls, value: dict) -> ModelInfo:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not MODEL_PATTERN.fullmatch(value["id"]):
            raise ValueError("Modelo inválido no cache.")
        if value["id"] != base_model(value["id"]):
            raise ValueError("O cache deve conter IDs sem sufixos do CLI.")
        return cls(
            id=value["id"], mode=value.get("mode") if value.get("mode") in {
                "chat", "responses", "incompatible", "other"
            } else None,
            max_input_tokens=positive_integer(value.get("max_input_tokens")),
            max_output_tokens=positive_integer(value.get("max_output_tokens")),
            tools=value.get("tools") if type(value.get("tools")) is bool else None,
            responses=value.get("responses") if type(value.get("responses")) is bool else None,
        )

    @property
    def native_cli(self) -> str:
        return native_cli(self.id)

    @property
    def family(self) -> str:
        if self.native_cli != "claude":
            return ""
        tail = self.id.lower().split("claude-", 1)[1]
        return next((family for family in CLAUDE_FAMILIES if family in tail.split("-")), "")

    @property
    def provider(self) -> str:
        """Prefixo de rota do ID: bedrock.anthropic, vertex_ai.anthropic, openai.us, anthropic/, google/..."""
        match = VENDOR.search(self.id)
        if match:
            return self.id[:match.start()]
        return self.id.split("/", 1)[0] if "/" in self.id else ""

    @property
    def chat(self) -> bool:
        """Modelo de conversa com ferramentas; imagem, áudio, embeddings e afins nunca entram nas listas dos CLIs."""
        if self.mode not in (None, "chat", "responses") or self.tools is False:
            return False
        return not NON_CHAT.search(self.id.lower())

    @property
    def one_million(self) -> bool:
        return self.max_input_tokens is not None and self.max_input_tokens >= 1_000_000

    @property
    def version(self) -> tuple:
        tail = self.id.split("claude-", 1)[-1]
        parts = re.findall(r"\d+", tail)
        version = tuple(int(part) for part in parts if len(part) < 6)
        dates = tuple(int(part) for part in parts if len(part) >= 6)
        stable = not any(marker in tail for marker in ("preview", "beta", "experimental"))
        return version, stable, dates, self.id

    def claude_id(self, verified: bool) -> str:
        # [1m] é sufixo nativo do Claude Code para modelos Claude; não se aplica a modelos de outros fornecedores.
        return self.id + ("[1m]" if verified and self.one_million and self.family else "")

    def to_cache(self) -> dict:
        return asdict(self)


def merge_records(available: list, metadata: list) -> list[ModelInfo]:
    """Cruza /v1/models (o que está disponível) com /v1/model/info do LiteLLM (capacidades), pelo ID completo."""
    identifiers = {base_model(entry["id"]) for entry in available
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                   and MODEL_PATTERN.fullmatch(entry["id"])}
    grouped = {identifier: [] for identifier in identifiers}
    for entry in metadata:
        if not isinstance(entry, dict) or not isinstance(entry.get("model_name"), str):
            continue
        name = entry["model_name"]
        if name in grouped:
            info = entry.get("model_info")
            grouped[name].append(info if isinstance(info, dict) else {})

    def minimum(rows: list[dict], key: str) -> int | None:
        values = [positive_integer(row.get(key)) for row in rows]
        return min(values) if values and all(value is not None for value in values) else None

    def capability(rows: list[dict], key: str) -> bool | None:
        values = [row.get(key) for row in rows]
        if any(value is False for value in values):
            return False
        return True if values and all(value is True for value in values) else None

    records = []
    for identifier in sorted(identifiers, key=str.casefold):
        rows = grouped[identifier]
        modes = {row.get("mode") for row in rows if isinstance(row.get("mode"), str)}
        mode = None
        if modes:
            mode = next(iter(modes)) if len(modes) == 1 else "incompatible"
            if mode not in ("chat", "responses", "incompatible"):
                mode = "other"
        responses = capability(rows, "supports_responses_api")
        if mode == "responses" and responses is None:
            responses = True
        records.append(ModelInfo(
            identifier, mode, minimum(rows, "max_input_tokens"), minimum(rows, "max_output_tokens"),
            capability(rows, "supports_function_calling"), responses,
        ))
    return records


@dataclass
class Catalog:
    """Modelos de UMA conexão. `clis` vem do conector: quais CLIs o host atende e se aceita qualquer modelo neles."""
    records: list[ModelInfo] = field(default_factory=list)
    status: str = "Catálogo indisponível; capacidades não confirmadas."
    verified: bool = False
    clis: frozenset = frozenset(CLIS)
    any_model: bool = False

    def find(self, model: str) -> ModelInfo | None:
        return next((record for record in self.records if record.id == base_model(model)), None)

    def allows(self, record: ModelInfo, cli: str) -> bool:
        if cli not in self.clis or not record.chat:
            return False
        if cli == "codex" and record.responses is False:
            return False
        return self.any_model or record.native_cli == cli

    def for_cli(self, cli: str) -> list[ModelInfo]:
        return [record for record in self.records if self.allows(record, cli)]

    def describe(self, record: ModelInfo) -> str:
        label = f"entrada {token_label(record.max_input_tokens)}"
        if not self.verified:
            label += " • não confirmada"
        if record.family:
            label = f"{record.family.title()} • {label}"
        return f"{record.id} [{label}]"

    def claude_families(self, selected: ModelInfo) -> dict[str, ModelInfo]:
        mapping = {}
        for family in CLAUDE_FAMILIES:
            if selected.family == family:
                mapping[family] = selected
                continue
            candidates = [record for record in self.for_cli("claude") if record.family == family]
            if candidates:
                mapping[family] = max(candidates, key=lambda record: (record.provider == selected.provider, record.version))
        return mapping


def codex_context(record: ModelInfo, verified: bool) -> tuple[int | None, int | None]:
    window = record.max_input_tokens if verified else None
    if window is None:
        return None, None
    threshold = window * 9 // 10
    output = record.max_output_tokens
    if output is not None:
        threshold = min(threshold, window - min(output, window // 4))
    return window, max(1, threshold)
