"""Janelas no Windows: sessões que abrem sem tomar o foco, e uma janela marcada sempre por cima.

- keep_behind: abre algo (aba do Windows Terminal ou console) e, se a janela nova tomar o foco do teclado, devolve o
  foco a quem estava na frente. Opcionalmente minimiza a janela nova (não cobre nada na tela; volta pela barra de
  tarefas) e a leva para outro monitor.
- Sempre por cima: um vigia oculto (instância única) mantém a janela com o título marcado como HWND_TOPMOST e reaplica
  se ela perder o estado ou for recriada. Só ordem Z: nunca toma o foco.
- Abrir por cima: tira o "sempre por cima" por um tempo, abre o item na frente e restaura quando o usuário volta para
  a janela marcada, passa para outra janela, o tempo acaba, pede "voltar" ou aperta Ctrl+Alt+J.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from agent_launcher import ROOT
from agent_launcher.domain.files import atomic_json, read_text_retrying

STATE_FILE = Path.home() / ".cli-launcher" / "topmost.json"
MUTEX_NAME = "Local\\agent-launcher-topmost"
FRONT_SECONDS = 300
POLL_SECONDS = 0.5
RESCAN_SECONDS = 30  # sem janela à vista, a varredura completa de segurança do vigia (o gancho avisa das novas)
STATE_RELOAD_SECONDS = 30  # o vigia relê o estado ao ser avisado da mudança e, por segurança, a cada tanto

SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOZORDER = 0x0001, 0x0002, 0x0010, 0x0004
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SW_SHOWMINNOACTIVE = 7
SW_SHOWNORMAL, SW_SHOWMINIMIZED = 1, 2
TERMINAL_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"
MIN_WIDTH, MIN_HEIGHT = 300, 150  # menor que isso (ou fora de todo monitor) o retângulo normal não serve para restaurar
GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x0008
VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_J, WM_HOTKEY, WM_QUIT = 0x1, 0x2, 0x4000, 0x4A, 0x0312, 0x0012
WINEVENT_SKIPOWNPROCESS = 0x0002
WAIT_TIMEOUT, MWMO_INPUTAVAILABLE, QS_ALLINPUT, FILE_NOTIFY_CHANGES = 258, 0x4, 0x4FF, 0x1 | 0x8 | 0x10

if os.name == "nt":
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.c_void_p]
    kernel32_errors = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32_errors.CreateMutexW.restype = wintypes.HANDLE
    kernel32_errors.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.FindFirstChangeNotificationW.restype = wintypes.HANDLE
    kernel32.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
    kernel32.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
    user32.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
    ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    MONITOR_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                      wintypes.LPARAM)


class WindowPlacement(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def window_title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def _visible_with_title(hwnd: int, title: str) -> bool:
    return bool(user32.IsWindowVisible(hwnd)) and window_title(hwnd) == title


def find_window(title: str) -> int | None:
    """Janela visível de nível superior com exatamente esse título. A achada antes vale enquanto continua visível com o
    título; senão procura entre todas, e o vigia (que ouve os eventos de janela) só repete a varredura a cada
    RESCAN_SECONDS, porque a janela nova ou renomeada já chega pelo gancho."""
    cached = _found_windows.get(title) if title else None
    if cached and _visible_with_title(cached, title):
        return cached
    _found_windows.pop(title, None)
    events = _window_events
    if title and events and not events.scan_due(title):
        return None
    hits = []

    def visit(hwnd, _):
        if _visible_with_title(hwnd, title):
            hits.append(hwnd)
        return True
    user32.EnumWindows(ENUM_PROC(visit), 0)
    found = hits[0] if hits else None
    if title and found:
        _found_windows[title] = found
    if events and title:
        events.scanned(title)
    return found


_found_windows: dict[str, int] = {}
_window_events: "WindowEvents | None" = None


class WindowEvents:
    """Eventos de janela do vigia (primeiro plano, janela mostrada, destruída e renomeada), cada um num gancho próprio,
    numa thread com laço GetMessage: é o laço que o sistema sempre acorda para entregar o evento. Uma janela que ganha
    o título procurado entra em `_found_windows` na hora e o vigia acorda pelo `signal`."""
    EVENTS = (0x0003, 0x8001, 0x8002, 0x800C)
    PROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
                              wintypes.DWORD, wintypes.DWORD) if os.name == "nt" else None

    def __init__(self):
        import threading  # só o vigia: a janela da sessão importa este módulo e tem orçamento de imports
        self.signal = kernel32_errors.CreateEventW(None, False, False, None)
        self.title, self.scanned_at, self.thread_id, self.hooked = "", 0.0, 0, False
        self._ready = threading.Event()
        self._callback = self.PROC(self._on_event)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def scan_due(self, title: str) -> bool:
        return title != self.title or time.monotonic() - self.scanned_at >= RESCAN_SECONDS

    def scanned(self, title: str) -> None:
        self.title, self.scanned_at = title, time.monotonic()

    def force_scan(self) -> None:
        self.scanned_at = 0.0

    def _on_event(self, _hook, event, hwnd, object_id, child_id, _thread, _time) -> None:
        try:
            if object_id or child_id or not hwnd:
                return
            if event == self.EVENTS[0]:
                kernel32.SetEvent(self.signal)
            elif event == self.EVENTS[1]:
                if _found_windows.get(self.title) == hwnd:
                    _found_windows.pop(self.title, None)
                    kernel32.SetEvent(self.signal)
            elif self.title and _found_windows.get(self.title) != hwnd and user32.GetAncestor(hwnd, 2) == hwnd \
                    and _visible_with_title(hwnd, self.title):
                _found_windows[self.title] = hwnd
                kernel32.SetEvent(self.signal)
        except Exception:  # um callback nunca deixa a exceção subir ao sistema
            pass

    def _loop(self) -> None:
        api = ctypes.WinDLL("user32")
        api.SetWinEventHook.restype = wintypes.HANDLE
        api.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, self.PROC, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.DWORD]
        self.thread_id = kernel32.GetCurrentThreadId()
        hooks = [api.SetWinEventHook(event, event, None, self._callback, 0, 0, WINEVENT_SKIPOWNPROCESS)
                 for event in self.EVENTS]
        self.hooked = all(hooks)
        self._ready.set()
        message = wintypes.MSG()
        while self.hooked and api.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        for hook in hooks:
            if hook:
                api.UnhookWinEvent(hook)

    def close(self) -> None:
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        kernel32.CloseHandle(self.signal)


def on_screen() -> set[int]:
    """Janelas de nível superior visíveis e não minimizadas agora."""
    hits = set()

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            hits.add(hwnd)
        return True
    user32.EnumWindows(ENUM_PROC(visit), 0)
    return hits


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def get_placement(hwnd: int) -> WindowPlacement:
    placement = WindowPlacement(length=ctypes.sizeof(WindowPlacement))
    user32.GetWindowPlacement(hwnd, ctypes.byref(placement))
    return placement


def rect_usable(rect: tuple[int, int, int, int], available: list[dict]) -> bool:
    """O retângulo "normal" de uma janela serve para restaurá-la: tamanho mínimo e à vista em algum monitor. Sem
    monitores conhecidos (consulta falhou) só vale o tamanho."""
    left, top, right, bottom = rect
    if right - left < MIN_WIDTH or bottom - top < MIN_HEIGHT:
        return False
    return not available or any(left < m["right"] and right > m["left"] and top < m["bottom"] and bottom > m["top"]
                                for m in available)


def default_rect(available: list[dict]) -> tuple[int, int, int, int]:
    """Retângulo padrão para uma janela com o normal corrompido: 70% da área de trabalho do monitor principal."""
    work = next((m for m in available if m["primary"]), available[0] if available else
                {"left": 0, "top": 0, "right": 1280, "bottom": 800})
    width, height = (work["right"] - work["left"]) * 7 // 10, (work["bottom"] - work["top"]) * 7 // 10
    left, top = work["left"] + (work["right"] - work["left"] - width) // 2, work["top"] + (work["bottom"] - work["top"] - height) // 2
    return left, top, left + width, top + height


def set_placement(hwnd: int, show_cmd: int, rect: tuple[int, int, int, int] | None = None) -> None:
    placement = get_placement(hwnd)
    placement.showCmd = show_cmd
    if rect:
        placement.rcNormalPosition = wintypes.RECT(*rect)
    user32.SetWindowPlacement(hwnd, ctypes.byref(placement))


def _sane_normal_rect(placement) -> tuple[int, int, int, int] | None:
    """Retângulo normal de substituição quando o do placement não cabe em nenhum monitor; None se ele presta."""
    rect = placement.rcNormalPosition
    available = monitors()
    if rect_usable((rect.left, rect.top, rect.right, rect.bottom), available):
        return None
    return default_rect(available)


def repair_placement(hwnd: int) -> bool:
    """Conserta a janela cujo retângulo "normal" virou o do estado minimizado (o Windows Terminal minimizado no meio
    da própria restauração fica em -32000 com 480x103: a janela não restaura nem maximiza e as abas nascem com 48x2).
    Grava um retângulo são sem mudar minimizada/normal/maximizada. Devolve se corrigiu."""
    placement = get_placement(hwnd)
    rect = _sane_normal_rect(placement)
    if rect is None:
        return False
    set_placement(hwnd, placement.showCmd, rect)
    return True


def heal_terminal_windows() -> int:
    """Antes de abrir uma aba: janelas do Windows Terminal com o retângulo normal corrompido são consertadas, para a
    aba não nascer minúscula. Só toca em janela já corrompida. Devolve quantas."""
    found = []

    def visit(hwnd, _):
        buffer = ctypes.create_unicode_buffer(80)
        user32.GetClassNameW(hwnd, buffer, 80)
        if buffer.value == TERMINAL_CLASS and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(ENUM_PROC(visit), 0)
    return sum(repair_placement(hwnd) for hwnd in found)


def minimize(hwnd: int) -> None:
    """Minimiza sem ativar (a janela fica na barra de tarefas). Pelo placement e com o retângulo normal conferido: um
    ShowWindow repetido enquanto o Windows Terminal se restaura gravava o retângulo de minimizada como o normal."""
    set_placement(hwnd, SW_SHOWMINNOACTIVE, _sane_normal_rect(get_placement(hwnd)))


def place_behind(hwnd: int, other: int) -> None:
    """Põe hwnd logo abaixo de other na ordem Z, sem ativar, mover nem redimensionar."""
    user32.SetWindowPos(hwnd, other, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def bring_back(hwnd: int) -> bool:
    """Devolve o foco a hwnd. Com o usuário digitando, o Windows recusa SetForegroundWindow a quem não está na frente
    (trava de primeiro plano); um Alt sintético em volta da chamada libera a troca (o mesmo que o WinActivate do
    AutoHotkey faz). Devolve se hwnd ficou na frente."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.SetForegroundWindow(hwnd) and foreground() == hwnd:
        return True
    user32.keybd_event(VK_MENU, 0, 0, 0)
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return foreground() == hwnd


def monitors() -> list[dict]:
    """Monitores: [{"handle", "primary", "left", "top", "right", "bottom"}] (área de trabalho, sem a barra de tarefas)."""
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    found = []

    def visit(handle, _dc, _rect, _data):
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            work = info.rcWork
            found.append({"handle": handle, "primary": bool(info.dwFlags & 1), "left": work.left, "top": work.top,
                          "right": work.right, "bottom": work.bottom})
        return True
    user32.EnumDisplayMonitors(None, None, MONITOR_PROC(visit), 0)
    return found


def pick_monitor(available: list[dict], other_monitor: bool, avoid: int | None = None) -> dict | None:
    """Monitor para as sessões: outro que não o da janela marcada (avoid) ou, sem ela, um não principal. Nenhum se não
    foi pedido ou só existe um monitor (a janela fica onde abriu)."""
    if not other_monitor or len(available) < 2:
        return None
    if avoid:
        return next((monitor for monitor in available if monitor["handle"] != avoid), None)
    return next((monitor for monitor in available if not monitor["primary"]), None)


def move_to(hwnd: int, monitor: dict) -> None:
    """Leva a janela para a área de trabalho do monitor, sem mudar a ordem Z nem ativar (encolhe se não couber)."""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = min(rect.right - rect.left, monitor["right"] - monitor["left"])
    height = min(rect.bottom - rect.top, monitor["bottom"] - monitor["top"])
    user32.SetWindowPos(hwnd, None, monitor["left"], monitor["top"], width, height, SWP_NOZORDER | SWP_NOACTIVATE)


def keep_behind(open_window: Callable[[], object], title: str, other_monitor: bool = False,
                timeout: float = 15, settle: float = 2.5, minimized: bool = False) -> dict:
    """Abre (open_window) sem tirar o foco do teclado de quem está na frente: espera a janela com esse título aparecer
    e, por settle segundos, devolve o foco se ela o tomar (o Windows Terminal ativa a janela ao abrir uma aba e não tem
    opção para não ativar). Com minimized, a janela nova é minimizada assim que aparece (e de novo se ela se reabrir
    nesse tempo); se ela já estava na tela antes (outra aba da mesma janela), só volta para trás de quem estava na
    frente. Não achar a janela não é erro da sessão (ela abriu); o resultado diz isso."""
    previous = foreground()
    shown = on_screen() if minimized else set()
    heal_terminal_windows()
    result = open_window()
    report = {"previous": previous, "window": None, "restored": False, "moved": False, "minimized": False,
              "result": result}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not report["window"]:
        report["window"] = find_window(title)
        if not report["window"]:
            time.sleep(0.01 if minimized else 0.05)
    window = report["window"]
    if not window:
        return report
    hide = minimized and window not in shown
    if hide:
        minimize(window)
        report["minimized"] = True
    title_marked = read_state().get("title")
    marked = find_window(title_marked) if title_marked else None
    monitor = pick_monitor(monitors(), other_monitor, user32.MonitorFromWindow(marked, 0) if marked else None)
    if monitor:
        move_to(window, monitor)
        report["moved"] = True
    # Nada na frente, ou quem estava na frente já era a janela das sessões: o foco fica como está.
    watch_focus = bool(previous) and previous != window
    if minimized and not hide and watch_focus:
        place_behind(window, previous)
    if not watch_focus and not hide:
        report["repaired"] = repair_placement(window)
        return report
    end = time.monotonic() + settle
    while time.monotonic() < end:
        if watch_focus and foreground() == window:
            report["restored"] = bring_back(previous) or report["restored"]
        if hide and not is_minimized(window):
            minimize(window)
        time.sleep(0.01 if hide else 0.05)
    report["repaired"] = repair_placement(window)
    return report


# ---- sempre por cima --------------------------------------------------------------------------------------------

def read_state(path: Path | None = None) -> dict:
    try:
        return json.loads(read_text_retrying(path or STATE_FILE))
    except (OSError, ValueError):
        return {}


def write_state(state: dict, path: Path | None = None) -> None:
    atomic_json(path or STATE_FILE, state)  # temporário de nome próprio: vigia e CLIs gravam ao mesmo tempo


def is_topmost(hwnd: int) -> bool:
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def set_topmost(hwnd: int, on: bool) -> None:
    """Liga ou desliga o "sempre por cima" sem ativar, mover ou redimensionar."""
    user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def restore_reason(state: dict, front: int, marked: int, now: float) -> str | None:
    """Por que encerrar o "abrir por cima" agora (ou None para continuar): o tempo acabou, o usuário voltou para a
    janela marcada, ou trocou o item aberto por outra janela."""
    if now >= state.get("front_until", 0):
        return "tempo"
    if front and front == marked:
        return "voltou"
    opened = state.get("front_window")
    if opened and front and front != opened:
        return "outra janela"
    return None


def topmost_step(state: dict, path: Path | None = None, now: float | None = None) -> str:
    """Uma volta do vigia; devolve o que fez (para testes e diagnóstico)."""
    now = time.time() if now is None else now
    marked = find_window(state.get("title") or "")
    if not marked:
        return "sem janela"
    if state.get("front_until"):
        front = foreground()
        if not state.get("front_window") and front and front != marked and now - state.get("front_since", now) >= 1:
            state["front_window"] = front  # o item aberto por cima (primeira janela na frente depois de abrir)
            write_state(state, path)
        reason = restore_reason(state, front, marked, now)
        if not reason:
            if is_topmost(marked):
                set_topmost(marked, False)
            return "pausado"
        for key in ("front_until", "front_since", "front_window"):
            state.pop(key, None)
        write_state(state, path)
        set_topmost(marked, True)
        return f"restaurado ({reason})"
    if not is_topmost(marked):
        set_topmost(marked, True)
        return "aplicado"
    return "ok"


def mutex_name(path: Path | None = None) -> str:
    """Um vigia por arquivo de estado (os testes usam arquivos próprios e não esbarram no vigia de verdade). O estado
    padrão tem sempre o mesmo nome, sem sufixo, seja ele pedido por --state ou não: o vigia nativo (que recebe sempre
    --state) e o Python nunca rodam juntos."""
    if path is None or Path(path) == STATE_FILE:
        return MUTEX_NAME
    return f"{MUTEX_NAME}-{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"


def note_watch_error(error: Exception, log: Path | None = None) -> None:
    """O vigia é um processo oculto: o erro vai para topmost.log (só o último), não para o vazio."""
    target = log or STATE_FILE.with_name("topmost.log")
    try:
        target.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(error).__name__}: {error}\n", encoding="utf-8")
    except OSError:
        pass


def _start_watching(path: Path | None) -> tuple["WindowEvents", int | None]:
    """Liga os eventos de janela e o aviso de mudança da pasta do estado do vigia. O que não ligar vai ao topmost.log e
    o vigia segue pelo caminho antigo (procura a janela e relê o estado a cada passo)."""
    global _window_events
    log = path.with_name("topmost.log") if path else None
    events = WindowEvents()
    if events.hooked:
        _window_events = events
    else:
        note_watch_error(RuntimeError("SetWinEventHook recusado: o vigia procura a janela a cada passo"), log)
    change = kernel32.FindFirstChangeNotificationW(str((path or STATE_FILE).parent), False, FILE_NOTIFY_CHANGES)
    if change in (None, 0, ctypes.c_void_p(-1).value):
        note_watch_error(RuntimeError("FindFirstChangeNotification recusado: o estado é relido a cada meio segundo"), log)
        change = None
    return events, change


def wait_for_activity(change: int | None, events: "WindowEvents | None") -> bool:
    """Dorme até a mudança do estado (devolve verdadeiro), um evento de janela que importa, uma mensagem (Ctrl+Alt+J) ou
    o batimento de POLL_SECONDS (que reaplica o "sempre por cima" perdido, coisa que nenhum evento avisa). Resultado
    inesperado da espera vale como mudança, para o estado ser relido."""
    handles = [handle for handle in (change, events.signal if events else None) if handle]
    array = (wintypes.HANDLE * max(len(handles), 1))(*handles)
    result = user32.MsgWaitForMultipleObjectsEx(len(handles), array, int(POLL_SECONDS * 1000), QS_ALLINPUT,
                                                MWMO_INPUTAVAILABLE)
    if result == WAIT_TIMEOUT:
        return False
    if change and result == 0:
        kernel32.FindNextChangeNotification(change)
        return True
    if result == len(handles) or (events and result == len(handles) - 1):
        return False
    time.sleep(POLL_SECONDS)
    return True


def topmost_watch(path: Path | None = None, hotkey: bool = True) -> int:
    """Laço do vigia (processo oculto). Instância única por mutex; sai quando o estado é desligado e tira o "sempre
    por cima" da janela. Ctrl+Alt+J encerra o "abrir por cima" e devolve o foco à janela marcada. Dorme até um evento
    (estado mudou, janela marcada apareceu, Ctrl+Alt+J) com um batimento de POLL_SECONDS."""
    global _window_events
    mutex = kernel32_errors.CreateMutexW(None, False, mutex_name(path))
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: outro vigia já cuida
        kernel32.CloseHandle(mutex)
        return 0
    registered = hotkey and user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_J)
    message = wintypes.MSG()
    events = change = None
    try:
        state = read_state(path)
        if state.get("enabled"):
            events, change = _start_watching(path)
        loaded_at = time.monotonic()
        while True:
            if not state.get("enabled"):
                if _window_events:
                    _window_events.force_scan()
                marked = find_window(state.get("title") or "")
                if marked:
                    set_topmost(marked, False)
                return 0
            try:
                resumed = False
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):  # PM_REMOVE
                    resumed = resumed or message.message == WM_HOTKEY
                if resumed:
                    topmost_resume(path)
                    if _window_events:
                        _window_events.force_scan()
                    marked = find_window(state.get("title") or "")
                    if marked:
                        user32.SetForegroundWindow(marked)  # quem recebeu a tecla pode trazer a janela para a frente
                    state, loaded_at = read_state(path), time.monotonic()
                topmost_step(state, path)
            except Exception as error:  # estado recusado mesmo com retry ou erro inesperado: registra e tenta no próximo ciclo
                note_watch_error(error, path.with_name("topmost.log") if path else None)
            if wait_for_activity(change, events) or not change or time.monotonic() - loaded_at >= STATE_RELOAD_SECONDS:
                state, loaded_at = read_state(path), time.monotonic()
    finally:
        _window_events = None
        if events:
            events.close()
        if change:
            kernel32.FindCloseChangeNotification(change)
        if registered:
            user32.UnregisterHotKey(None, 1)
        kernel32.CloseHandle(mutex)


def topmost_on(title: str, path: Path | None = None, spawn: bool = True) -> None:
    """Marca a janela com esse título como sempre por cima e garante o vigia rodando (oculto, sem console)."""
    if not title.strip():
        raise ValueError("Informe o título da janela.")
    state = read_state(path)
    state.update(enabled=True, title=title)
    write_state(state, path)
    if spawn:
        from agent_launcher.platform import native  # fora do topo: a janela da sessão importa este módulo e tem orçamento de imports
        watcher = native.executable("vigia")
        if watcher:
            command = [str(watcher), "--state", str(path or STATE_FILE)]
        else:
            from agent_launcher.session.window import console_python  # fora do topo: window importa este módulo
            python = console_python()
            pythonw = python.with_name("pythonw.exe")
            command = [str(pythonw if pythonw.is_file() else python), str(ROOT / "focus.py"), "--watch"]
            if path:
                command += ["--state", str(path)]
        subprocess.Popen(command, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)


def topmost_off(path: Path | None = None) -> None:
    """Desliga: o vigia tira o "sempre por cima" e sai."""
    state = read_state(path)
    state["enabled"] = False
    write_state(state, path)


def topmost_resume(path: Path | None = None) -> None:
    """Encerra um "abrir por cima" em andamento: o vigia volta a deixar a janela marcada por cima."""
    state = read_state(path)
    if state.get("front_until"):
        state["front_until"] = 1
        write_state(state, path)


def open_front(target: str, seconds: float = FRONT_SECONDS, opener: Callable[[str], object] | None = None,
               path: Path | None = None) -> None:
    """Abre um item por cima (pedido explícito do usuário): pausa o "sempre por cima" e abre pelo shell, que entrega
    o foco ao item. O vigia restaura sozinho (ver restore_reason)."""
    state = read_state(path)
    if state.get("enabled"):
        marked = find_window(state.get("title") or "")
        state.update(front_until=time.time() + seconds, front_since=time.time())
        state.pop("front_window", None)
        write_state(state, path)
        if marked:
            set_topmost(marked, False)  # já agora, sem esperar a próxima volta do vigia
    (opener or os.startfile)(target)


def watch_main(argv: list[str]) -> int:
    """O vigia (focus.py --watch da raiz, oculto)."""
    if "--watch" not in argv:
        return 2
    state_path = Path(argv[argv.index("--state") + 1]) if "--state" in argv else None
    return topmost_watch(state_path, hotkey="--no-hotkey" not in argv)
