from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def install_daytona_stub() -> None:
    """Make orchestrator imports deterministic without the real Daytona SDK."""
    module = types.ModuleType("daytona_sdk")

    class Placeholder:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    module.CreateSandboxFromSnapshotParams = Placeholder
    module.Daytona = Placeholder
    module.DaytonaConfig = Placeholder
    module.SessionExecuteRequest = Placeholder
    module.VolumeMount = Placeholder
    sys.modules["daytona_sdk"] = module


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_orchestrator(name: str = "marsh_orchestrator_test"):
    install_daytona_stub()
    return load_module(name, "orchestrator/orchestrator.py")
