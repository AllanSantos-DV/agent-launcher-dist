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
- VS Code e Copilot app: <raiz>/usage.jsonl que o endpoint local grava (endpoint/server.py). As linhas dele com destino
  Claude Code ou Codex (sessão aberta pelo endpoint local) não entram: as conversas já contam esse uso.

Conversas do perfil: as das pastas dos projetos dele. Pasta em mais de um perfil: só as que o registro (§1) atribui a
este perfil (Claude por session_id; Codex/Copilot pela primeira conversa criada até 10 min depois de cada abertura, ou
pelo session_id de uma retomada). Cache por arquivo (caminho, mtime, tamanho) em <home>/cache/usage.json; o uso por dia
(`usage --days`) tem o seu, em <home>/cache/usage-days.json, e o por bloco (`usage --blocks`), o seu, em
<home>/cache/usage-blocks.json, para não invalidar o do mês.

Blocos de 5 horas (`usage --blocks`, como `ccusage blocks`): o bloco começa na hora cheia, no fuso local da máquina (o
ccusage usa UTC), da primeira requisição e dura 5 h; a requisição que cai no fim do bloco ou depois dele abre o seguinte.
Os carimbos vêm de cada CLI (Claude: timestamp de cada linha assistant; Codex: de cada evento token_count; Copilot CLI: de
cada session.usage_record), guardados por hora local, com o menor e o maior minuto de cada hora. O `rate_limits` dos eventos do Codex não serve para mostrar o reset real: no
dado real a janela é a semanal (`window_minutes` 10080) ou vem nula, nunca a de 5 h. O custo é o equivalente de API, não o
da assinatura.
"""
from __future__ import annotations

import calendar
import datetime
import functools
import glob
import json
import math
import os
import re
import time
from pathlib import Path

from agent_launcher.domain import profiles
from agent_launcher.domain.catalog import PRICES, ModelInfo, base_model
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json, note_state_recovery
from agent_launcher.session import registry

DESTINATIONS = ("claude", "codex", "copilot", "vscode", "copilot_app", "endpoint")
CONVERSATION_DESTINATIONS = ("claude", "codex")  # o uso deles vem das conversas, mesmo quando a sessão passa pelo endpoint
ATTRIBUTION_WINDOW = 10 * 60
CACHE_FILE = "usage.json"
CACHE_SCHEMA = 2  # 2: resultado com "context" e o ponto de continuação ("resume")
DAYS_CACHE_FILE = "usage-days.json"
DAYS_CACHE_SCHEMA = 1
BLOCKS_CACHE_FILE = "usage-blocks.json"
BLOCKS_CACHE_SCHEMA = 2  # 2: baldes por hora local com o menor e o maior minuto (o 1 guardava um balde por minuto)
BLOCK_SECONDS = 5 * 3600
BLOCKS_HISTORY_DAYS = 8
MAX_DAYS = 366
DIALOG_DAYS = 14
USAGE_FILE = "usage.jsonl"
RECENT_IDS = 64  # ids do Claude lembrados entre leituras: os blocos de uma mensagem vêm em linhas próximas
SAVE_EVERY = 30  # s entre gravações do cache durante uma leitura longa (a primeira leitura de um mês pode levar minutos)
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


def recent_months(count: int = 12, today: tuple[int, int] | None = None) -> list[str]:
    """Os `count` últimos meses (AAAA-MM), do corrente para trás; `today` é (ano, mês) e só existe para os testes."""
    year, number = today or (int(time.strftime("%Y")), int(time.strftime("%m")))
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{number:02d}")
        year, number = (year - 1, 12) if number == 1 else (year, number - 1)
    return months


def local_today() -> datetime.date:
    return datetime.date.today()


def current_time() -> float:
    return time.time()


def recent_days(count: int, today: datetime.date | None = None) -> list[str]:
    """Os `count` últimos dias (AAAA-MM-DD, hora local), do de hoje para trás; `today` só existe para os testes."""
    first = today or local_today()
    return [(first - datetime.timedelta(days=offset)).isoformat() for offset in range(count)]


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


def day_of(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def hour_of(epoch: float) -> str:
    """A hora cheia local (epoch em segundos) como chave: o bloco do `usage --blocks` sempre começa numa hora cheia."""
    return str(int(block_start(epoch)))


def _note_span(spans: dict, key: str, at: float) -> None:
    """Alarga o [menor, maior] minuto (epoch em segundos) em que houve requisição no balde `key`."""
    minute = int(at // 60 * 60)
    span = spans.get(key)
    if span is None:
        spans[key] = [minute, minute]
    else:
        span[0], span[1] = min(span[0], minute), max(span[1], minute)


def _number(value) -> int:
    return value if type(value) is int and value > 0 else 0


def _add(months: dict, month: str, model: str, values) -> None:
    row = months.setdefault(month, {}).setdefault(model or "?", [0, 0, 0, 0])
    for index, value in enumerate(values):
        row[index] += value


def _key(folder) -> str:
    return os.path.normcase(os.path.normpath(str(folder)))


# ------------------------------------------------------------------------------------------------------ leitores
def _resume_point(stream, resume: dict | None) -> int:
    """Onde continuar a leitura: o offset salvo, se os bytes antes dele ainda são os mesmos (arquivo só cresceu); senão 0."""
    if not resume or not isinstance(resume.get("offset"), int):
        return 0
    offset, tail = resume["offset"], bytes.fromhex(str(resume.get("tail", "")))
    if offset < len(tail):
        return 0
    stream.seek(offset - len(tail))
    if stream.read(len(tail)) != tail:
        stream.seek(0)
        return 0
    return offset


def _lines(stream, offset: int, markers: tuple[bytes, ...], position: list):
    """Linhas completas a partir de offset que têm todos os marcadores; position[0] acompanha o fim da última linha
    completa (a linha que o CLI ainda está gravando fica para a próxima leitura)."""
    position[0] = offset
    for raw in stream:
        if not raw.endswith(b"\n"):
            break
        position[0] += len(raw)
        if all(marker in raw for marker in markers):
            yield raw.decode("utf-8", "replace")


def _resume_state(stream, offset: int, **state) -> dict:
    stream.seek(max(0, offset - 64))
    return {"offset": offset, "tail": stream.read(offset - max(0, offset - 64)).hex(), **state}


def _months_copy(result: dict) -> dict:
    return json.loads(json.dumps(result.get("months") or {}))


def _spans_start(resume: dict | None, offset: int, enabled: bool) -> dict | None:
    """Os minutos extremos por balde: `None` quando o leitor não os guarda; com `resume`, os do resultado anterior."""
    if not enabled:
        return None
    return json.loads(json.dumps(resume.get("spans") or {})) if offset else {}


def _with_spans(result: dict, spans: dict | None) -> dict:
    if spans is not None:
        result["spans"] = spans
    return result


def read_claude(path: Path, resume: dict | None = None, bucket=month_of, with_spans: bool = False) -> dict:
    """{"months": {mês: {modelo: [entrada, saída, cache_lido, cache_gravado]}}, "resume": ...} de um transcript do Claude
    Code. Com `resume` (o resultado anterior do mesmo arquivo) lê só o que foi acrescentado depois dele. `bucket` (epoch ->
    chave) escolhe o período: com `day_of`, as chaves de "months" são dias. Com `with_spans`, o resultado traz também
    "spans": {chave: [menor, maior minuto]} das requisições de cada período."""
    with open(path, "rb") as stream:
        offset = _resume_point(stream, (resume or {}).get("resume"))
        state = resume["resume"] if offset else {}
        months = _months_copy(resume) if offset else {}
        spans = _spans_start(resume, offset, with_spans)
        recent = {item[0]: item[1:] for item in state.get("recent", [])}
        count, context = state.get("count", 0), (resume or {}).get("context") if offset else None
        position = [offset]
        for line in _lines(stream, offset, (b'"usage"', b'"assistant"'), position):
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
            usage = message["usage"]
            values = [_number(usage.get("input_tokens")), _number(usage.get("output_tokens")),
                      _number(usage.get("cache_read_input_tokens")), _number(usage.get("cache_creation_input_tokens"))]
            # resposta interrompida grava uso zerado: o contexto que vale é o do último pedido que chegou ao host
            context = values[0] + values[2] + values[3] or context
            at = parse_time(entry.get("timestamp"))
            if at is None:
                continue
            identifier = message.get("id") if isinstance(message.get("id"), str) else f"linha-{count}"
            count += 1
            # o Claude grava uma linha por bloco com o mesmo id e o mesmo usage: vale a última, contada uma vez
            if identifier in recent:
                old_month, old_model, old_values = recent.pop(identifier)
                _add(months, old_month, old_model, [-value for value in old_values])
            recent[identifier] = (bucket(at), base_model(model), values)
            _add(months, *recent[identifier])
            if spans is not None:
                _note_span(spans, recent[identifier][0], at)
            while len(recent) > RECENT_IDS:
                recent.pop(next(iter(recent)))
        state = _resume_state(stream, position[0], count=count,
                              recent=[[identifier, *item] for identifier, item in recent.items()])
    return _with_spans({"months": months, "context": context, "resume": state}, spans)


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


def read_codex(path: Path, resume: dict | None = None, bucket=month_of, with_spans: bool = False) -> dict:
    """Como read_claude, para um rollout do Codex: soma o crescimento do acumulado entre eventos token_count."""
    with open(path, "rb") as stream:
        offset = _resume_point(stream, (resume or {}).get("resume"))
        state = resume["resume"] if offset else {}
        months = _months_copy(resume) if offset else {}
        spans = _spans_start(resume, offset, with_spans)
        model, previous = state.get("model", ""), state.get("previous")
        context = (resume or {}).get("context") if offset else None
        position = [offset]
        for line in _lines(stream, offset, (), position):
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
            latest = payload["info"].get("last_token_usage")
            if isinstance(latest, dict) and _number(latest.get("input_tokens")):
                context = _number(latest.get("input_tokens"))
            total = payload["info"].get("total_token_usage")
            at = parse_time(entry.get("timestamp"))
            if not isinstance(total, dict) or at is None:
                continue
            cached = _number(total.get("cached_input_tokens"))
            current = [max(0, _number(total.get("input_tokens")) - cached), _number(total.get("output_tokens")), cached,
                       _number(total.get("cache_write_input_tokens"))]
            if previous is not None and all(now >= before for now, before in zip(current, previous)):
                delta = [now - before for now, before in zip(current, previous)]
            else:
                delta = current  # primeiro evento (ou contador zerado)
            previous = current
            if any(delta):
                _add(months, bucket(at), model, delta)
                if spans is not None:
                    _note_span(spans, bucket(at), at)
        state = _resume_state(stream, position[0], model=model, previous=previous)
    return _with_spans({"months": months, "context": context, "resume": state}, spans)


def copilot_meta(folder: Path, home: Path | None = None) -> dict:
    """Id, pasta e criação da sessão do Copilot (workspace.yaml); arquivo ilegível vai para o state-recovery.log de `home`."""
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
    except FileNotFoundError:
        pass
    except OSError as error:
        if home is not None:
            note_state_recovery(home, f"{folder.name}/workspace.yaml ilegível ({error}); a sessão do Copilot fica fora do Uso do mês")
    return meta


def _copilot_values(usage: dict) -> tuple:
    inputs, cached = _number(usage.get("inputTokens")), _number(usage.get("cacheReadTokens"))
    # inputTokens do Copilot CLI inclui o cache lido (contagem estilo OpenAI): separa para não cobrar duas vezes
    return (inputs - cached if cached <= inputs else inputs, _number(usage.get("outputTokens")), cached,
            _number(usage.get("cacheWriteTokens")))


def read_copilot(path: Path, bucket=month_of, with_spans: bool = False) -> dict:
    """Como read_claude, para o events.jsonl de uma sessão do Copilot CLI (relido inteiro: não tem `resume`)."""
    records, shutdowns, context, spans = {}, [], None, {} if with_spans else None
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
                records[str(identifier)] = (bucket(at), str(usage.get("model") or "?"), _copilot_values(usage), at)
                # inputTokens já inclui o cache lido: é o contexto inteiro do pedido
                context = _number(usage.get("inputTokens")) or context
            elif entry.get("type") == "session.shutdown" and isinstance(data.get("modelMetrics"), dict):
                shutdowns.append((at, data["modelMetrics"]))
    months: dict = {}
    if records:
        for month, model, values, at in records.values():
            _add(months, month, model, values)
            if spans is not None:
                _note_span(spans, month, at)
        return _with_spans({"months": months, "context": context}, spans)
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
                _add(months, bucket(at), model, delta)
                if spans is not None:
                    _note_span(spans, bucket(at), at)
    return _with_spans({"months": months}, spans)


read_claude_days = functools.partial(read_claude, bucket=day_of)
read_codex_days = functools.partial(read_codex, bucket=day_of)
read_copilot_days = functools.partial(read_copilot, bucket=day_of)
read_claude_hours = functools.partial(read_claude, bucket=hour_of, with_spans=True)
read_codex_hours = functools.partial(read_codex, bucket=hour_of, with_spans=True)
read_copilot_hours = functools.partial(read_copilot, bucket=hour_of, with_spans=True)


# --------------------------------------------------------------------------------------------------------- cache
class FileCache:
    """Resultado por arquivo, válido enquanto (mtime, tamanho) não mudam. Cada par (arquivo, schema) é um cache à parte."""

    def __init__(self, home: Path, name: str = CACHE_FILE, schema: int = CACHE_SCHEMA):
        self.path = Path(home) / "cache" / name
        self.schema = schema
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.files = data["files"] if data.get("schema") == schema and isinstance(data.get("files"), dict) else {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.files = {}
        self.used, self.changed = set(), False
        self.saved_at = time.monotonic()

    def get(self, path: Path, reader, stat=None) -> dict:
        """Resultado do arquivo; o que mudou é relido, e o reader que sabe continuar (resume) lê só o que cresceu."""
        key = str(path)
        try:
            stat = stat or os.stat(path)
        except OSError:
            return {"months": {}}
        signature = [stat.st_mtime, stat.st_size]
        self.used.add(key)
        cached = self.files.get(key)
        previous = cached.get("result") if isinstance(cached, dict) and isinstance(cached.get("result"), dict) else None
        if previous is not None and cached.get("signature") == signature:
            return previous
        try:
            result = reader(path, previous) if previous and isinstance(previous.get("resume"), dict) else reader(path)
        except OSError:
            return {"months": {}}
        self.files[key] = {"signature": signature, "result": result}
        self.changed = True
        if time.monotonic() - self.saved_at >= SAVE_EVERY:
            self.save()
        return result

    def save(self) -> None:
        stale = [key for key in self.files if key not in self.used and not os.path.exists(key)]
        for key in stale:
            del self.files[key]
        self.saved_at = time.monotonic()
        if self.changed or stale:
            try:
                atomic_json(self.path, {"schema": self.schema, "files": self.files})
                self.changed = False
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
    """Soma ao `totals` o período `month` (um mês, ou um dia no uso por dia) do resultado de um transcript."""
    for model, values in (result.get("months") or {}).get(month, {}).items():
        row = totals.setdefault((destination, model), {"values": [0, 0, 0, 0], "conversations": set(), "requests": 0,
                                                       "connections": set()})
        for index, value in enumerate(values):
            row["values"][index] += value
        row["conversations"].add(conversation)


def _claude(home, folders, shared, start, cache, collect, reader=read_claude):
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
                collect("claude", cache.get(path, reader, stat), session)


def _codex(home, folders, shared, start, cache, collect, reader=read_codex):
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
    _attribute(home, "codex", by_folder, shared, cache, collect, reader)


def _copilot(home, folders, shared, start, cache, collect, reader=read_copilot):
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
        meta = copilot_meta(folder, home)
        if meta.get("cwd") and _key(meta["cwd"]) in wanted:
            by_folder.setdefault(wanted[_key(meta["cwd"])], []).append((path, stat, meta))
    _attribute(home, "copilot", by_folder, shared, cache, collect, reader)


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


def _attribute(home, cli, by_folder, shared, cache, collect, reader):
    for folder, items in by_folder.items():
        if shared[folder]:
            candidates = [{"id": meta.get("id") or str(path), "created": meta.get("created")} for path, _s, meta in items]
            chosen = attributed(candidates, registry.opened_since(home, folder, cli),
                                registry.conversation_ids(home, folder, cli))
            items = [item for item in items if (item[2].get("id") or str(item[0])) in chosen]
        for path, stat, meta in items:
            collect(cli, cache.get(path, reader, stat), meta.get("id") or str(path))


def endpoint_destination(entry: dict) -> str:
    destination = entry.get("destination")
    return destination if destination in DESTINATIONS else "endpoint"


def _endpoint(base: Path, profile_id: str, start: float, end: float, totals: dict, bucket=None, spans=None) -> None:
    """Soma o usage.jsonl do endpoint no intervalo. Com `bucket` (epoch -> dia), `totals` é {dia: totais}; com `spans`,
    guarda também o menor e o maior minuto de cada balde."""
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
            destination = endpoint_destination(entry)
            if destination in CONVERSATION_DESTINATIONS:
                continue
            model = str(entry.get("model") or "?")
            target = totals if bucket is None else totals.setdefault(bucket(at), {})
            if spans is not None:
                _note_span(spans, bucket(at), at)
            row = target.setdefault((destination, model), {
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


def shared_folders(folders, base: Path) -> dict:
    """Para cada pasta de projeto, se ela está em mais de um perfil (a conversa dela só conta para o dono que a abriu);
    pasta que o cadastro de perfis não consegue ler conta como não compartilhada."""
    shared = {}
    for folder in folders:
        try:
            shared[folder] = len(profiles.profiles_with_path(folder, base)) > 1
        except OSError:
            shared[folder] = False
    return shared


def usage_report(home: Path, month: str | None = None) -> dict:
    """Uso do perfil dono de `home` no mês (AAAA-MM, hora local; padrão o mês corrente)."""
    home = Path(home)
    month, start, end = month_bounds(month)
    base = profiles.base_of(home)
    profile_id = profiles.profile_of_home(home, base)["id"]
    folders = project_folders(home)
    shared = shared_folders(folders, base)
    cache = FileCache(home)
    totals: dict = {}

    def collect(destination, result, conversation):
        _collect(totals, destination, result, month, conversation)
    _claude(home, folders, shared, start, cache, collect)
    _codex(home, folders, shared, start, cache, collect)
    _copilot(home, folders, shared, start, cache, collect)
    cache.save()
    _endpoint(base, profile_id, start, end, totals)
    return _summary(profile_id, month, _model_rows(price_table(home), totals))


def _model_rows(tables: dict, totals: dict) -> list[dict]:
    """Linhas por destino e modelo, em ordem de destino e nome, com o custo do catálogo (null sem preço)."""
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
    return models


def usage_days_report(home: Path, days: int, today: datetime.date | None = None) -> list[dict]:
    """Uso do perfil dono de `home` em cada um dos `days` últimos dias (hora local, do de hoje para trás), como
    {"day", "currency", "total"} com o mesmo `total` do relatório do mês. Dia sem uso fica de fora. Cache próprio."""
    home = Path(home)
    wanted = recent_days(days, today)
    first = datetime.date.fromisoformat(wanted[-1])
    start = datetime.datetime.combine(first, datetime.time.min).timestamp()
    end = datetime.datetime.combine(datetime.date.fromisoformat(wanted[0]) + datetime.timedelta(days=1),
                                    datetime.time.min).timestamp()
    base = profiles.base_of(home)
    profile_id = profiles.profile_of_home(home, base)["id"]
    folders = project_folders(home)
    shared = shared_folders(folders, base)
    cache = FileCache(home, DAYS_CACHE_FILE, DAYS_CACHE_SCHEMA)
    by_day: dict = {}

    def collect(destination, result, conversation):
        for day in wanted:
            if day in (result.get("months") or {}):
                _collect(by_day.setdefault(day, {}), destination, result, day, conversation)
    _claude(home, folders, shared, start, cache, collect, read_claude_days)
    _codex(home, folders, shared, start, cache, collect, read_codex_days)
    _copilot(home, folders, shared, start, cache, collect, read_copilot_days)
    cache.save()
    _endpoint(base, profile_id, start, end, by_day, day_of)
    tables = price_table(home)
    rows = []
    for day in wanted:
        total = _summary(profile_id, day, _model_rows(tables, by_day.get(day, {})))["total"]
        if any(total[field] for field in FIELDS):
            rows.append({"day": day, "currency": "USD", "total": total})
    return rows


def block_start(epoch: float) -> float:
    """A hora cheia, no fuso local, em que cai o instante."""
    return datetime.datetime.fromtimestamp(epoch).replace(minute=0, second=0, microsecond=0).timestamp()


def group_blocks(items: list[tuple[float, dict]]) -> list[tuple[float, list[tuple[float, dict]]]]:
    """Agrupa (instante, linhas) em blocos de 5 h, do mais antigo ao mais novo, como (início, itens). Uma requisição a mais
    de 5 h da anterior cai sempre depois do fim do bloco, então o fim do bloco é a única regra de corte."""
    blocks: list = []
    for item in sorted(items, key=lambda item: item[0]):
        if not blocks or item[0] >= blocks[-1][0] + BLOCK_SECONDS:
            blocks.append((block_start(item[0]), []))
        blocks[-1][1].append(item)
    return blocks


def _local_iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _merge_rows(target: dict, rows: dict) -> None:
    for key, row in rows.items():
        merged = target.setdefault(key, {"values": [0, 0, 0, 0], "conversations": set(), "requests": 0, "connections": set()})
        for index, value in enumerate(row["values"]):
            merged["values"][index] += value
        merged["conversations"] |= row["conversations"]
        merged["requests"] += row["requests"]
        merged["connections"] |= row["connections"]


def usage_blocks_report(home: Path, now: float | None = None) -> list[dict]:
    """Blocos de 5 h do perfil dono de `home` nos últimos BLOCKS_HISTORY_DAYS dias, do mais recente para trás, como
    {"start", "end", "active", "remaining_seconds", "first_request", "last_request", "currency", "total"} (horas locais
    com deslocamento; `total` como o do `usage_days_report`, com `cost_complete`). `active`: `now` está dentro do bloco, e
    então `remaining_seconds` conta até o fim dele. O primeiro bloco do histórico começa na primeira requisição dentro dele.
    Os leitores são os do `usage --days`, por hora local com o menor e o maior minuto de cada hora; cache próprio. A
    hora em que cai o começo do histórico só entra se começa depois dele (o balde é a hora inteira)."""
    home = Path(home)
    now = current_time() if now is None else now
    since = now - BLOCKS_HISTORY_DAYS * 86400
    first_hour = since if block_start(since) == since else block_start(since + 3600)
    base = profiles.base_of(home)
    profile_id = profiles.profile_of_home(home, base)["id"]
    folders = project_folders(home)
    shared = shared_folders(folders, base)
    cache = FileCache(home, BLOCKS_CACHE_FILE, BLOCKS_CACHE_SCHEMA)
    by_hour: dict = {}
    spans: dict = {}

    def collect(destination, result, conversation):
        for hour in result.get("months") or {}:
            if int(hour) >= first_hour:
                _collect(by_hour.setdefault(hour, {}), destination, result, hour, conversation)
                low, high = (result.get("spans") or {}).get(hour, (None, None))
                if low is not None:
                    _note_span(spans, hour, low)
                    _note_span(spans, hour, high)
    _claude(home, folders, shared, since, cache, collect, read_claude_hours)
    _codex(home, folders, shared, since, cache, collect, read_codex_hours)
    _copilot(home, folders, shared, since, cache, collect, read_copilot_hours)
    cache.save()
    _endpoint(base, profile_id, first_hour, math.inf, by_hour, hour_of, spans)
    items = [(float(hour), rows) for hour, rows in by_hour.items() if any(any(row["values"]) for row in rows.values())]
    tables = price_table(home)
    report = []
    for start, group in reversed(group_blocks(items)):
        rows: dict = {}
        for _moment, hour_rows in group:
            _merge_rows(rows, hour_rows)
        end = start + BLOCK_SECONDS
        active = start <= now < end
        report.append({"start": _local_iso(start), "end": _local_iso(end), "active": active,
                       "remaining_seconds": int(end - now) if active else None,
                       "first_request": _local_iso(spans[str(int(group[0][0]))][0]),
                       "last_request": _local_iso(spans[str(int(group[-1][0]))][1]),
                       "currency": "USD", "total": _summary(profile_id, day_of(start), _model_rows(tables, rows))["total"]})
    return report


def _summary(profile: str, month: str, models: list[dict]) -> dict:
    """Totais por destino e geral a partir das linhas por destino e modelo."""
    destinations: dict = {}
    total = _empty()
    for row in models:
        _accumulate(destinations.setdefault(row["destination"], _empty()), row)
        _accumulate(total, row)
    return {"profile": profile, "month": month, "note": NOTE, "currency": "USD", "destinations": destinations,
            "models": models, "total": total}


def usage_report_all(home: Path, month: str | None = None) -> dict:
    """Uso do mês de todos os perfis numa tabela só: a mesma linha de destino e modelo soma entre os perfis, e a que
    algum perfil não consegue precificar fica sem custo."""
    base = profiles.base_of(Path(home))
    merged: dict[tuple, dict] = {}
    month_label = month
    for profile in profiles.list_profiles(base):
        report = usage_report(profiles.home_of(profile["id"], base), month)
        month_label = report["month"]
        for row in report["models"]:
            target = merged.setdefault((row["destination"], row["model"]), {
                "destination": row["destination"], "model": row["model"], **{field: 0 for field in FIELDS},
                "conversations": 0, "requests": 0, "cost": 0.0, "priced": True})
            for field in (*FIELDS, "conversations", "requests"):
                target[field] += row[field]
            target["priced"] = target["priced"] and row["priced"]
            target["cost"] = target["cost"] + row["cost"] if target["priced"] else None
    models = sorted(merged.values(), key=lambda row: (DESTINATIONS.index(row["destination"])
                                                      if row["destination"] in DESTINATIONS else 99,
                                                      row["model"].casefold()))
    return _summary("todos", month_label or month_bounds(month)[0], models)


def money(value: float) -> str:
    """Valor na moeda do relatório, no formato brasileiro (US$ 1.234,50)."""
    return f"US$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


USAGE_HEADER = ("Destino", "Modelo", "Entrada", "Saída", "Custo")
DESTINATION_NAMES = {"claude": "Claude Code", "codex": "Codex", "copilot": "Copilot CLI", "vscode": "VS Code",
                     "copilot_app": "Copilot app", "endpoint": "Endpoint local"}


def tokens(value: int) -> str:
    """Quantidade de tokens com ponto de milhar (1.234)."""
    return f"{value:,}".replace(",", ".")


def usage_rows(report: dict) -> list[tuple[str, str, str, str, str]]:
    """Linhas da tabela de uso (diálogo e relatório de texto): destino, modelo, entrada (com cache), saída, custo."""

    rows = []
    unpriced = False
    for row in report.get("models", []):
        entrada = row["input_tokens"] + row["cache_read"] + row["cache_write"]
        priced = row.get("priced", row["cost"] is not None) and row["cost"] is not None
        unpriced = unpriced or not priced
        rows.append((DESTINATION_NAMES.get(row["destination"], row["destination"]), row["model"], tokens(entrada),
                     tokens(row["output_tokens"]), money(row["cost"]) if priced else "sem preço"))
    total = report.get("total") or {}
    if rows:
        if total.get("cost") is None:
            total_cost = "sem preço"
        else:
            total_cost = money(total["cost"]) + (" (parcial)" if unpriced else "")
        rows.append(("Total", "", tokens(total.get("input_tokens", 0) + total.get("cache_read", 0)
                                         + total.get("cache_write", 0)),
                     tokens(total.get("output_tokens", 0)), total_cost))
    return rows


def usage_text(report: dict) -> str:
    """O relatório de uso separado por tabulação: colado numa planilha, cada valor cai na sua coluna."""
    scope = " (todos os perfis)" if report.get("profile") == "todos" else ""
    lines = [f"Mês {report['month']}{scope}", "\t".join(USAGE_HEADER)]
    lines += ["\t".join(row) for row in usage_rows(report)]
    return "\n".join(lines)


USAGE_DAYS_HEADER = ("Dia", "Entrada", "Saída", "Custo")
USAGE_BLOCKS_HEADER = ("Início", "Fim", "Ativo", "Restante", "Entrada", "Saída", "Custo")


def _spent(total: dict) -> str:
    """Custo de um total do relatório: "sem preço" onde o catálogo não cobre, "(parcial)" na soma que deixa um modelo de fora."""
    if total["cost"] is None:
        return "sem preço"
    return money(total["cost"]) + ("" if total.get("cost_complete", True) else " (parcial)")


def usage_days_rows(rows: list[dict]) -> list[tuple[str, str, str, str]]:
    """Linhas da tabela de uso por dia (diálogo e `usage_days_text`): dia, entrada com o cache, saída e custo."""
    return [(row["day"], tokens(row["total"]["input_tokens"] + row["total"]["cache_read"] + row["total"]["cache_write"]),
             tokens(row["total"]["output_tokens"]), _spent(row["total"])) for row in rows]


def usage_days_text(rows: list[dict]) -> str:
    """O uso por dia separado por tabulação, no formato de `usage_text`: entrada com o cache, saída e custo."""
    return "\n".join(["\t".join(USAGE_DAYS_HEADER)] + ["\t".join(row) for row in usage_days_rows(rows)])


def remaining_text(seconds: int) -> str:
    """Tempo restante de um bloco ativo: 2h 40min, ou 45min abaixo de uma hora."""
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes:02d}min" if hours else f"{minutes}min"


def usage_blocks_rows(rows: list[dict]) -> list[tuple[str, ...]]:
    """Linhas da tabela de blocos de 5 h (diálogo e `usage_blocks_text`): início e fim em hora local, "sim" e o tempo
    restante no bloco ativo, entrada com o cache, saída e custo (equivalente de API)."""
    return [(datetime.datetime.fromisoformat(row["start"]).strftime("%Y-%m-%d %H:%M"),
             datetime.datetime.fromisoformat(row["end"]).strftime("%Y-%m-%d %H:%M"),
             "sim" if row["active"] else "", remaining_text(row["remaining_seconds"]) if row["active"] else "",
             tokens(row["total"]["input_tokens"] + row["total"]["cache_read"] + row["total"]["cache_write"]),
             tokens(row["total"]["output_tokens"]), _spent(row["total"])) for row in rows]


def usage_blocks_text(rows: list[dict]) -> str:
    """Os blocos de 5 h separados por tabulação, no formato de `usage_days_text`."""
    return "\n".join(["\t".join(USAGE_BLOCKS_HEADER)] + ["\t".join(row) for row in usage_blocks_rows(rows)])


SOURCE_COST_HEADER = ("Fonte", "Custo", "Limite", "Entrada", "Saída")


def currency_money(value: float, currency: str) -> str:
    """Valor na moeda do conector, no formato brasileiro (US$ 1.234,50 para USD; EUR 1.234,50 para as demais)."""
    return money(value) if currency == "USD" else money(value).replace("US$", currency, 1)


def source_costs_rows(costs: dict) -> list[tuple[str, str, str, str, str]]:
    """Linhas do custo por fonte que o conector devolveu (`connectors.normalize_costs`): fonte, custo, limite, entrada e
    saída ("—" onde o conector não informou), mais o total dos custos; sem fontes, nenhuma linha."""
    currency = costs["currency"]

    def tokens_or_dash(value: int | None) -> str:
        return "—" if value is None else tokens(value)
    rows = [(item["source"], currency_money(item["cost"], currency),
             "—" if item["limit"] is None else currency_money(item["limit"], currency),
             tokens_or_dash(item["tokens_in"]), tokens_or_dash(item["tokens_out"])) for item in costs["sources"]]
    if rows:
        rows.append(("Total", currency_money(sum(item["cost"] for item in costs["sources"]), currency), "—", "—", "—"))
    return rows


def source_costs_text(month: str, costs: dict) -> str:
    """O custo por fonte separado por tabulação (como `usage_text`): colado numa planilha, cada valor cai na sua coluna."""
    lines = [f"Mês {month}", "\t".join(SOURCE_COST_HEADER)]
    return "\n".join(lines + ["\t".join(row) for row in source_costs_rows(costs)])


# ----------------------------------------------------------------------------------------------- teto de gasto do mês
LIMIT_WARN_PERCENT = 80.0
LIMIT_OVER_PERCENT = 100.0


def parse_limit(text: str) -> float | None:
    """Teto mensal digitado (ponto ou vírgula decimal, sem separador de milhar); vazio ou "off" desliga (None)."""
    text = text.strip()
    if text.lower() in ("", "off"):
        return None
    try:
        value = float(text.replace(",", ".")) if text.count(",") + text.count(".") <= 1 else math.nan
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise LauncherError(f"Limite mensal inválido: {text!r}. Use um valor maior que zero (100 ou 100,50) ou off.")
    return value


def format_limit(value: float) -> str:
    """O teto como o campo da interface o mostra: o mesmo valor que parse_limit lê de volta."""
    return f"{value:.10g}".replace(".", ",")


def limit_status(report: dict, limit: float | None) -> dict | None:
    """Gasto do relatório frente ao teto: valor, percentual, nível (ok, warn a 80%, over a 100%) e se a soma é parcial
    (modelo sem preço fica fora do gasto). None quando o limite está desligado."""
    if limit is None:
        return None
    spent = (report.get("total") or {}).get("cost") or 0.0
    percent = round(spent * 100 / limit, 2)
    level = "over" if percent >= LIMIT_OVER_PERCENT else "warn" if percent >= LIMIT_WARN_PERCENT else "ok"
    return {"amount": limit, "currency": report.get("currency", "USD"), "spent": spent, "percent": percent,
            "partial": any(row["cost"] is None for row in report.get("models", [])), "level": level}


def limit_text(status: dict) -> str:
    """O gasto frente ao teto numa linha; diz "soma parcial" quando há modelo sem preço."""
    text = f"{money(status['spent'])} de {money(status['amount'])} ({int(status['percent'])}%)"
    if status["partial"]:
        text += " — soma parcial: modelo sem preço fica fora da conta e o gasto real pode ser maior"
    return text


def limit_warning(status: dict | None) -> str | None:
    """Aviso da faixa de status a partir dos 80% do teto; None abaixo disso ou com o limite desligado."""
    if status is None or status["level"] == "ok":
        return None
    prefix = "Teto de gasto do mês atingido" if status["level"] == "over" else "Teto de gasto do mês perto do limite"
    return f"{prefix}: {limit_text(status)}."


def source_limit_statuses(costs: dict) -> list[dict]:
    """Custo frente ao limite de cada fonte que o conector deu (`connectors.normalize_costs`): fonte, custo, limite,
    moeda, percentual e nível (ok, warn a 80%, over a 100%). Fonte sem limite ou com limite zero fica fora."""
    statuses = []
    for item in costs["sources"]:
        if not item["limit"]:
            continue
        percent = round(item["cost"] * 100 / item["limit"], 2)
        level = "over" if percent >= LIMIT_OVER_PERCENT else "warn" if percent >= LIMIT_WARN_PERCENT else "ok"
        statuses.append({"source": item["source"], "cost": item["cost"], "amount": item["limit"],
                         "currency": costs["currency"], "percent": percent, "level": level})
    return statuses


def source_limit_warning(status: dict, connection: str | None = None) -> str | None:
    """Texto do balão do limite de uma fonte, no estilo do aviso do teto; None abaixo dos 80%. Com `connection` o texto
    nomeia também a conexão (perfil com mais de uma)."""
    if status["level"] == "ok":
        return None
    state = "atingido" if status["level"] == "over" else "perto do limite"
    currency = status["currency"]
    owner = f" (conexão {connection})" if connection else ""
    return (f"Limite da fonte {status['source']}{owner} do mês {state}: {currency_money(status['cost'], currency)} de "
            f"{currency_money(status['amount'], currency)} ({int(status['percent'])}%).")