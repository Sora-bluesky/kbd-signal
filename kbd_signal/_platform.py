"""Platform primitives — the single place OS branching lives.

Only the OS-specific *mechanisms* belong here (how to grab/release an
advisory lock, how to detach a child process, where the user data dir is).
The *policy* around them — the bounded lock retry + timeout in
kbd_signal.states, what to spawn, how config is merged — stays OS-common in
its own module and calls the stable interface below. Keeping the shared
policy single-sourced is what protects the already-working Windows path
from POSIX changes.
"""

import os
import subprocess
import sys

# Advisory-locking primitive differs by platform: msvcrt on Windows, fcntl
# on POSIX (macOS/Linux). Exactly one imports successfully; select once here.
try:
    import msvcrt
    _WIN = True
except ImportError:  # POSIX
    import fcntl
    _WIN = False


def try_lock(f):
    """Acquire an exclusive advisory lock on `f` without blocking. Raises
    OSError (as both msvcrt and fcntl already do) when another process holds
    it, which the caller's retry loop treats as "not yet"."""
    if _WIN:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(f):
    if _WIN:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def detach_kwargs():
    """Popen kwargs that fully detach a fire-and-forget child so it outlives
    the hook process."""
    if _WIN:
        # Detach with no console window so the resident restorer is invisible.
        return {"creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW}
    # New session so the child isn't tied to the hook's controlling terminal.
    return {"start_new_session": True}


def state_dir():
    """Per-OS directory for config/state/log, honouring the platform's
    conventional user data location:

      Windows  %LOCALAPPDATA%\\kbd-signal
      macOS    ~/Library/Application Support/kbd-signal
      Linux    $XDG_STATE_HOME/kbd-signal (or ~/.local/state/kbd-signal)
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support")
    else:  # linux and other POSIX
        # XDG spec: a relative $XDG_STATE_HOME is ignored (it would tie the
        # state dir to the process CWD); fall back to the default in that case.
        xdg = os.environ.get("XDG_STATE_HOME")
        base = (xdg if xdg and os.path.isabs(xdg)
                else os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "kbd-signal")
