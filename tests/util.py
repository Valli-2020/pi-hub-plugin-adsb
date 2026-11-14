"""Shared test helpers: import paths, loading the plugin from a temp copy."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLUGIN_SRC = os.path.join(REPO, "pi_hub_plugins", "adsb")
CORE = os.environ.get("PIHUB_CORE", "")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
if CORE:
    sys.path.insert(0, CORE)          # the real pi_hub package
else:
    sys.path.insert(0, os.path.join(HERE, "stubs"))


def load_package(src: str = PLUGIN_SRC):
    """Import the plugin directory as a package, the way Pi Hub does."""
    name = "adsb_test_" + uuid.uuid4().hex[:8]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(src, "__init__.py"), submodule_search_locations=[src])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def temp_plugin():
    """Copy the plugin into a temp dir (it writes history.json and tile-cache/
    next to its code) and import it from there.  Returns (module, dir)."""
    tmp = tempfile.mkdtemp(prefix="adsb-test-")
    dst = os.path.join(tmp, "adsb")
    shutil.copytree(PLUGIN_SRC, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "history.json*", "tile-cache"))
    return load_package(dst), tmp


class FakeCtx:
    """Stand-in for PluginContext: only the config accessors are used."""

    def __init__(self, config: dict):
        self._config = dict(config)

    def get_config(self) -> dict:
        return self._config

    def save_config(self) -> None:
        pass
