"""Just enough of pi_hub.plugins.base to import and drive the plugin in tests.

Set PIHUB_CORE to a Pi Hub checkout to test against the real module instead.
"""
from __future__ import annotations


class RouteDef:
    def __init__(self, method, path, handler, caps=None):
        self.method, self.path, self.handler, self.caps = method.upper(), path, handler, caps or []


class TaskDef:
    def __init__(self, name, fn, interval=0, autostart=True):
        self.name, self.fn, self.interval, self.autostart = name, fn, float(interval or 0), autostart


class Contribution:
    def __init__(self, slot, id, provider=None, poll=30, order=100, caps=None,
                 label="", icon_svg="", static=None):
        self.slot, self.id, self.provider, self.poll, self.order = slot, id, provider, poll, order
        self.caps, self.label, self.icon_svg, self.static = caps or [], label, icon_svg, static


class FrameDef:
    def __init__(self, id, entry, css=None, assets=None, surfaces=None, renders=None,
                 reads=None, height=240):
        self.id, self.entry, self.css, self.assets = id, list(entry), list(css or []), list(assets or [])
        self.surfaces, self.renders, self.reads, self.height = list(surfaces or []), list(renders or []), list(reads or []), height


class Plugin:
    name = ""
    version = "0.0.0"
    capabilities: list = []

    def load(self, ctx):
        pass

    def unload(self):
        pass

    def migrate_config(self, old_version, config):
        return config


class PluginContext:
    pass
