"""Processos em execução e o executável de cada um (Windows): quem está de fato rodando um arquivo."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE = ctypes.c_void_p(-1).value


class _Entry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def _image(kernel32, pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None  # processo de outro usuário ou de sistema: não é uma CLI do usuário
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def running() -> list[tuple[int, str]]:
    """(pid, caminho do executável) de cada processo que o usuário atual pode inspecionar."""
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE):
        return []
    found = []
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            image = _image(kernel32, entry.th32ProcessID)
            if image:
                found.append((entry.th32ProcessID, image))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def descendants(root: int) -> list[int]:
    """PIDs de todos os descendentes de `root` neste instante (filhos, netos…), pela tabela pai→filho do sistema."""
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE):
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    found, pending = [], [root]
    while pending:
        for pid in children.get(pending.pop(), []):
            if pid != root and pid not in found:
                found.append(pid)
                pending.append(pid)
    return found


def image_of(pid: int) -> str | None:
    """Caminho do executável do processo (None se morreu ou não pode ser inspecionado)."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    return _image(kernel32, pid)


class _RmUniqueProcess(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RmProcessInfo(ctypes.Structure):
    _fields_ = [("Process", _RmUniqueProcess), ("strAppName", wintypes.WCHAR * 256),
                ("strServiceShortName", wintypes.WCHAR * 64), ("ApplicationType", ctypes.c_int),
                ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]


def file_users(path: Path) -> list[int]:
    """PIDs dos processos que estão com o arquivo aberto agora (Restart Manager do Windows). Erro da API vira OSError."""
    if os.name != "nt":
        return []
    manager = ctypes.WinDLL("rstrtmgr")
    session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(64)
    if code := manager.RmStartSession(ctypes.byref(session), 0, key):
        raise OSError(f"RmStartSession falhou (código {code})")
    try:
        files = (wintypes.LPCWSTR * 1)(str(path))
        if code := manager.RmRegisterResources(session, 1, files, 0, None, 0, None):
            raise OSError(f"RmRegisterResources falhou para {path} (código {code})")
        capacity = 8
        while True:
            needed, count, reasons = wintypes.UINT(), wintypes.UINT(capacity), wintypes.DWORD()
            infos = (_RmProcessInfo * capacity)()
            code = manager.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
            if code == 234:  # ERROR_MORE_DATA: a lista cresceu entre as chamadas
                capacity = needed.value + 4
                continue
            if code:
                raise OSError(f"RmGetList falhou para {path} (código {code})")
            return [infos[index].Process.dwProcessId for index in range(count.value)]
    finally:
        manager.RmEndSession(session)


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def running_from(paths, processes=running) -> list[tuple[int, str]]:
    """Processos cujo executável é exatamente um dos `paths` (comparação pelo caminho real, sem diferença de caixa e
    de atalho). Nome parecido em outra pasta não conta, nem arquivo que apenas está aberto por outro programa."""
    wanted = {_key(path) for path in paths if Path(path).exists()}
    return [(pid, image) for pid, image in processes() if _key(image) in wanted] if wanted else []
