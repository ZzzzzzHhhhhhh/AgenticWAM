"""Application-level composition; all concrete imports stay outside the core."""

import json

from agenticwam.backends.vela_signals import completion_settings
from agenticwam.core.types import ContractError, RunSettings, integer


def load_profile(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("capability"), dict):
        raise ContractError("profile must declare a capability")
    capability = value["capability"]
    if capability.get("input_modalities") != ["text"]:
        raise ContractError("the built-in input/compiler adapters support text only")
    skills = capability.setdefault("skills", ["language"])
    if not isinstance(skills, list) or not skills or any(not isinstance(s, str) or not s for s in skills):
        raise ContractError("capability.skills must be a nonempty string array")
    for key, low, high, default in (
        ("max_steps", 1, 64, 64),
        ("step_timeout_sec", 1, 600, 300),
        ("visual_watchdog_chunks", 1, 256, 5),
    ):
        integer(value.setdefault(key, default), key, low, high)
    rules = value.setdefault("verification", {})
    if not isinstance(rules, dict) or set(rules) - {"preconditions", "invariants", "handoff_criteria"}:
        raise ContractError("invalid verification rules")
    for ruleset in rules.values():
        if not isinstance(ruleset, list) or any(not isinstance(rule, str) or not rule.strip() for rule in ruleset):
            raise ContractError("verification rules must be arrays of nonempty strings")
    run_settings(value)
    for option in ("batch_verification", "verification_memory"):
        if option in value and type(value[option]) is not bool:
            raise ContractError(f"{option} must be boolean")
    return value


def run_settings(profile):
    return RunSettings(
        units_per_check=profile.get("chunks_per_check", 1),
        max_units_per_step=profile.get("max_chunks_per_step", 40),
        confirmations=profile.get("confirmations", 2),
        confirmation_interval_ns=profile.get("confirmation_interval_ns", 300_000_000),
        max_replans=profile.get("max_replans", 0),
    )


def make_planner(profile, model):
    from agenticwam import plugins
    from agenticwam.planners.language import LanguagePlanner

    if profile.get("planner"):
        return plugins.load("planners", profile["planner"], model=model, profile=profile)
    return LanguagePlanner(model, profile)


def assemble(profile, model, output, *, endpoint=None, on_event=None):
    from agenticwam import plugins
    from agenticwam.backends.vela import VelaContextBackend
    from agenticwam.contexts.text import TextCompiler
    from agenticwam.core.runner import Runner
    from agenticwam.monitors.verified import RequiredSignal, VerifiedMonitor
    from agenticwam.monitors.vision import VisionVerifier

    backend_name = profile.get("backend", "vela-openwam")
    if backend_name == "vela-openwam":
        if not endpoint:
            raise ContractError("the Vela/OpenWAM adapter requires an endpoint")
        signal = completion_settings(profile.get("completion"))
        backend = VelaContextBackend(
            endpoint, camera_roles=profile.get("camera_roles", ["front_view", "wrist_view"]), completion=signal
        )
        signal_name = None if signal["type"] == "vision" else signal["type"]
    else:
        backend = plugins.load("backends", backend_name, config=profile.get("backend_config", {}))
        signal_name = profile.get("required_signal")
    compiler = TextCompiler(supported_skills=profile["capability"].get("skills", ["language"]))
    if profile.get("compiler"):
        compiler = plugins.load("compilers", profile["compiler"], config=profile.get("compiler_config", {}))
    planner = make_planner(profile, model)
    monitor = VerifiedMonitor(
        VisionVerifier(
            model, profile.get("verification"), memory_entries=3 if profile.get("verification_memory", True) else 0
        ),
        gate=RequiredSignal(signal_name) if signal_name else None,
        watchdog_units=profile.get("visual_watchdog_chunks", 5),
        batch_verification=profile.get("batch_verification", False),
    )
    if profile.get("monitor"):
        monitor = plugins.load("monitors", profile["monitor"], model=model, profile=profile)
    settings = run_settings(profile)
    if settings.max_replans and not hasattr(planner, "revise"):
        raise ContractError("selected planner does not support replanning")
    return Runner(
        backend,
        monitor,
        compiler,
        output,
        settings=settings,
        replanner=planner if settings.max_replans else None,
        on_event=on_event,
    )
