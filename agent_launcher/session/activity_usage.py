"""Contexto usado e custo estimado da conversa de cada sessão aberta (`launcher status --usage`, dentro de `activity`).

A conversa de cada sessão é a que `activity.annotate` já acha; aqui só se lê o transcript dela com
`conversation_usage.conversation_usage` (cache por arquivo, o mesmo da lista de conversas) e se compara o contexto com a janela
do modelo da abertura (`window_warning`, catálogo em cache, sem rede). A leitura roda numa thread com teto de tempo para um
transcript grande não travar o `status`; o que não terminou no teto vira ausência explícita. O cache guarda cada transcript só
quando a leitura dele termina: o que ficou na fila continua do cache na leitura seguinte, mas um transcript que sozinho passa
do teto só entra no cache se o processo viver até o fim da leitura. Nunca há valor inventado: o que não se sabe é `null` e
`usage_reason` diz por quê."""
from __future__ import annotations

import threading

from agent_launcher.domain import conversation_usage as cu
from agent_launcher.domain import usage
from agent_launcher.domain.errors import LauncherError

NO_CONVERSATION = "conversa da sessão não identificada"
READING = object()  # marca do alvo cuja leitura começou e ainda não terminou


def absent(reason: str) -> dict:
    """As chaves de uso todas ausentes (null), com o motivo."""
    return {"context_tokens": None, "cost_usd": None, "window_warning": None, "usage_reason": reason}


def catalog_for(store, reference: str):
    """O catálogo em cache (sem rede) da conexão `reference` (id ou nome); LauncherError se a conexão não existe ou o
    conector não carrega."""
    from agent_launcher.domain.gateway import Gateway
    return Gateway(store, store.find_connection(reference)).catalog(offline=True)


def _window_warning(store, entry: dict, context: int, catalogs: dict) -> tuple[str | None, str | None]:
    """(aviso de janela, motivo de não haver janela a comparar) do modelo da abertura; sem conexão ou modelo gravados não
    há o que comparar e isso não é falha."""
    reference, model = entry.get("connection"), entry.get("model")
    if not reference or not model:
        return None, None
    if reference not in catalogs:
        try:
            catalogs[reference] = (catalog_for(store, reference), None)
        except LauncherError as error:
            catalogs[reference] = (None, f"janela do modelo indisponível: {error}")
    catalog, problem = catalogs[reference]
    if catalog is None:
        return None, problem
    return cu.window_warning(context, catalog.find(model), catalog.verified), None


def _read_one(store, cli: str, conversation_id: str, entry: dict, tables: dict, cache, catalogs: dict) -> dict:
    if cu.transcript(cli, conversation_id) is None:
        return absent("transcript da conversa não encontrado")
    result = cu.conversation_usage(store.home, cli, conversation_id, tables, cache)
    if result.context is None and result.cost is None:
        return absent("transcript sem uso registrado")
    reasons = []
    if result.cost is None:
        reasons.append("custo indisponível: algum modelo usado não tem preço no catálogo")
    warning = None
    if result.context is None:
        reasons.append("contexto não registrado no transcript")
    else:
        warning, problem = _window_warning(store, entry, result.context, catalogs)
        if problem:
            reasons.append(problem)
    return {"context_tokens": result.context, "cost_usd": None if result.cost is None else round(result.cost, 6),
            "window_warning": warning, "usage_reason": "; ".join(reasons) or None}


def read(store, targets: list[tuple[str, str | None, dict | None]], budget_s: float) -> list[dict]:
    """Uma resposta de uso por alvo `(cli, id da conversa ou None, abertura do registro do perfil)`, na mesma ordem. Erro de
    leitura de um alvo vira o motivo dele e não impede os outros; o que passar de `budget_s` segundos no total vira o motivo
    do teto."""
    found: list = [None if target[1] else absent(NO_CONVERSATION) for target in targets]
    if all(item is not None for item in found):
        return [item for item in found if item is not None]

    def work() -> None:
        try:
            cache, tables, catalogs = usage.FileCache(store.home), usage.price_table(store.home), {}
        except Exception as error:  # noqa: BLE001 - qualquer falha de preparo vira o motivo de cada alvo
            for index, item in enumerate(found):
                if item is None:
                    found[index] = absent(f"leitura do uso falhou: {type(error).__name__}: {error}")
            return
        for index, (cli, conversation_id, entry) in enumerate(targets):
            if found[index] is not None:
                continue
            found[index] = READING
            try:
                found[index] = _read_one(store, cli, conversation_id, entry or {}, tables, cache, catalogs)
            except Exception as error:  # noqa: BLE001 - o erro aparece no resultado, nunca é engolido
                found[index] = absent(f"leitura do uso falhou: {type(error).__name__}: {error}")
            cache.save()

    worker = threading.Thread(target=work, name="uso-das-sessoes", daemon=True)
    worker.start()
    worker.join(budget_s)
    limit = f"{budget_s:g}".replace(".", ",")
    snapshot = list(found)
    over = f"teto de {limit} s para ler o uso estourado"
    return [item if isinstance(item, dict) else
            absent(f"{over}: este transcript sozinho passou do teto e só entra no cache quando a leitura inteira termina, "
                   "então a próxima leitura pode estourar de novo" if item is READING else
                   f"{over}; a próxima leitura continua do cache") for item in snapshot]
