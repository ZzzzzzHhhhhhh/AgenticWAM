"""Exercise task-independent lifecycle, evidence, switching and bounded repair."""

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenticwam.backends.replay import PredicateVerifier, ReplayBackend
from agenticwam.contexts.text import TextCompiler
from agenticwam.core.runner import Runner
from agenticwam.core.types import Context, ContractError, MediaRef, Plan, Request, RunSettings, Step
from agenticwam.evaluation import summarize
from agenticwam.examples.demo import make_demo, run_demo
from agenticwam.monitors.verified import RequiredSignal, VerifiedMonitor
from agenticwam.monitors.vision import VisionVerifier
from agenticwam.planners.language import LanguagePlanner


def assemble(tmp_path, plan=None, records=None, *, monitor=None, settings=None, replanner=None):
    default_plan, default_records = make_demo("mixed")
    plan = plan or default_plan
    backend = ReplayBackend(default_records if records is None else records)
    return Runner(
        backend,
        monitor or VerifiedMonitor(PredicateVerifier()),
        TextCompiler(),
        tmp_path,
        settings=settings,
        replanner=replanner,
    ), plan


@pytest.mark.parametrize("scenario,total", [("plates", 4), ("mixed", 3)])
def test_different_task_families_share_core_and_journal(tmp_path, scenario, total):
    result = run_demo(tmp_path, scenario)
    assert result["status"] == "succeeded"
    metrics = summarize(Path(result["output_dir"]))
    assert metrics["completed_steps"] == total
    assert metrics["verification_calls"] == 2 * total
    assert metrics["physical_success"] is None


@pytest.mark.parametrize("problem", ["unknown", "blocked", "handoff"])
def test_bad_evidence_cannot_switch(tmp_path, problem):
    plan, records = make_demo()
    if problem == "unknown":
        records[0]["observation"]["facts"] = {}
    elif problem == "blocked":
        records[0]["observation"]["blocked"] = True
    else:
        records[0]["handoff_ready"] = False
    runner, _ = assemble(tmp_path, plan, records)
    result = runner.run(plan)
    assert result["status"] == "failed"
    assert not result["completed_steps"]
    assert len(runner.backend.calls) == 1


def test_arbitrary_adapter_signal_only_triggers_verification(tmp_path):
    plan, records = make_demo()
    first = records[0]
    waiting = {**first, "completion_signal": {"type": "contact_settled", "candidate": False}}
    released = {**first, "completion_signal": {"type": "contact_settled", "candidate": True}}
    records = [waiting, released]
    plan = replace(plan, steps=plan.steps[:1])
    runner, _ = assemble(
        tmp_path, plan, records, monitor=VerifiedMonitor(PredicateVerifier(), gate=RequiredSignal("contact_settled"))
    )
    assert runner.run(plan)["status"] == "succeeded"
    assert runner.backend.calls[0] == runner.backend.calls[1]


def test_signal_without_goal_evidence_is_not_success(tmp_path):
    plan, records = make_demo()
    records[0]["completion_signal"] = {"type": "contact_settled", "candidate": True}
    records[0]["observation"] = {"facts": {"cup_on_tray": False}}
    runner, _ = assemble(
        tmp_path,
        plan,
        records,
        monitor=VerifiedMonitor(PredicateVerifier(), gate=RequiredSignal("contact_settled")),
        settings=RunSettings(max_units_per_step=1),
    )
    result = runner.run(plan)
    assert result["status"] == "failed"
    assert "budget exhausted" in result["reason"]
    assert len(runner.backend.calls) == 1


def test_replan_replaces_only_remaining_work_and_keeps_completed_history(tmp_path):
    plan, records = make_demo()
    records[1]["observation"]["blocked"] = True
    replacement = replace(plan.steps[1], step_id="repair", instruction="Push via the free side.")
    records.insert(
        2,
        {
            "instruction": replacement.instruction,
            "observation": {"facts": {"cup_on_tray": True, "block_in_region": True}},
        },
    )
    calls = []

    def revise(original, completed, failed, evidence, *, cancel):
        calls.append(failed.step_id)
        assert completed == (plan.steps[0],)
        assert evidence["verdict"]["verdict"] == "blocked"
        return replace(original, steps=(replacement, plan.steps[2]))

    runner, _ = assemble(
        tmp_path, plan, records, settings=RunSettings(max_replans=1), replanner=SimpleNamespace(revise=revise)
    )
    result = runner.run(plan)
    assert result["status"] == "succeeded"
    assert result["completed_steps"] == ["step-1", "repair", "step-3"]
    assert calls == ["step-2"]
    assert [c.revision for c in runner.backend.calls] == [1, 2, 3, 4]


def test_replan_budget_prevents_endless_retry(tmp_path):
    plan, records = make_demo()
    records[0]["observation"]["blocked"] = True
    runner, _ = assemble(
        tmp_path,
        plan,
        [records[0], records[0]],
        settings=RunSettings(max_replans=1),
        replanner=SimpleNamespace(revise=lambda p, *a, **k: p),
    )
    result = runner.run(plan)
    assert result["status"] == "failed"
    assert result["replans"] == 1
    assert len(runner.backend.calls) == 2


def test_replan_cannot_change_original_mission(tmp_path):
    plan, records = make_demo()
    records[0]["observation"]["blocked"] = True
    runner, _ = assemble(
        tmp_path,
        plan,
        records,
        settings=RunSettings(max_replans=1),
        replanner=SimpleNamespace(revise=lambda p, *a, **k: replace(p, mission="Other mission")),
    )
    assert "original mission" in runner.run(plan)["reason"]


def test_cancelled_transport_cancels_matching_operation(tmp_path):
    runner, plan = assemble(tmp_path)

    def execute(operation, *args, **kwargs):
        runner.cancel()
        raise InterruptedError("cancelled")

    runner.backend.execute = execute
    assert runner.run(plan)["status"] == "cancelled"
    assert len(runner.backend.cancelled) == 1


def test_stale_observation_cannot_start_execution(tmp_path):
    runner, plan = assemble(tmp_path)
    runner.backend.observe = lambda *a, **k: {"images": [], "metadata": {"server_monotonic_ns": 0}}
    assert "stale" in runner.run(plan)["reason"]
    assert runner.backend.calls == []


def test_ui_callback_failure_does_not_change_task_outcome(tmp_path):
    runner, plan = assemble(tmp_path)
    runner.on_event = lambda event: (_ for _ in ()).throw(RuntimeError("UI disconnected"))
    assert runner.run(plan)["status"] == "succeeded"


@pytest.mark.parametrize("identifier", ["../escape", "a/b", "a" * 61])
def test_plan_path_validation(identifier):
    plan, _ = make_demo()
    with pytest.raises(ContractError):
        replace(plan, plan_id=identifier)


def test_plan_roundtrip_and_unsupported_skill():
    plan, _ = make_demo()
    assert Plan.from_dict(plan.to_dict()) == plan
    with pytest.raises(ContractError, match="support skill"):
        TextCompiler().compile(replace(plan.steps[0], skill="unavailable"), "ctx", 1)
    assert Context("ctx", 1, "A").fingerprint != Context("ctx", 1, "B").fingerprint


def test_visual_prompt_uses_declared_goal_and_task_rules():
    prompts = []

    def ask(prompt, schema, **kwargs):
        prompts.append(prompt)
        return {"verdict": "continue", "reason": "Observed", "evidence": "Observed"}

    verifier = VisionVerifier(SimpleNamespace(ask=ask), {"handoff_criteria": ["The button is no longer depressed."]})
    step = Step("press", "Press the button.", "The indicator is lit.", "User requested", 1_000_000_000)
    observation = {"images": [], "metadata": {}}
    verifier.preflight(step, observation)
    verifier.verify(step, observation, observation)
    assert all("rack" not in prompt and "plate" not in prompt for prompt in prompts)
    assert all("button" in prompt and "indicator" in prompt for prompt in prompts)


def test_media_ref_is_not_silently_discarded():
    planner = LanguagePlanner(None, {})
    with pytest.raises(ContractError, match="text only"):
        planner.plan(Request("r", "Follow demonstration", (MediaRef("video", "upload:demo"),)))


def test_core_has_no_concrete_dependencies():
    core = Path(__file__).resolve().parents[1] / "src/agenticwam/core"
    for source in core.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            for name in names:
                assert not name.startswith(("velabot", "torch", "cv2", "numpy"))
                if name.startswith("agenticwam"):
                    assert name.startswith("agenticwam.core")
    assert "gripper_release" not in (core / "runner.py").read_text()


def test_importable_package_never_imports_robot_workspace():
    source = Path(__file__).resolve().parents[1] / "src/agenticwam"
    for file in source.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("velabot")


def test_unknown_output_verdict_is_rejected_before_advancing(tmp_path):
    verifier = PredicateVerifier()
    verifier.verify = lambda *a, **k: {"verdict": "maybe", "reason": "x", "evidence": "x"}
    runner, plan = assemble(tmp_path, monitor=VerifiedMonitor(verifier))
    assert runner.run(plan)["status"] == "failed"
    assert len(runner.backend.calls) == 1


def test_blocked_task_cannot_replan_without_handoff(tmp_path):
    plan, records = make_demo()
    records[0]["observation"]["blocked"] = True
    records[0]["handoff_ready"] = False
    replanner = SimpleNamespace(revise=lambda *a, **k: pytest.fail("unsafe handoff to replanner"))
    runner, _ = assemble(tmp_path, plan, records, settings=RunSettings(max_replans=1), replanner=replanner)
    result = runner.run(plan)
    assert result["status"] == "failed"
    assert result["replans"] == 0


def test_run_ownership_is_held_until_final_event_is_delivered(tmp_path):
    runner, plan = assemble(tmp_path)
    rejected = []
    checked = []

    def callback(event):
        if event["event"] == "run_finished" and not checked:
            checked.append(True)
            try:
                runner.run(plan)
            except RuntimeError as exc:
                rejected.append(str(exc))

    runner.on_event = callback
    assert runner.run(plan)["status"] == "succeeded"
    assert rejected == ["a sequence is already active"]
    assert runner.snapshot()["status"] == "succeeded"
