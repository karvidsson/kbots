"""Find inherited gate sandboxes even after double fork, setsid and reparenting."""

import ctypes
import errno
import os
import signal
import time

from src.core.alert_channels import AlertError


class GateProcesses:
    def __init__(self, allowed, denied):
        self.allowed, self.denied = os.fsencode(allowed), os.fsencode(denied)
        self.sandbox = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib", use_errno=True)
        self.sandbox.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self.sandbox.sandbox_check.restype = ctypes.c_int
        self.flags = 1 | ctypes.c_int.in_dll(self.sandbox, "SANDBOX_CHECK_NO_REPORT").value
        self.proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.proc.proc_listpids.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
        self.proc.proc_listpids.restype = ctypes.c_int

    def belongs(self, pid):
        def check(operation, *args):
            ctypes.set_errno(0)
            value = self.sandbox.sandbox_check(pid, operation, self.flags if operation else 0, *args)
            if value in {0, 1}:
                return value
            if ctypes.get_errno() == errno.ESRCH:
                return None  # It exited during this census.
            raise AlertError("Gate process identity could not be inspected")

        # An unsandboxed process also allows the positive probe, hence both a
        # sandbox check and the negative control are mandatory. These are fresh
        # controller-owned paths outside every gate-writable/readable subtree.
        return (
            check(None) == 1
            and check(b"file-read-data", self.allowed) == 0
            and check(b"file-read-data", self.denied) == 1
        )

    def members(self):
        # PROC_UID_ONLY = 4 in Darwin sys/proc_info.h. A gate cannot change uid.
        size = self.proc.proc_listpids(4, os.getuid(), None, 0)
        if size <= 0:
            raise AlertError("Gate process enumeration is unavailable")
        for _ in range(4):
            array = (ctypes.c_int * (size // 4 + 1024))()
            length = self.proc.proc_listpids(4, os.getuid(), array, ctypes.sizeof(array))
            if length <= 0:
                raise AlertError("Gate process enumeration failed")
            if length < ctypes.sizeof(array):
                return {pid for pid in array[: length // 4] if pid > 1 and self.belongs(pid)}
            size *= 2
        raise AlertError("Gate process enumeration exceeded its bound")

    def terminate(self):
        deadline = time.monotonic() + 5
        stopped = set()
        # Freeze parents and detached children before killing. Rescan after
        # freezing, so a child forked during enumeration cannot escape the sweep.
        while time.monotonic() < deadline:
            members = self.members()
            if not members:
                return
            for pid in members:
                if self.belongs(pid):
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
            if members <= stopped:
                for pid in members:
                    if self.belongs(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            stopped |= members
            time.sleep(0.01)
        raise AlertError("Gate process cleanup could not be verified; workspace is quarantined")
