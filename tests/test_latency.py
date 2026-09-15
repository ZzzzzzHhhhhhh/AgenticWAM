"""Latency changes must retain fresh evidence, cancellation and switching gates."""

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenticwam.backends.replay import ReplayBackend
from agenticwam.backends.vela import VelaContextBackend
from agenticwam.contexts.text import TextCompiler
from agenticwam.core.runner import Runner
from agenticwam.core.types import ContractError, RunSettings
from agenticwam.evaluation import summarize
from agenticwam.examples.demo import make_demo
from agenticwam.monitors.verified import RequiredSignal, VerifiedMonitor
from agenticwam.monitors.vision import VisionVerifier
from agenticwam.planners.codex import CodexJson


def verdict(status):
    return {"verdict": status, "reason": "fixture reason", "evidence": "fixture evidence"}


class FixtureModel:
    def __init__(self, statuses=("succeeded", "succeeded")):
        self.statuses = statuses
        self.calls = []

    def ask(self, prompt, schema, **kwargs):
        self.calls.append((prompt, schema, kwargs))
        if "after_0" in schema["properties"]:
            return {key: verdict(status) for key, status in zip(schema["properties"], self.statuses, strict=True)}
        return verdict("continue" if "Never use succeeded at preflight" in prompt else "succeeded")


def window_run(tmp_path, *, enabled=True, statuses=("succeeded", "succeeded"), handoff=True):
    plan, records = make_demo("plates")
    plan = replace(plan, steps=plan.steps[:2])
    for record in records:
        record["completion_signal"] = {"type": "settled", "candidate": True}
        record["handoff_ready"] = handoff
    model = FixtureModel(statuses)
    runner = Runner(
        ReplayBackend(records),
        VerifiedMonitor(VisionVerifier(model), gate=RequiredSignal("settled"), batch_verification=enabled),
        TextCompiler(),
        tmp_path,
        settings=RunSettings(max_units_per_step=1),
    )
    return runner, plan, model


def test_batched_checks_reduce_calls_but_keep_every_preflight_and_confirmation(tmp_path):
    legacy, plan, legacy_model = window_run(tmp_path / "legacy", enabled=False)
    batch, _, model = window_run(tmp_path / "batch")
    assert legacy.run(plan)["status"] == "succeeded"
    result = batch.run(plan)
    assert result["status"] == "succeeded"
    assert len(legacy_model.calls) == 6
    assert len(model.calls) == 4
    metrics = summarize(Path(result["output_dir"]))
    assert metrics["verification_calls"] == 4  # Evidence decisions, not network requests.
    assert metrics["verification_requests"] == 2
    assert metrics["preflight_requests"] == 2
    assert metrics["latency_seconds"]["verify_window"]["count"] == 2
    for prompt, schema, _ in model.calls:
        if "after_0" not in schema["properties"]:
            continue
        evidence = json.loads(prompt.split("\n", 1)[1])
        assert evidence["after_0"]["server_monotonic_ns"] < evidence["after_1"]["server_monotonic_ns"]
        assert (
            evidence["after_1"]["server_monotonic_ns"] - evidence["after_0"]["server_monotonic_ns"]
            > batch.settings.confirmation_interval_ns
        )


@pytest.mark.parametrize(
    "statuses",
    [
        ("succeeded", "unknown"),
        ("succeeded", "continue"),
        ("continue", "blocked"),
        ("unknown", "succeeded"),
        ("continue", "unknown"),
        ("continue", "succeeded"),
    ],
)
def test_no_switch_when_either_window_frame_fails(statuses, tmp_path):
    runner, plan, _ = window_run(tmp_path, statuses=statuses)
    result = runner.run(plan)
    assert result["status"] == "failed"
    assert result["completed_steps"] == []
    assert len(runner.backend.calls) == 1


def test_batching_cannot_authorize_unfinished_handoff(tmp_path):
    runner, plan, model = window_run(tmp_path, handoff=False)
    assert runner.run(plan)["status"] == "failed"
    assert all("after_0" not in schema["properties"] for _, schema, _ in model.calls)
    assert len(runner.backend.calls) == 1


def test_cancel_during_window_never_switches(tmp_path):
    runner, plan, model = window_run(tmp_path)
    ask = model.ask

    def cancel_after_response(prompt, schema, **kwargs):
        response = ask(prompt, schema, **kwargs)
        if "after_0" in schema["properties"]:
            runner.cancel()
        return response

    model.ask = cancel_after_response
    assert runner.run(plan)["status"] == "cancelled"
    assert len(runner.backend.calls) == 1


def test_stale_second_window_frame_never_reaches_model(tmp_path):
    runner, plan, model = window_run(tmp_path)
    observe = runner.backend.observe
    calls = 0

    def stale(directory, **kwargs):
        nonlocal calls
        calls += 1
        observation = observe(directory, **kwargs)
        if calls == 3:
            observation["metadata"]["server_monotonic_ns"] = kwargs["after_ns"]
        return observation

    runner.backend.observe = stale
    assert "stale" in runner.run(plan)["reason"]
    assert len(model.calls) == 1


def test_watchdog_without_signal_does_not_collect_completion_window():
    monitor = VerifiedMonitor(
        VisionVerifier(FixtureModel()),
        gate=RequiredSignal("settled"),
        watchdog_units=5,
        batch_verification=True,
    )
    result = {"handoff_ready": True, "completion_signal": {"type": "settled", "candidate": False}}
    assert not monitor.should_check(result, 1)
    assert monitor.should_check(result, 5)
    assert monitor.window_size(result, 2) == 1


def test_window_missing_verdict_rejected_and_images_ordered_once():
    def ask(prompt, schema, **kwargs):
        assert kwargs["images"] == ["before-front", "before-wrist", "a-front", "a-wrist", "b-front", "b-wrist"]
        assert "hardware fault" in prompt
        return {"after_0": verdict("succeeded")}

    verifier = VisionVerifier(SimpleNamespace(ask=ask))
    plan, _ = make_demo()
    observations = [
        {"images": [f"{name}-front", f"{name}-wrist"], "metadata": {"hardware": "hardware fault"}}
        for name in ("before", "a", "b")
    ]
    with pytest.raises(ContractError, match="every observation"):
        verifier.verify_many(plan.steps[0], observations[0], observations[1:])


def camera_backend():
    backend = VelaContextBackend("http://localhost:1")
    backend.cancel_epoch = 7
    backend.snapshot = lambda: {
        "agent": {"cancel_epoch": 7},
        "updated_monotonic_ns": 100,
        "cameras": {"streams": [{"role": role, "monotonic_ns": 99} for role in backend.camera_roles]},
        "hardware": {"alerts": ["preserve evidence"]},
    }
    return backend


def test_camera_downloads_are_parallel_and_order_preserving(tmp_path):
    backend = camera_backend()
    both_started = threading.Barrier(2)

    def request(path, **kwargs):
        both_started.wait(timeout=2)  # A sequential implementation cannot pass.
        return path.encode()

    backend._request = request
    observation = backend.observe(tmp_path)
    assert [Path(p).read_bytes().decode().split("/")[-1] for p in observation["images"]] == list(backend.camera_roles)
    assert observation["metadata"]["hardware"] == {"alerts": ["preserve evidence"]}
    assert observation["diagnostics"]["image_bytes"] == sum(Path(p).stat().st_size for p in observation["images"])
    assert "diagnostics" not in observation["metadata"]


def test_failed_camera_download_produces_no_partial_observation(tmp_path):
    backend = camera_backend()

    def request(path, **kwargs):
        if "wrist" in path:
            raise TimeoutError("camera unavailable")
        return b"jpeg"

    backend._request = request
    with pytest.raises(TimeoutError):
        backend.observe(tmp_path)
    assert not list(tmp_path.iterdir())


def test_codex_collects_bounded_numeric_usage_and_response(tmp_path, monkeypatch):
    import agenticwam.planners.codex as module

    real_popen = module.subprocess.Popen
    fixture = tmp_path / "fake_codex.py"
    fixture.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert '--json' in args\n"
        "assert '--sandbox' in args and args[args.index('--sandbox')+1] == 'read-only'\n"
        "assert 'shell_tool' in args\n"
        "custom = next((a.split('=',1)[1] for a in args if a.startswith('model_instructions_file=')), None)\n"
        "if custom: assert 'Preserve every task constraint' in pathlib.Path(json.loads(custom)).read_text()\n"
        "sys.stdin.read()\n"
        "pathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({'ok': True}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 80, 'cached_input_tokens': 20, "
        "'output_tokens': 10, 'reasoning_output_tokens': 5, 'secret': 'never persist'}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda command, **kw: real_popen(
            [sys.executable, str(fixture), *command[1:]],
            **kw,
        ),
    )
    model = CodexJson(compact_instructions=True)
    assert model.ask("test", {"properties": {"ok": {"type": "boolean"}}}) == {"ok": True}
    metrics = model.last_call_metrics
    assert metrics["status"] == "succeeded"
    assert metrics["usage"] == {
        "input_tokens": 80,
        "cached_input_tokens": 20,
        "output_tokens": 10,
        "reasoning_output_tokens": 5,
    }
    assert metrics["elapsed_seconds"] > 0
    assert "secret" not in json.dumps(metrics)
    assert CodexJson(compact_instructions=False).ask("test", {"properties": {"ok": {"type": "boolean"}}}) == {
        "ok": True
    }
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        model.ask("test", {"properties": {}}, cancel=cancelled)
    assert model.last_call_metrics["status"] == "failed"
    assert "usage" not in model.last_call_metrics  # Do not carry usage across failed requests.


def test_legacy_journals_do_not_invent_latency_measurements(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"status": "succeeded", "completed_steps": []}))
    (tmp_path / "events.jsonl").write_text(json.dumps({"event": "verification", "wall_ns": 1}) + "\n")
    metrics = summarize(tmp_path)
    assert metrics["verification_calls"] == 1
    assert metrics["verification_requests"] is None
    assert metrics["latency_seconds"] == {}
