"""Texto em PowerShell para colar: literais de aspas simples e o comando nativo com argumentos que chegam iguais no
PowerShell 5.1 e no 7.

O 5.1 não escapa aspas duplas ao montar a linha do processo e decide pôr o argumento entre aspas contando as `"` (a
`\\"` também) para saber se um espaço está "fora" delas. Por isso cada argumento vai já escapado no padrão do Windows e o
7.3+ é posto no modo `Legacy` (o mesmo algoritmo do 5.1) dentro de um bloco, sem alterar o shell de quem cola."""
from __future__ import annotations

import json
import re

from agent_launcher.domain.errors import LauncherError

LEGACY_PREFIX = "& { $PSNativeCommandArgumentPassing = 'Legacy'; "
_SINGLE_QUOTES = "'‘’‚‛"  # o PowerShell trata as aspas tipográficas simples como `'`
_TOML_ASSIGNMENT = re.compile(r'([\w.\-]+)="([^"\\\t\r\n]*)"')


def quote(value: str) -> str:
    """Literal de aspas simples: o apóstrofo (e as aspas tipográficas simples) dobrado, nada é interpretado."""
    return "'" + re.sub(f"([{_SINGLE_QUOTES}])", r"\1\1", value) + "'"


def _escaped(argument: str) -> str:
    """O argumento no padrão do Windows: `"` vira `\\"` e as barras invertidas antes dela, dobradas."""
    return re.sub(r'(\\*)"', lambda found: found.group(1) * 2 + '\\"', argument)


def _would_wrap(text: str) -> bool:
    """O 5.1 põe o argumento entre aspas quando há espaço fora de um par de `"` (conta também as `\\"`)."""
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        elif not quoted and char.isspace():
            return True
    return False


def _argument(argument: str) -> str:
    """O texto do argumento que o PowerShell entrega ao programa idêntico (JSON e valor TOML, com o mesmo valor).
    Se todos os espaços ficam entre `\\"`, o 5.1 não embrulha e o programa quebraria o argumento no espaço: o JSON ganha
    um espaço depois da chave de abertura (JSON ignora) e o valor TOML `chave="a b"` troca o espaço por `\\u0020`."""
    if argument in ("", "--%") or (argument.endswith("\\") and any(char.isspace() for char in argument)):
        raise LauncherError(f"O PowerShell 5.1 não passa o argumento {argument!r} ao programa; use --shell cmd.")
    text = _escaped(argument)
    if not any(char.isspace() for char in argument) or _would_wrap(text):
        return text
    if argument[:1] in "{[":
        try:
            json.loads(argument)
        except ValueError:
            pass
        else:
            spaced = _escaped(argument[0] + " " + argument[1:])
            if _would_wrap(spaced):
                return spaced
    assignment = _TOML_ASSIGNMENT.fullmatch(argument)
    if assignment:
        return _escaped(f'{assignment.group(1)}="{assignment.group(2).replace(" ", chr(92) + "u0020")}"')
    raise LauncherError(f"O PowerShell 5.1 não passa o argumento {argument!r} ao programa; use --shell cmd.")


def native_command(argv: list[str]) -> str:
    """A linha que roda `argv` como programa nativo: `& 'programa' 'arg' ...`, no bloco que fixa o modo `Legacy`."""
    return LEGACY_PREFIX + "& " + " ".join([quote(argv[0]), *(quote(_argument(part)) for part in argv[1:])]) + " }"
