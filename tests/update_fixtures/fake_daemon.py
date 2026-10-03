"""Start the actual detached updater, then remain alive until the installer stops us."""
import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import time

home = Path(os.environ["HOME"])
entry = home / ".agentcat/connectors/bin/agentcat"
loader = SourceFileLoader("survival_agentcat", str(entry))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
(home / "fake-daemon.pid").write_text(str(os.getpid()))
proc = module.start_auto_update_install(os.environ["SURVIVAL_VERSION"])
(home / "installer.pid").write_text(str(proc.pid))
while True:
    time.sleep(0.1)
