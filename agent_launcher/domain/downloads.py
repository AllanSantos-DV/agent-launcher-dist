"""Download que só segue redirecionamento para onde se espera: o host pedido e os que o GitHub usa para servir release."""
from __future__ import annotations

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
