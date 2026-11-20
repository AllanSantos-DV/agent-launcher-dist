"""Cores da interface por esquema (tema e cor de destaque), sem depender do Qt.

A fonte única é a paleta clara verde escrita em theme.py (o `STYLE` e as constantes). Cada cor dela tem um par escuro em
`DARK`; as três que servem de fundo e de texto ao mesmo tempo (`TEXT_ROLE`) têm um segundo par para o papel de texto.
A cor de destaque gira o matiz das cores não semânticas e devolve cada uma com a luminância que tinha, para o contraste
do verde valer também no azul, no roxo e no grafite. Erro, aviso e informação não giram."""
from __future__ import annotations

import colorsys
import re
from functools import lru_cache

DARK = {
    "#f5f6f1": "#121915", "#ffffff": "#19231e", "#fafbf7": "#161f1a", "#eef1e9": "#232f28", "#edf1e7": "#1f2b25",
    "#ebeee7": "#1f2a24", "#e9efdf": "#2a4034", "#e6ecdf": "#25362d", "#e3eddf": "#213a2e", "#eef2e9": "#1d2d25",
    "#dfecda": "#2a4a3a", "#dce8d5": "#345240", "#dcebcf": "#2f5645", "#d5e6cf": "#355a47", "#dfe7dc": "#2a3a31",
    "#dbe0d4": "#2c3a32", "#d4dfd0": "#27342c", "#d8e1d7": "#34463b", "#d5ded1": "#33443a", "#b7cdb0": "#3f6652",
    "#b8c5b6": "#3f4f45", "#abc19f": "#4f7360", "#adc0aa": "#4b6355", "#a3b3a8": "#6a8074", "#99bd7e": "#a5d18a",
    "#749965": "#7fae8e", "#60725f": "#94a89b", "#5f6c5d": "#8a9a8f", "#526659": "#a6bbad", "#51664f": "#a9bdb0",
    "#4d624b": "#94a89b", "#233e32": "#34493d", "#193d2b": "#e8f4ec", "#1b3d2b": "#eaf5ee", "#286046": "#2f7a58",
    "#1c4c35": "#266447", "#193d32": "#0e1613", "#264c3f": "#17261f", "#2a5141": "#1e3329", "#305942": "#1f3a2e",
    "#41684c": "#2a4a3a", "#547954": "#3d6350", "#4c735a": "#3a6552", "#50715d": "#31503f", "#bbd7a8": "#a5d18a",
    "#c4d8c6": "#b3cbb7", "#c8d9c7": "#b9cfbd", "#cfdfce": "#c3d7c7", "#d6e4d6": "#d0e1d4", "#b5cdbf": "#a4bdae",
    "#edf5e6": "#e8f3ec", "#f0f5e9": "#ecf4ee", "#f2f7ec": "#eef6f0",
    "#a32018": "#ff9d90", "#fbe8e5": "#3a1d1a", "#eec3bd": "#6d3a34", "#f6d5d0": "#4e2622",
    "#7e4216": "#f0c58c", "#f4e5ce": "#382a15", "#e2c79f": "#6b5230",
    "#0d5568": "#8dd3e6", "#e1f0f3": "#12303a", "#b9d3da": "#2e5b69", "#cde5ea": "#1a4150",
}
TEXT_ROLE = {"#ffffff": "#ffffff", "#233e32": "#e5eee8", "#286046": "#86d4a4"}
SEMANTIC = frozenset({"#a32018", "#fbe8e5", "#eec3bd", "#f6d5d0", "#7e4216", "#f4e5ce", "#e2c79f",
                      "#0d5568", "#e1f0f3", "#b9d3da", "#cde5ea"})
ACCENTS = {"green": (0, 1.0), "blue": (65, 1.0), "purple": (125, 1.0), "graphite": (0, 0.1)}
TEXT_PROPERTIES = frozenset({"color", "selection-color", "placeholder-text-color"})
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
DECLARATION = re.compile(r"([\w-]+)(\s*:\s*)([^;{}]+)")

_scheme = (False, "green")


def set_scheme(dark: bool, accent: str) -> None:
    """Põe o esquema em vigor para `tone` e `toned`."""
    if accent not in ACCENTS:
        raise ValueError(f"Cor de destaque desconhecida: {accent}")
    global _scheme
    _scheme = (bool(dark), accent)


def scheme() -> tuple[bool, str]:
    """(escuro, cor de destaque) em vigor."""
    return _scheme


def luminance(color: str) -> float:
    """Luminância relativa (WCAG) de #rrggbb."""
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _hex(red: float, green: float, blue: float) -> str:
    return "#" + "".join(f"{round(value * 255):02x}" for value in (red, green, blue))


@lru_cache(maxsize=None)
def rotate(color: str, accent: str) -> str:
    """`color` com o matiz e a saturação da cor de destaque e a mesma luminância; cinza fica como está."""
    shift, saturation = ACCENTS[accent]
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    hue, lightness, chroma = colorsys.rgb_to_hls(*channels)
    if chroma == 0 or (shift, saturation) == (0, 1.0):
        return color
    hue, chroma = (hue + shift / 360) % 1.0, chroma * saturation
    target, low, high = luminance(color), 0.0, 1.0
    for _ in range(40):
        middle = (low + high) / 2
        if luminance(_hex(*colorsys.hls_to_rgb(hue, middle, chroma))) < target:
            low = middle
        else:
            high = middle
    return _hex(*colorsys.hls_to_rgb(hue, (low + high) / 2, chroma))


def tone(color: str, text: bool = False) -> str:
    """Cor da paleta clara (#rrggbb) no esquema em vigor. `text` pede o papel de texto, que difere do de fundo ou
    traço nas cores que fazem os dois. Cor fora da paleta levanta KeyError: cor que não é do tema (a do perfil) não
    passa por aqui."""
    key = color.lower()
    if key not in DARK:
        raise KeyError(f"Cor fora da paleta do tema: {color}")
    dark, accent = _scheme
    if not dark and accent == "green":
        return color
    base = (TEXT_ROLE.get(key, DARK[key]) if text else DARK[key]) if dark else key
    return base if key in SEMANTIC else rotate(base, accent)


@lru_cache(maxsize=None)
def _toned(css: str, dark: bool, accent: str) -> str:
    def declaration(match):
        name, colon, value = match.groups()
        text = name.lower() in TEXT_PROPERTIES
        return name + colon + HEX.sub(lambda found: tone(found.group(0), text), value)

    return DECLARATION.sub(declaration, css)


def toned(css: str) -> str:
    """Folha de estilo escrita com a paleta clara, com cada cor trocada pela do esquema em vigor."""
    return _toned(css, *_scheme)
