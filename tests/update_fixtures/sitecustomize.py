"""Child-process OS/network fakes, enabled only by the survival test's private env."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import urllib.request

if os.environ.get("AGENTCAT_SURVIVAL_SANDBOX"):
    home = Path(os.environ["HOME"])
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def kill_daemon():
        pid_file = home / "fake-daemon.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                if os.name == "posix":
                    os.killpg(pid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def run(args, *a, **kw):
        name = Path(str(args[0])).name.lower()
        if name in ("schtasks.exe", "reg.exe"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if name in ("security", "secret-tool"):
            return subprocess.CompletedProcess(args, 1, "", "")
        if name == "powershell.exe" and "-Command" in args:
            if "Stop-Process" in args[-1]:
                kill_daemon()
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *a, **kw)

    def popen(args, *a, **kw):
        if os.name == "nt" and args[-1] == "daemon" and str(args[0]).endswith("agentcat.cmd"):
            (home / "bootstrapped").write_text("ok")
            return type("FakeProcess", (), {"pid": 0})()
        return real_popen(args, *a, **kw)

    def urlopen(request, *a, **kw):
        url = request.full_url if hasattr(request, "full_url") else request
        # No provider or external request can escape the rehearsal.
        if url == "http://127.0.0.1:8765/healthz":
            if not (home / "bootstrapped").exists():
                raise OSError("fake daemon stopped")
            response = io.BytesIO(b"ok\n")
            response.status = 200
            return response
        if url == "http://127.0.0.1:8765/v1/version":
            return io.BytesIO(json.dumps({"connectorVersion": os.environ["SURVIVAL_VERSION"], "contractVersion": 1}).encode())
        raise OSError("network disabled in update rehearsal")

    subprocess.run = run
    subprocess.Popen = popen
    urllib.request.urlopen = urlopen
