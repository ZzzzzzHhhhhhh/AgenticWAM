"""Installed entry-point plugins, selected by trusted application configuration."""

from importlib.metadata import entry_points

from vela_agent.core.types import ContractError

KINDS = {"backends", "planners", "monitors", "compilers"}


def discover(kind):
    if kind not in KINDS:
        raise ContractError("unknown plugin kind")
    found = {}
    for entry in entry_points(group=f"vela_agent.{kind}"):
        if entry.name in found:
            raise ContractError(f"duplicate {kind} plugin: {entry.name}")
        found[entry.name] = entry
    return found


def load(kind, name, **kwargs):
    entries = discover(kind)
    if name not in entries:
        raise ContractError(f"missing {kind} plugin {name!r}")
    # Only an explicitly selected plugin is loaded. Model output never selects code.
    return entries[name].load()(**kwargs)
