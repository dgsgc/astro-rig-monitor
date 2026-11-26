"""What the scheduled task runs: keeps the PC awake and runs the monitor, restarting it if it stops.

    pythonw -s -B -m armwin.launcher

The monitor runs as a child process in a Windows job object, so ending the task (Restart Monitor,
Run Monitor off, an update) always ends the monitor with it. With no config.toml yet, the monitor
serves its setup page on 8080, or the next free port up to 8099.
"""

import ctypes
import os
import socket
import subprocess
import sys
import time
from ctypes import wintypes

from . import paths

RESTART = 75                    # the monitor's exit code for "settings changed, start me again"
CREATE_NO_WINDOW = 0x08000000


def note(log, text: str):
    log.write(time.strftime("%Y-%m-%dT%H:%M:%S") + " launcher: " + text + "\n")
    log.flush()


def keep_awake():
    """Stop the PC idle-sleeping while this runs (the screen can still sleep)."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def kill_with_me_job():
    """A job object whose processes end when this process ends, however it ends."""
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    return job


def port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    paths.DATA.mkdir(parents=True, exist_ok=True)
    try:
        if paths.CONSOLE.stat().st_size > 1_000_000:
            paths.CONSOLE.unlink()
    except OSError:
        pass
    log = open(paths.CONSOLE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    keep_awake()
    job = kill_with_me_job()
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               ARM_RESTART_EXIT=str(RESTART), ARM_APP=paths.version())
    fails = 0
    while True:
        port = 8080
        if not paths.CONFIG.exists():
            port = next((p for p in range(8080, 8100) if port_free(p)), 8080)
            paths.SETUP_PORT.write_text(str(port), encoding="utf-8")
        note(log, "starting the monitor")
        started = time.time()
        child = subprocess.Popen(
            [str(paths.PYTHONW), *paths.PY_FLAGS, "-m", "arm", "--config", str(paths.CONFIG), "--setup-port", str(port)],
            cwd=str(paths.DATA), env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        ctypes.windll.kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(child._handle)))
        code = child.wait()
        if code == RESTART:
            note(log, "settings changed: restarting the monitor")
            continue
        note(log, f"monitor stopped (status {code}); starting it again shortly")
        fails = fails + 1 if time.time() - started < 60 else 0
        time.sleep(min(10 * (fails + 1), 120))      # back off if it keeps failing at once


if __name__ == "__main__":
    main()
