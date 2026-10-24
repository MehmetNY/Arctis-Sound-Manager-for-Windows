"""Delete the POSIX-only names that Windows' Python simply does not have.

The Windows harnesses run on Linux, where ``os.getuid()`` or ``signal.SIGHUP`` always
exist, so code that calls them unguarded passes every simulated run and then dies with
an AttributeError on a real PC. Removing the names turns that class of bug into a loud
failure here. Applied before any ``arctis_sound_manager`` import.
"""
import os
import signal
import socket

# os.uname is kept: Linux's ctypes.util.find_library needs it, and on Windows that function takes another branch.
_OS = ("getuid", "geteuid", "getgid", "getegid", "getpgid", "setsid", "killpg", "fork", "forkpty",
       "mkfifo", "chown", "lchown", "nice", "sched_getaffinity", "sched_setaffinity")
_SIGNAL = ("SIGHUP", "SIGKILL", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGCHLD", "SIGPIPE", "SIGQUIT",
           "SIGTSTP", "SIGCONT")
# socket.AF_UNIX is deliberately kept: this Linux asyncio loop builds its self-pipe with it
# (Windows uses the Proactor loop instead), so removing it would only break the harness itself.
_SOCKET = ()


def apply() -> list[str]:
    removed = []
    for module, names in ((os, _OS), (signal, _SIGNAL), (socket, _SOCKET)):
        for name in names:
            if hasattr(module, name):
                delattr(module, name)
                removed.append(f"{module.__name__}.{name}")
    # Windows' os.py defines process_cpu_count as cpu_count when sched_getaffinity is missing; on
    # Linux it calls sched_getaffinity, which would now raise NameError inside asyncio.to_thread.
    if "os.sched_getaffinity" in removed and hasattr(os, "process_cpu_count"):
        os.process_cpu_count = os.cpu_count
    return removed
