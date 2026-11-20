"""Platform primitives — the single place OS branching lives.

Only the OS-specific *mechanisms* belong here (how to grab/release an
advisory lock, how to detach a child process, where the user data dir is,
which hidapi backend to load). The *policy* around them — the bounded lock
retry + timeout in kbd_signal.states, what to spawn, how config is merged —
stays OS-common in its own module and calls the stable interface below.
Keeping the shared policy single-sourced is what protects the
already-working Windows path from POSIX changes.
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


def hid_module():
    """The hidapi module to talk through.

    On Linux the PyPI `hidapi` wheel builds `hid` on the libusb backend. That
    backend learns an interface's usage page by opening the USB device node,
    which is root-only by default, so for an ordinary user every interface
    enumerates with usage_page 0 and the 0xFF60 filter never matches; opening
    a device through it would also detach the kernel's usbhid driver from that
    interface. The same wheel ships a `hidraw` module with the identical API on
    the kernel hidraw backend, which reads usage pages from sysfs and opens
    /dev/hidraw* directly. Prefer it on Linux, and fall back to `hid` when it
    is absent (e.g. a hidapi built with `hid` itself on hidraw).

    Imported lazily so the hot no-op hook path never loads the native library.
    """
    if sys.platform.startswith("linux"):
        try:
            import hidraw
            return hidraw
        except ImportError:
            pass
    import hid
    return hid



def hid_backend_hint():
    """A sentence to add to a "no raw-HID interface found" message when the
    backend itself may be the cause: on Linux without the `hidraw` module, a
    libusb `hid` cannot see raw-HID interfaces as an ordinary user. Empty
    everywhere else."""
    if sys.platform.startswith("linux") and hid_module().__name__ != "hidraw":
        return ("hidapi's `hidraw` module is not installed; if its `hid` "
                "module is the libusb build, it cannot see raw-HID interfaces "
                "without root.")
    return ""


def hid_open_hint():
    """A sentence to add when opening a found interface fails. On Linux the
    usual cause is /dev/hidraw* being root-only; enumeration reads sysfs and
    succeeds regardless, so this is where a missing udev rule shows up."""
    if sys.platform.startswith("linux"):
        return ("On Linux, /dev/hidraw* is root-only by default; grant access "
                "with a udev rule (see README: Install, Linux).")
    return ""
