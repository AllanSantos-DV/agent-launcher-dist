"""Processos filhos que morrem com a janela (Job Object), árvore inteira encerrada e porta livre em 127.0.0.1."""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess

from agent_launcher.domain.errors import LauncherError


def kill_with_parent(process: subprocess.Popen):
    """Job Object com KILL_ON_JOB_CLOSE: se esta janela morrer (inclusive fechada no X), o app-server e os filhos dele morrem junto."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.OpenProcess.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise LauncherError("Não foi possível criar o Job Object do app-server.")

    class Limits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Limits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise LauncherError("Não foi possível configurar o Job Object do app-server.")
    handle = kernel32.OpenProcess(0x0001 | 0x0100, False, process.pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    try:
        if not handle or not kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle)):
            raise LauncherError("Não foi possível vincular o app-server a esta janela.")
    finally:
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    return job


def close_job(job) -> None:
    if job and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(job)


def stop_tree(process: subprocess.Popen) -> None:
    """Encerra o processo e todos os filhos (o shim do npm deixaria o codex.exe vivo com terminate())."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.terminate()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
