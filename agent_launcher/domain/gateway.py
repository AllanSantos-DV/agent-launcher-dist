"""Uma conexão pronta para uso: conector, opções com segredos, protocolos servidos, destinos atendidos e catálogo."""
from __future__ import annotations

import hashlib
import json
import time

from agent_launcher.connectors import PROTOCOLS, ConnectorError, normalize_targets
from agent_launcher.connectors._http import NO_KEY
from agent_launcher.domain.catalog import Catalog, ModelInfo
from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json

CACHE_TTL = 6 * 60 * 60


class Gateway:
    """targets: o formato da 1.0 ({"claude", "codex", "any_model", "claude_auth"}), mantido para o --dry-run e para
    quem já o lê; protocols: as URLs por protocolo; clis: os destinos que consomem algum desses protocolos."""

    def __init__(self, store, connection: dict):
        self.store = store
        self.connection = connection
        modules, errors = store.connectors()
        self.module = modules.get(connection["connector"])
        if self.module is None:
            raise LauncherError(f"O conector {connection['connector']!r} da conexão {connection['name']} não está "
                                "disponível." + (f" Erros de carga: {'; '.join(errors)}" if errors else ""))
        self.options = store.connection_options(connection)
        try:
            normalized = normalize_targets(self.module.targets(self.options))
        except ConnectorError as error:
            raise LauncherError(f"{connection['name']}: {error}") from error
        self.protocols = {protocol: normalized[protocol] for protocol in PROTOCOLS if normalized[protocol]}
        self.any_model = normalized["any_model"]
        self.messages_auth = normalized["messages_auth"]
        self.targets = {"claude": normalized["messages"], "codex": normalized["responses"],
                        "any_model": self.any_model, "claude_auth": self.messages_auth}
        from agent_launcher.harnesses import registry
        self.clis = frozenset(harness.ID for harness in registry.all() if harness.available_for(self.protocols))
        self.cache_path = store.home / "cache" / f"models-{connection['id']}.json"
        plain = json.dumps([connection["connector"], connection["options"]], sort_keys=True)
        self.fingerprint = hashlib.sha256(plain.encode()).hexdigest()

    @classmethod
    def for_project(cls, store, project: dict | None = None, reference: str | None = None) -> Gateway:
        return cls(store, store.connection_for(project, reference))

    @property
    def name(self) -> str:
        return self.connection["name"]

    @property
    def url(self) -> str:
        return self.targets.get("claude") or self.targets.get("codex") or self.protocols.get("chat_completions") or ""

    @property
    def token(self) -> str:
        return self.options.get("api_key") or NO_KEY

    @property
    def key(self) -> str:
        """A chave real da conexão, ou "" (host que já injeta a autenticação ou não exige chave)."""
        return self.options.get("api_key") or ""

    def verify(self) -> None:
        try:
            self.module.verify(self.options)
        except ConnectorError as error:
            raise LauncherError(f"{self.name}: {error}") from error

    def empty_catalog(self, records=(), status="Catálogo indisponível; capacidades não confirmadas.", verified=False) -> Catalog:
        return Catalog(list(records), status, verified, self.clis, self.any_model, frozenset(self.protocols))

    def _save(self, records, fetched_at: float, metadata: bool) -> None:
        atomic_json(self.cache_path, {"schema": 3, "fingerprint": self.fingerprint, "fetched_at": fetched_at,
                                      "metadata_available": metadata,
                                      "records": [record.to_cache() for record in records]})

    def catalog(self, refresh: bool = False, offline: bool = False, save_cache: bool = True) -> Catalog:
        cached = self.empty_catalog()
        fetched_at = 0
        metadata_available = False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("schema") == 3 and data.get("fingerprint") == self.fingerprint:
                fetched_at = float(data["fetched_at"])
                cached.records = [ModelInfo.from_cache(record) for record in data["records"]]
                metadata_available = data.get("metadata_available") is True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            cached = self.empty_catalog()
        age = time.time() - fetched_at
        fresh = 0 <= age < CACHE_TTL
        cached.verified = bool(cached.records and metadata_available and fresh)
        if cached.records:
            cached.status = (f"Cache local • {int(age / 60)} min • atualizar: r" if fresh else
                             "AVISO: cache desatualizado; limites de contexto não serão configurados.")
            if not metadata_available:
                cached.status += " • host sem metadados de contexto"
        if offline:
            return cached
        if cached.records and not refresh and fresh and (metadata_available or age < 60):
            return cached
        try:
            records, metadata_available = self.module.models(self.options)
            if not records:
                raise ConnectorError("o host não retornou modelos para este acesso.")
        except ConnectorError as error:
            if cached.records:
                cached.verified = False
                cached.status = f"AVISO: catálogo em cache sem confirmação; contexto não será configurado. {error}"
                if save_cache:
                    try:
                        self._save(cached.records, fetched_at, False)
                    except OSError:
                        pass
                if not refresh:
                    return cached
            raise LauncherError(f"{self.name}: {error}") from error
        status = ("Catálogo ao vivo • limites de contexto informados pelo host" if metadata_available else
                  "Catálogo ao vivo • o host não informa contexto; os CLIs usam seus próprios limites")
        catalog = self.empty_catalog(records, status, metadata_available)
        if save_cache:
            try:
                self._save(records, time.time(), metadata_available)
            except OSError:
                catalog.status += " • não foi possível salvar o cache"
        return catalog
