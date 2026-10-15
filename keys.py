"""Teclas para uma sessão aberta: responde ou dispensa um diálogo (confiança de pasta, pedido de permissão, pergunta)
sem ninguém na frente da janela.

Como: o processo da janela da sessão (desktop.py) fica registrado em ~/.cli-launcher/sessions/<pid>.json. Para enviar,
um processo auxiliar sem janela se desanexa do próprio console, anexa-se ao console da sessão (AttachConsole) e escreve
eventos de teclado no buffer de entrada dele (WriteConsoleInputW). Isso vale para o console real e para o pseudoconsole
(ConPTY) do Windows Terminal, não depende de foco, de a janela estar visível ou minimizada, nem de SendInput (que só
atinge a janela em primeiro plano). Ler a tela (ReadConsoleOutputCharacterW) usa o mesmo caminho.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

SESSIONS_DIR = Path.home() / ".cli-launcher" / "sessions"
KEY_DELAY = 0.05  # entre uma tecla e outra: o CLI lê cada uma como um evento separado

# nome -> (código virtual, caractere, tecla estendida, estado dos modificadores)
SHIFT_PRESSED, LEFT_CTRL_PRESSED, ENHANCED_KEY = 0x10, 0x08, 0x100
NAMED = {
    "enter": (0x0D, "\r", False, 0), "esc": (0x1B, "\x1b", False, 0), "tab": (0x09, "\t", False, 0),
    "shift+tab": (0x09, "\t", False, SHIFT_PRESSED), "backspace": (0x08, "\x08", False, 0),
    "space": (0x20, " ", False, 0), "ctrl+c": (0x43, "\x03", False, LEFT_CTRL_PRESSED),
    "up": (0x26, "\0", True, 0), "down": (0x28, "\0", True, 0), "left": (0x25, "\0", True, 0),
    "right": (0x27, "\0", True, 0), "home": (0x24, "\0", True, 0), "end": (0x23, "\0", True, 0),
    "pageup": (0x21, "\0", True, 0), "pagedown": (0x22, "\0", True, 0), "delete": (0x2E, "\0", True, 0),
}


class KeysError(Exception):
    pass


def parse(tokens: list[str]) -> list[tuple[int, str, bool, int]]:
    """Tokens -> teclas. Aceita nomes (enter, esc, up, down, left, right, tab, shift+tab, space, backspace, ctrl+c, home,
    end, pageup, pagedown, delete), um caractere (ex.: 1, y) e text:ALGO (digita ALGO). Qualquer outro é erro."""
    keys: list[tuple[int, str, bool, int]] = []
    for token in tokens:
        name = token.lower()
        if name in NAMED:
            keys.append(NAMED[name])
        elif token.startswith("text:") and len(token) > 5:
            keys += [typed(char) for char in token[5:]]
        elif len(token) == 1:
            keys.append(typed(token))
        else:
            raise KeysError(f"Tecla desconhecida: {token!r}. Use um nome ({', '.join(NAMED)}), um caractere ou text:ALGO.")
    if not keys:
        raise KeysError("Nenhuma tecla informada.")
    return keys


def typed(char: str) -> tuple[int, str, bool, int]:
    vk = ctypes.windll.user32.VkKeyScanW(char) if os.name == "nt" else -1
    if vk == -1:  # sem tecla no layout: vai só o caractere (VK 0), como colar
        return (0, char, False, 0)
    return (vk & 0xFF, char, False, SHIFT_PRESSED if vk & 0x100 else 0)


# ---- registro das sessões abertas -------------------------------------------------------------------------------

def alive(pid: int) -> bool:
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def register(pid: int, project: str, project_id: str, cli: str, title: str, controlled: bool,
             folder: Path | None = None) -> Path:
    target = (folder or SESSIONS_DIR) / f"{pid}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": pid, "project": project, "project_id": project_id, "cli": cli,
                                     "title": title, "controlled": controlled, "startedAt": time.time()},
                                    ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return target


def unregister(pid: int, folder: Path | None = None) -> None:
    ((folder or SESSIONS_DIR) / f"{pid}.json").unlink(missing_ok=True)


def sessions(folder: Path | None = None) -> list[dict]:
    """Sessões abertas agora, da mais recente para a mais antiga. Registro de processo que já morreu é descartado."""
    found = []
    for path in (folder or SESSIONS_DIR).glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            pid = int(item["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if alive(pid):
            found.append(item)
        else:
            path.unlink(missing_ok=True)
    return sorted(found, key=lambda item: item.get("startedAt", 0), reverse=True)


def resolve(project_ids: set[str], cli: str | None = None, pid: int | None = None,
            folder: Path | None = None) -> dict:
    """A sessão certa: por pid, ou por projeto (ids que o identificam) e CLI. Zero ou mais de uma é erro, com a lista."""
    available = sessions(folder)
    if pid is not None:
        hits = [item for item in available if item["pid"] == pid]
    else:
        hits = [item for item in available if item["project_id"] in project_ids and (not cli or item["cli"] == cli)]
    if len(hits) == 1:
        return hits[0]
    listing = ", ".join(f"pid {item['pid']} ({item['title']})" for item in hits or available) or "nenhuma"
    raise KeysError(("Nenhuma sessão aberta corresponde. Abertas: " if not hits else
                     "Mais de uma sessão corresponde; use --pid. Candidatas: ") + listing)


# ---- escrita e leitura no console da sessão ---------------------------------------------------------------------

class KeyEvent(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class InputRecord(ctypes.Structure):
    class _Event(ctypes.Union):
        _fields_ = [("key", KeyEvent), ("pad", ctypes.c_ubyte * 16)]
    _anonymous_ = ("event",)
    _fields_ = [("EventType", wintypes.WORD), ("event", _Event)]


class Coord(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class ScreenInfo(ctypes.Structure):
    _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", wintypes.WORD), ("window", wintypes.SMALL_RECT),
                ("maximum", Coord)]


def attached(pid: int):
    """Anexa este processo ao console de pid e devolve (entrada, saída). Quem chama desanexa no fim."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        raise KeysError(f"Não foi possível anexar ao console da sessão (pid {pid}): erro {ctypes.get_last_error()}.")
    handles = []
    for name in ("CONIN$", "CONOUT$"):
        handle = kernel32.CreateFileW(name, 0xC0000000, 3, None, 3, 0, None)  # GENERIC_READ|WRITE, SHARE_READ|WRITE
        if handle in (None, wintypes.HANDLE(-1).value):
            raise KeysError(f"Não foi possível abrir {name} da sessão (pid {pid}): erro {ctypes.get_last_error()}.")
        handles.append(handle)
    return kernel32, handles[0], handles[1]


def inject(pid: int, keys: list[tuple[int, str, bool, int]]) -> None:
    kernel32, console_in, console_out = attached(pid)
    try:
        for vk, char, extended, modifiers in keys:
            state = modifiers | (ENHANCED_KEY if extended else 0)
            scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0) if vk else 0
            pair = (InputRecord * 2)()
            for index, down in enumerate((True, False)):
                pair[index].EventType = 1  # KEY_EVENT
                pair[index].key = KeyEvent(down, 1, vk, scan, char, state)
            written = wintypes.DWORD()
            if not kernel32.WriteConsoleInputW(console_in, pair, 2, ctypes.byref(written)) or written.value != 2:
                raise KeysError(f"Escrita no console da sessão falhou: erro {ctypes.get_last_error()}.")
            time.sleep(KEY_DELAY)
    finally:
        kernel32.CloseHandle(console_in)
        kernel32.CloseHandle(console_out)
        kernel32.FreeConsole()


def screen(pid: int) -> str:
    """O que a sessão mostra agora (a parte visível do console), uma linha por linha, sem espaços à direita."""
    kernel32, console_in, console_out = attached(pid)
    try:
        info = ScreenInfo()
        if not kernel32.GetConsoleScreenBufferInfo(console_out, ctypes.byref(info)):
            raise KeysError(f"Leitura da tela da sessão falhou: erro {ctypes.get_last_error()}.")
        width = info.window.Right - info.window.Left + 1
        lines = []
        for row in range(info.window.Top, info.window.Bottom + 1):
            buffer = ctypes.create_unicode_buffer(width)
            read = wintypes.DWORD()
            kernel32.ReadConsoleOutputCharacterW(console_out, buffer, width, Coord(info.window.Left, row),
                                                 ctypes.byref(read))
            lines.append(buffer.value.rstrip())
        return "\n".join(lines).rstrip()
    finally:
        kernel32.CloseHandle(console_in)
        kernel32.CloseHandle(console_out)
        kernel32.FreeConsole()


def run_helper(arguments: list[str], timeout: float = 30) -> str:
    """Executa o auxiliar (este arquivo) num processo sem janela e sem console próprio: AttachConsole só funciona para
    quem não tem console, e assim o console de quem chamou (o do orquestrador) não é tocado."""
    python = Path(sys.executable)
    command = [str(python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python), "-I",
               str(Path(__file__).resolve()), *arguments]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise KeysError((result.stderr or result.stdout).strip() or f"auxiliar saiu com código {result.returncode}")
    return result.stdout


def send(pid: int, tokens: list[str]) -> list[str]:
    parse(tokens)  # erra aqui, antes de abrir o auxiliar, se alguma tecla for inválida
    run_helper(["--inject", str(pid), *tokens])
    return tokens


def read_screen(pid: int) -> str:
    return run_helper(["--screen", str(pid)])


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--inject":
            inject(int(sys.argv[2]), parse(sys.argv[3:]))
        elif len(sys.argv) == 3 and sys.argv[1] == "--screen":
            sys.stdout.buffer.write(screen(int(sys.argv[2])).encode("utf-8"))
        else:
            raise KeysError("uso interno: keys.py --inject PID TECLA... | --screen PID")
    except KeysError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
