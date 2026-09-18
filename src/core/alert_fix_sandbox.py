"""Offline repository gates in an OS sandbox, with no installation secrets."""

import json
import os
import platform
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from src.core.alert_channels import AlertError
from src.core.alert_fix_files import artifact_directories, directory
from src.core.alert_fix_processes import GateProcesses


class FixSandbox:
    def __init__(self, workspace, dependencies=None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.clean = True
        self.cancelled = threading.Event()
        self.artifacts = artifact_directories(self.workspace)
        self.dependencies = Path(dependencies).resolve(strict=True) if dependencies else None
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise AlertError("No supported OS sandbox for offline fix tests; no PR was opened")

    def verify(self):
        if not self.clean or self.cancelled.is_set():
            raise AlertError("Gate cleanup or cancellation prevents further controller access")
        artifact_directories(self.workspace, self.artifacts)

    def run(self, command, timeout=300):
        self.verify()
        executable = shutil.which(command[0])
        if not executable:
            raise AlertError("Repository gate executable is unavailable")
        scratch = self.workspace / ".alert-test"
        for name in ("home", "tmp"):
            with directory(self.workspace, (".alert-test", name), create=True):
                pass
        readonly = [
            "/System",
            str(Path(sys.base_prefix).resolve()),
            "/usr/bin",
            "/usr/lib",
            "/usr/share",
            "/usr/libexec",
            "/bin",
            "/sbin",
            "/Library/Apple",
            "/opt/homebrew/bin",
            "/opt/homebrew/Cellar",
            "/opt/homebrew/opt",
            "/opt/homebrew/lib",
            "/opt/homebrew/share",
            "/dev/null",
            "/dev/urandom",
            "/dev/random",
            "/dev/fd",
            "/private/etc/localtime",
            "/private/etc/hosts",
            "/private/etc/passwd",
            "/private/etc/group",
            str(Path(executable).resolve()),
        ]
        if self.dependencies:
            readonly.append(str(self.dependencies))
        quote = json.dumps
        profile = "(version 1)(deny default)(allow process-exec process-fork sysctl-read)"
        profile += "(allow signal process-info* (target same-sandbox))"
        profile += '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))'
        profile += '(allow file-read* (literal "/"))'
        profile += '(allow file-read-metadata (literal "/etc") (literal "/var") (literal "/tmp"))'
        profile += "".join(
            "(allow file-read-metadata (path-ancestors " + quote(p) + "))" for p in [*readonly, str(self.workspace)]
        )
        profile += "".join("(allow file-read* (subpath " + quote(p) + "))" for p in readonly)
        profile += "(allow file-read* (subpath " + quote(str(self.workspace)) + "))"
        # Tests cannot rewrite the patch, package scripts or existing tests,
        # even temporarily between the before/after hashes. Only generated
        # artifacts and private tool caches are writable by child processes.
        for name in self.artifacts:
            path = quote(str(self.workspace / name))
            profile += "(allow file-write* (require-all (subpath " + path + ") (require-not (literal " + path + "))))"
        profile += '(allow file-write* (literal "/dev/null"))'
        # Network is denied by default, including loopback and Unix sockets.
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(scratch / "home"),
            "TMPDIR": str(scratch / "tmp"),
            "CI": "true",
            "NO_COLOR": "1",
            "OPENSSL_CONF": "/dev/null",
            "COREPACK_ENABLE_NETWORK": "0",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NUXT_TELEMETRY_DISABLED": "1",
        }

        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (20_000_000, 20_000_000))

        # The trusted launcher cannot run repository code before the controller
        # has verified the inherited sandbox identity. No argv/env marker is used.
        launcher = (
            "import os,subprocess,sys; ready=int(sys.argv[1]); go=int(sys.argv[2]);"
            "os.write(ready,b'R'); os.close(ready); permit=os.read(go,1); os.close(go);"
            "sys.exit(subprocess.run(sys.argv[3:],close_fds=True).returncode if permit==b'G' else 125)"
        )
        control = tempfile.mkdtemp(prefix="gate-", dir=self.workspace.parent)
        try:
            allow, deny = Path(control) / "allow", Path(control) / "deny"
            allow.touch(mode=0o600)
            deny.touch(mode=0o600)
            scoped = profile + "(allow file-read-data (literal " + quote(str(allow)) + "))"
            processes = GateProcesses(allow, deny)
            ready_read, ready_write = os.pipe()
            go_read, go_write = os.pipe()
            self.clean = False
            try:
                with tempfile.TemporaryFile(dir=scratch) as output:
                    process = subprocess.Popen(
                        [
                            "/usr/bin/sandbox-exec",
                            "-p",
                            scoped,
                            str(Path(sys.executable).resolve()),
                            "-I",
                            "-S",
                            "-c",
                            launcher,
                            str(ready_write),
                            str(go_read),
                            executable,
                            *command[1:],
                        ],
                        cwd=self.workspace,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        preexec_fn=limits,
                        pass_fds=(ready_write, go_read),
                    )
                    os.close(ready_write)
                    os.close(go_read)
                    ready_write = go_read = -1
                    try:
                        if not select.select([ready_read], [], [], 10)[0] or os.read(ready_read, 1) != b"R":
                            raise AlertError("Sandbox launcher could not establish its cleanup boundary")
                        if not processes.belongs(process.pid) or processes.belongs(os.getpid()):
                            raise AlertError("Inherited sandbox process identity could not be verified")
                        os.write(go_write, b"G")
                        deadline = time.monotonic() + timeout
                        while process.poll() is None:
                            if self.cancelled.is_set():
                                raise AlertError("Repository gate cancelled")
                            if time.monotonic() >= deadline:
                                raise AlertError("Repository gate exceeded its time budget")
                            time.sleep(0.02)
                        code = process.returncode
                    finally:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                        processes.terminate()
                        artifact_directories(self.workspace, self.artifacts)
                        self.clean = True
                    output.seek(0)
                    text = output.read(24_000).decode(errors="replace")
            finally:
                for fd in (ready_read, ready_write, go_read, go_write):
                    if fd >= 0:
                        os.close(fd)
        finally:
            if self.clean:
                shutil.rmtree(control)
        return {"command": command, "exit_code": code, "output": text}
