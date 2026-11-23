"""Download que só segue redirecionamento para onde se espera: o host pedido e os que o GitHub usa para servir release."""
from __future__ import annotations

import hashlib
from pathlib import Path

from agent_launcher.domain.files import replace_retrying

# Hosts que o GitHub usa para servir release e arquivo bruto: um redirecionamento só pode ir para eles ou para o host pedido.
GITHUB_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                "raw.githubusercontent.com")


def restricted_redirects(url: str, refuse, hosts: tuple[str, ...] = GITHUB_HOSTS):
    """Classe (sem argumentos) de handler para urllib.request.build_opener. Redirecionamento para fora de `hosts` e do
    host de `url`, ou para http fora do host de `url`, levanta `refuse(mensagem)` em vez de seguir."""
    import urllib.parse
    import urllib.request

    class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlsplit(newurl)
            origin = urllib.parse.urlsplit(url).hostname
            if target.hostname not in {origin, *hosts} or (target.scheme != "https" and target.hostname != origin):
                raise refuse(f"{url} redirecionou para {newurl}, fora do host pedido e dos hosts de download esperados; "
                             "download recusado.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    return RestrictedRedirect


class DownloadRefused(Exception):
    """Download interrompido: `kind` é "limit" (passou do teto) ou "sha256" (não confere com o publicado)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def save_verified(response, destination: Path, expected_sha256: str, limit: int, progress=None) -> None:
    """Grava `response` em `destination` por um arquivo `.parte`, que só ganha o nome final depois de o SHA-256 conferir;
    `progress(bytes_recebidos)` a cada bloco. Levanta DownloadRefused ao passar de `limit` bytes ou se o SHA-256 diverge;
    erro de disco sobe como OSError. O `.parte` nunca fica."""
    digest, received = hashlib.sha256(), 0
    partial = destination.with_name(destination.name + ".parte")
    try:
        with open(partial, "wb") as stream:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > limit:
                    raise DownloadRefused("limit")
                digest.update(chunk)
                stream.write(chunk)
                if progress:
                    progress(received)
        if digest.hexdigest() != expected_sha256.lower():
            raise DownloadRefused("sha256")
        replace_retrying(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
