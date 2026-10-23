from __future__ import annotations

import json
from pathlib import Path

from agent_launcher.domain.errors import LauncherError
from agent_launcher.domain.files import atomic_json
from agent_launcher.platform import secret_store


class Vault:
    """Segredos das conexões em secrets.json, cada valor cifrado com DPAPI para o usuário Windows atual."""

    def __init__(self, home: Path):
        self.path = home / "secrets.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as error:
            raise LauncherError(f"Cofre de chaves ilegível em {self.path}; o arquivo foi preservado.") from error
        if not isinstance(data, dict) or not all(isinstance(value, str) for value in data.values()):
            raise LauncherError(f"Cofre de chaves fora do formato em {self.path}; o arquivo foi preservado.")
        return data

    def get(self, key: str) -> str:
        try:
            return secret_store.unprotect(self._read().get(key, ""))
        except secret_store.SecretProtectionError as error:
            raise LauncherError(f"Não foi possível abrir a chave salva ({error}). Edite a conexão e cole a chave de novo.") from error

    def set(self, key: str, value: str) -> None:
        data = self._read()
        if value:
            try:
                data[key] = secret_store.protect(value)
            except secret_store.SecretProtectionError as error:
                raise LauncherError(str(error)) from error
        else:
            data.pop(key, None)
        atomic_json(self.path, data)

    def forget(self, prefix: str) -> None:
        data = self._read()
        remaining = {key: value for key, value in data.items() if not key.startswith(prefix)}
        if remaining != data:
            atomic_json(self.path, remaining)
