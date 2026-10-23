"""Uso por perfil: tokens e custo estimado do mês, por destino e por modelo.

Fontes (só números; nada de conteúdo de conversa sai dos arquivos):
- Claude Code: ~/.claude/projects/<pasta codificada>/<session>.jsonl (e <session>/subagents/*.jsonl), linhas
  type=assistant com message.usage {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}.
  O Claude grava uma linha por bloco de conteúdo com o mesmo message.id e o mesmo usage: conta uma vez por id.
- Codex: ~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl, 1ª linha session_meta (cwd, timestamp, id); eventos
  payload.type=token_count com info.total_token_usage (acumulado; input_tokens inclui cached_input_tokens) e o modelo
  no turn_context. Conta a diferença do acumulado entre eventos (o mesmo total repetido não soma de novo).
- Copilot CLI: ~/.copilot/session-state/<id>/workspace.yaml (cwd, created_at) e events.jsonl: session.usage_record
  (uma por chamada, data.usage {model, inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}); sem elas,
  session.shutdown com data.modelMetrics (acumulado por modelo, cresce a cada retomada).
- VS Code e Copilot app: <raiz>/usage.jsonl que o endpoint local grava (endpoint/server.py).

Conversas do perfil: as das pastas dos projetos dele. Pasta em mais de um perfil: só as que o registro (§1) atribui a
este perfil (Claude por session_id; Codex/Copilot pela primeira conversa criada até 10 min depois de cada abertura, ou
pelo session_id de uma retomada). Cache por arquivo (caminho, mtime, tamanho) em <home>/cache/usage.json.
"""
from __future__ import annotations

import calendar
import datetime
import glob
import json
import os
import re
import time
from pathlib import Path

from agent_launcher.domain import profiles
from agent_launcher.domain.catalog import PRICES, ModelInfo, base_model
from agent_launcher.domain.files import atomic_json
from agent_launcher.session import registry

DESTINATIONS = ("claude", "codex", "copilot", "vscode", "copilot_app", "endpoint")
ATTRIBUTION_WINDOW = 10 * 60
CACHE_FILE = "usage.json"
CACHE_SCHEMA = 1
USAGE_FILE = "usage.jsonl"
NOTE = "Estimativa local, a partir das conversas e do endpoint deste perfil; o host não informa consumo."
FIELDS = ("input_tokens", "output_tokens", "cache_read", "cache_write")


# ----------------------------------------------------------------------------------------------------------- tempo
def month_bounds(month: str | None) -> tuple[str, float, float]:
    """'AAAA-MM' (padrão: mês corrente, hora local) -> (mês, início, fim) em epoch."""
    if not month:
        month = time.strftime("%Y-%m")
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(month).strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"Mês inválido: {month!r}. Use AAAA-MM.")
    year, number = int(match.group(1)), int(match.group(2))
    start = time.mktime((year, number, 1, 0, 0, 0, 0, 0, -1))
    days = calendar.monthrange(year, number)[1]
    end = time.mktime((year, number, days, 0, 0, 0, 0, 0, -1)) + 86400
    return f"{year:04d}-{number:02d}", start, end


def parse_time(value) -> float | None:
    """ISO 8601 (com Z) ou epoch em s/ms -> epoch em segundos."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / (1000 if value > 1e11 else 1)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def month_of(epoch: float) -> str:
    return time.strftime("%Y-%m", time.localtime(epoch))


def _number(value) -> int:
    return value if type(value) is int and value > 0 else 0


def _add(months: dict, month: str, model: str, values) -> None:
    row = months.setdefault(month, {}).setdefault(model or "?", [0, 0, 0, 0])
    for index, value in enumerate(values):
        row[index] += value


def _key(folder) -> str:
    return os.path.normcase(os.path.normpath(str(folder)))


# ------------------------------------------------------------------------------------------------------ leitores
def read_claude(path: Path) -> dict:
    """{"months": {mês: {modelo: [entrada, saída, cache_lido, cache_gravado]}}} de um transcript do Claude Code."""
    seen = {}
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if entry.get("type") != "assistant" or not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            model = entry.get("requestedModel") if isinstance(entry.get("requestedModel"), str) else message.get("model")
            if not isinstance(model, str) or model.startswith("<"):
                continue  # <synthetic>: mensagem gerada localmente, sem chamada ao modelo
            at = parse_time(entry.get("timestamp"))
            if at is None:
                continue
            usage = message["usage"]
            values = (_number(usage.get("input_tokens")), _number(usage.get("output_tokens")),
                      _number(usage.get("cache_read_input_tokens")), _number(usage.get("cache_creation_input_tokens")))
            identifier = message.get("id") if isinstance(message.get("id"), str) else f"linha-{len(seen)}"
            seen[identifier] = (month_of(at), base_model(model), values)
    months: dict = {}
    for month, model, values in seen.values():
        _add(months, month, model, values)
    return {"months": months}


def codex_meta(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            first = json.loads(stream.readline() or "{}")
    except (OSError, ValueError):
        return {}
    payload = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return {}
    return {"id": payload.get("id") if isinstance(payload.get("id"), str) else None,
            "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
            "created": parse_time(payload.get("timestamp")) or parse_time(first.get("timestamp"))}


def read_codex(path: Path) -> dict:
    months: dict = {}
    model, previous = "", None
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"turn_context"' not in line and '"token_count"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not isinstance(payload, dict):
                continue
            if entry.get("type") == "turn_context":
                model = payload["model"] if isinstance(payload.get("model"), str) else model
                continue
            if payload.get("type") != "token_count" or not isinstance(payload.get("info"), dict):
                continue
            total = payload["info"].get("total_token_usage")
            at = parse_time(entry.get("timestamp"))
            if not isinstance(total, dict) or at is None:
                continue
            cached = _number(total.get("cached_input_tokens"))
            current = (max(0, _number(total.get("input_tokens")) - cached), _number(total.get("output_tokens")), cached,
                       _number(total.get("cache_write_input_tokens")))
            if previous is not None and all(now >= before for now, before in zip(current, previous)):
                delta = tuple(now - before for now, before in zip(current, previous))
            else:
                delta = current  # primeiro evento (ou contador zerado)
            previous = current
            if any(delta):
                _add(months, month_of(at), model, delta)
    return {"months": months}


def copilot_meta(folder: Path) -> dict:
    meta = {"id": folder.name, "cwd": None, "created": None}
    try:
        with open(folder / "workspace.yaml", encoding="utf-8", errors="replace") as stream:
            for _number_, line in zip(range(40), stream):
                key, _, value = line.partition(":")
                value = value.strip().strip("'\"")
                if key == "cwd":
                    meta["cwd"] = value or None
                elif key == "created_at":
                    meta["created"] = parse_time(value)
                elif key == "id" and value:
                    meta["id"] = value
    except OSError:
        pass
    return meta


def _copilot_values(usage: dict) -> tuple:
    inputs, cached = _number(usage.get("inputTokens")), _number(usage.get("cacheReadTokens"))
    # inputTokens do Copilot CLI inclui o cache lido (contagem estilo OpenAI): separa para não cobrar duas vezes
    return (inputs - cached if cached <= inputs else inputs, _number(usage.get("outputTokens")), cached,
            _number(usage.get("cacheWriteTokens")))


def read_copilot(path: Path) -> dict:
    records, shutdowns = {}, []
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"session.usage_record"' not in line and '"session.shutdown"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            data = entry.get("data") if isinstance(entry, dict) else None
            at = parse_time(entry.get("timestamp")) if isinstance(entry, dict) else None
            if not isinstance(data, dict) or at is None:
                continue
            if entry.get("type") == "session.usage_record" and isinstance(data.get("usage"), dict):
                usage = data["usage"]
                accounting = usage.get("accounting") if isinstance(usage.get("accounting"), dict) else {}
                identifier = accounting.get("usageId") or usage.get("apiCallId") or entry.get("id") or len(records)
                records[str(identifier)] = (month_of(at), str(usage.get("model") or "?"), _copilot_values(usage))
            elif entry.get("type") == "session.shutdown" and isinstance(data.get("modelMetrics"), dict):
                shutdowns.append((at, data["modelMetrics"]))
    months: dict = {}
    if records:
        for month, model, values in records.values():
            _add(months, month, model, values)
        return {"months": months}
    previous: dict = {}
    for at, metrics in shutdowns:  # acumulado por modelo: soma só o que cresceu desde o último encerramento
        for model, metric in metrics.items():
            usage = metric.get("usage") if isinstance(metric, dict) else None
            if not isinstance(usage, dict):
                continue
            current = _copilot_values(usage)
            before = previous.get(model)
            delta = (tuple(now - old for now, old in zip(current, before))
                     if before and all(now >= old for now, old in zip(current, before)) else current)
            previous[model] = current
            if any(delta):
                _add(months, month_of(at), model, delta)
    return {"months": months}


# --------------------------------------------------------------------------------------------------------- cache
class FileCache:
    """Resultado por arquivo, válido enquanto (mtime, tamanho) não mudam."""

    def __init__(self, home: Path):
        self.path = Path(home) / "cache" / CACHE_FILE
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.files = data["files"] if data.get("schema") == CACHE_SCHEMA and isinstance(data.get("files"), dict) else {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.files = {}
        self.used, self.changed = set(), False

    def get(self, path: Path, reader, stat=None) -> dict:
        key = str(path)
        try:
            stat = stat or os.stat(path)
        except OSError:
            return {"months": {}}
        signature = [stat.st_mtime, stat.st_size]
        self.used.add(key)
        cached = self.files.get(key)
        if isinstance(cached, dict) and cached.get("signature") == signature and isinstance(cached.get("result"), dict):
            return cached["result"]
        try:
            result = reader(path)
        except OSError:
            return {"months": {}}
        self.files[key] = {"signature": signature, "result": result}
        self.changed = True
        return result

    def save(self) -> None:
        stale = [key for key in self.files if key not in self.used and not os.path.exists(key)]
        for key in stale:
            del self.files[key]
        if self.changed or stale:
            try:
                atomic_json(self.path, {"schema": CACHE_SCHEMA, "files": self.files})
            except OSError:
                pass


# ------------------------------------------------------------------------------------------------------ atribuição
def claude_projects() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def codex_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def copilot_sessions() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "session-state"


def project_folders(home: Path) -> list[str]:
    try:
        data = json.loads((Path(home) / "config.json").read_text(encoding="utf-8-sig"))
        projects = data.get("projects") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [item["path"] for item in projects if isinstance(item, dict) and isinstance(item.get("path"), str)]


def attributed(candidates: list[dict], openings: list[float], known: set[str]) -> set[str]:
    """Conversas (id, created) que este perfil abriu: a primeira criada até 10 min depois de cada abertura, e as de id
    conhecido (retomadas)."""
    chosen = {item["id"] for item in candidates if item["id"] in known}
    ordered = sorted((item for item in candidates if item.get("created") is not None), key=lambda item: item["created"])
    for at in openings:
        first = next((item for item in ordered if at - 5 <= item["created"] <= at + ATTRIBUTION_WINDOW), None)
        if first:
            chosen.add(first["id"])
    return chosen


def _collect(totals: dict, destination: str, result: dict, month: str, conversation: str) -> None:
    for model, values in (result.get("months") or {}).get(month, {}).items():
        row = totals.setdefault((destination, model), {"values": [0, 0, 0, 0], "conversations": set(), "requests": 0,
                                                       "connections": set()})
        for index, value in enumerate(values):
            row["values"][index] += value
        row["conversations"].add(conversation)


def _claude(home, folders, shared, month, start, cache, totals):
    base = claude_projects()
    for folder in folders:
        directory = base / re.sub(r"[^A-Za-z0-9]", "-", str(folder))
        known = registry.conversation_ids(home, folder, "claude") if shared[folder] else None
        try:
            transcripts = list(directory.glob("*.jsonl"))
        except OSError:
            continue
        for transcript in transcripts:
            session = transcript.stem
            if known is not None and session not in known:
                continue
            files = [transcript, *sorted((directory / session / "subagents").glob("*.jsonl"))]
            for path in files:
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_mtime < start:
                    continue
                _collect(totals, "claude", cache.get(path, read_claude, stat), month, session)


def _codex(home, folders, shared, month, start, cache, totals):
    wanted = {_key(folder): folder for folder in folders}
    by_folder: dict = {}
    for name in glob.glob(str(codex_sessions() / "*" / "*" / "*" / "rollout-*.jsonl")):
        path = Path(name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime < start:
            continue
        meta = _cached_meta(cache, path, stat, codex_meta)
        if meta.get("cwd") and _key(meta["cwd"]) in wanted:
            by_folder.setdefault(wanted[_key(meta["cwd"])], []).append((path, stat, meta))
    _attribute(home, "codex", by_folder, shared, month, cache, totals, read_codex)


def _copilot(home, folders, shared, month, start, cache, totals):
    wanted = {_key(folder): folder for folder in folders}
    by_folder: dict = {}
    try:
        sessions = list(copilot_sessions().iterdir())
    except OSError:
        sessions = []
    for folder in sessions:
        path = folder / "events.jsonl"
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime < start:
            continue
        meta = copilot_meta(folder)
        if meta.get("cwd") and _key(meta["cwd"]) in wanted:
            by_folder.setdefault(wanted[_key(meta["cwd"])], []).append((path, stat, meta))
    _attribute(home, "copilot", by_folder, shared, month, cache, totals, read_copilot)


def _cached_meta(cache: FileCache, path: Path, stat, reader) -> dict:
    """Metadados (1ª linha) do rollout, guardados junto do resultado do arquivo."""
    key = str(path) + "#meta"
    signature = [stat.st_mtime, stat.st_size]
    cache.used.add(key)
    cached = cache.files.get(key)
    if isinstance(cached, dict) and cached.get("signature") == signature and isinstance(cached.get("result"), dict):
        return cached["result"]
    meta = reader(path)
    cache.files[key] = {"signature": signature, "result": meta}
    cache.changed = True
    return meta


def _attribute(home, cli, by_folder, shared, month, cache, totals, reader):
    for folder, items in by_folder.items():
        if shared[folder]:
            candidates = [{"id": meta.get("id") or str(path), "created": meta.get("created")} for path, _s, meta in items]
            chosen = attributed(candidates, registry.opened_since(home, folder, cli),
                                registry.conversation_ids(home, folder, cli))
            items = [item for item in items if (item[2].get("id") or str(item[0])) in chosen]
        for path, stat, meta in items:
            _collect(totals, cli, cache.get(path, reader, stat), month, meta.get("id") or str(path))


def endpoint_destination(entry: dict) -> str:
    destination = entry.get("destination")
    return destination if destination in DESTINATIONS else "endpoint"


def _endpoint(base: Path, profile_id: str, start: float, end: float, totals: dict) -> None:
    try:
        stream = open(Path(base) / USAGE_FILE, encoding="utf-8", errors="replace")
    except OSError:
        return
    with stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or (entry.get("profile") or profiles.DEFAULT) != profile_id:
                continue
            at = entry.get("at")
            if not isinstance(at, (int, float)) or not start <= at < end:
                continue
            model = str(entry.get("model") or "?")
            row = totals.setdefault((endpoint_destination(entry), model), {
                "values": [0, 0, 0, 0], "conversations": set(), "requests": 0, "connections": set()})
            for index, field in enumerate(FIELDS):
                row["values"][index] += _number(entry.get(field))
            row["requests"] += 1
            if isinstance(entry.get("connection"), str):
                row["connections"].add(entry["connection"])


# ----------------------------------------------------------------------------------------------------------- preço
def price_table(home: Path) -> dict:
    """{id da conexão: {modelo: ModelInfo}} dos catálogos em cache das conexões deste perfil (só os com preço)."""
    tables = {}
    for path in (Path(home) / "cache").glob("models-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records = [ModelInfo.from_cache(item) for item in data.get("records") or []]
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        priced = {record.id: record for record in records
                  if record.input_cost_per_token is not None or record.output_cost_per_token is not None}
        if priced:
            tables[path.stem.removeprefix("models-")] = priced
    return tables


def find_price(tables: dict, model: str, connections=()) -> ModelInfo | None:
    """Preço do modelo: primeiro nas conexões que o serviram, depois em qualquer conexão do perfil; ID exato, depois
    pelo sufixo (claude-sonnet-5-5 do transcript = vertex_ai.anthropic.claude-sonnet-5-5 do catálogo)."""
    model = base_model(model)
    ordered = [tables[item] for item in connections if item in tables] + list(tables.values())
    for table in ordered:
        if model in table:
            return table[model]
    for table in ordered:
        matches = [record for identifier, record in table.items()
                   if identifier.endswith(("." + model, "/" + model)) or model.endswith(("." + identifier, "/" + identifier))]
        if len(matches) == 1:
            return matches[0]
    return None


def cost(record: ModelInfo | None, values) -> float | None:
    if record is None or record.input_cost_per_token is None or record.output_cost_per_token is None:
        return None
    inputs, outputs, read, write = values
    read_price = record.cache_read_input_token_cost
    write_price = record.cache_creation_input_token_cost
    return (inputs * record.input_cost_per_token + outputs * record.output_cost_per_token
            + read * (read_price if read_price is not None else record.input_cost_per_token)
            + write * (write_price if write_price is not None else record.input_cost_per_token))


# ---------------------------------------------------------------------------------------------------------- relatório
def _empty() -> dict:
    return {**{field: 0 for field in FIELDS}, "conversations": 0, "requests": 0, "cost": None}


def _accumulate(target: dict, row: dict) -> None:
    for field in (*FIELDS, "conversations", "requests"):
        target[field] += row[field]
    if row["cost"] is not None:
        target["cost"] = (target["cost"] or 0.0) + row["cost"]
    target["cost_complete"] = target.get("cost_complete", True) and row["cost"] is not None


def usage_report(home: Path, month: str | None = None) -> dict:
    """Uso do perfil dono de `home` no mês (AAAA-MM, hora local; padrão o mês corrente)."""
    home = Path(home)
    month, start, end = month_bounds(month)
    base = profiles.base_of(home)
    profile_id = profiles.profile_of_home(home, base)["id"]
    folders = project_folders(home)
    shared = {}
    for folder in folders:
        try:
            shared[folder] = len(profiles.profiles_with_path(folder, base)) > 1
        except OSError:
            shared[folder] = False
    cache = FileCache(home)
    totals: dict = {}
    _claude(home, folders, shared, month, start, cache, totals)
    _codex(home, folders, shared, month, start, cache, totals)
    _copilot(home, folders, shared, month, start, cache, totals)
    cache.save()
    _endpoint(base, profile_id, start, end, totals)
    tables = price_table(home)
    models = []
    for (destination, model), row in sorted(totals.items(), key=lambda item: (DESTINATIONS.index(item[0][0])
                                                                              if item[0][0] in DESTINATIONS else 99,
                                                                              item[0][1].casefold())):
        values = row["values"]
        price = cost(find_price(tables, model, sorted(row["connections"])), values)
        # priced=False: o catálogo das conexões não tem preço (entrada e saída) deste modelo; cost fica null
        models.append({"destination": destination, "model": model, **dict(zip(FIELDS, values)),
                       "conversations": len(row["conversations"]), "requests": row["requests"],
                       "cost": price, "priced": price is not None})
    destinations: dict = {}
    total = _empty()
    for row in models:
        _accumulate(destinations.setdefault(row["destination"], _empty()), row)
        _accumulate(total, row)
    return {"profile": profile_id, "month": month, "note": NOTE, "currency": "USD", "destinations": destinations,
            "models": models, "total": total}
