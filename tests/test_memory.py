"""Episode context must stay bounded and cannot promote old beliefs to evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace

from agenticwam.examples.demo import make_demo
from agenticwam.monitors.memory import VerificationMemory, encoded
from agenticwam.monitors.vision import VisionVerifier


def test_next_check_receives_previous_assessment_but_new_task_does_not():
    prompts = []

    def ask(prompt, schema, **kwargs):
        prompts.append(json.loads(prompt.split("\n", 1)[1]))
        assert "fallible" in prompt
        return {"verdict": "continue", "reason": "Object still held", "evidence": "Not yet released"}

    verifier = VisionVerifier(SimpleNamespace(ask=ask))
    plan, _ = make_demo()
    observation = {"images": [], "metadata": {"server_monotonic_ns": 100}}
    verifier.set_context(plan, (), plan.steps)
    verifier.preflight(plan.steps[0], observation)
    verifier.verify(plan.steps[0], observation, observation)
    assert prompts[0]["execution_memory"]["recent_assessments"] == []
    assert (
        prompts[1]["execution_memory"]["recent_assessments"][0]["observations"][0]["evidence_excerpt"]["text"]
        == "Not yet released"
    )
    verifier.set_context(plan, plan.steps[:1], plan.steps[1:])
    verifier.preflight(plan.steps[1], observation)
    assert prompts[-1]["execution_memory"]["recent_assessments"] == []
    assert prompts[-1]["execution_memory"]["completed_count"] == 1
    assert prompts[-1]["execution_memory"]["completed_tail"][0]["step_id"] == plan.steps[0].step_id
    verifier.reset()
    assert verifier.memory.context == {}
    assert verifier.memory.payload()["recent_assessments"] == []


def test_large_history_and_mission_cannot_exceed_memory_budget():
    memory = VerificationMemory(entries=3, max_bytes=1024)
    plan, _ = make_demo()
    plan = replace(plan, mission="长" * 32768)
    memory.set_context(plan, plan.steps[:1], plan.steps[1:])
    memory.begin(plan.steps[1])
    for i in range(100):
        memory.remember(
            "verification",
            [{"metadata": {"server_monotonic_ns": i}}],
            [
                {
                    "verdict": "unknown",
                    "reason": "长" * 10000,
                    "evidence": "文" * 10000,
                }
            ],
        )
        assert len(encoded(memory.payload()).encode()) <= 1024
    assert len(memory.recent) == 3
    assert "mission_excerpt" in memory.payload()["omitted_fields"]


def test_history_remains_chronological_and_disabled_memory_is_empty():
    memory = VerificationMemory(entries=3)
    for i in range(10):
        memory.remember(
            "verification",
            [{"metadata": {"server_monotonic_ns": i}}],
            [
                {
                    "verdict": "continue",
                    "reason": "progress",
                    "evidence": "visible movement",
                }
            ],
        )
    assert [item["observations"][0]["server_monotonic_ns"] for item in memory.payload()["recent_assessments"]] == [
        7,
        8,
        9,
    ]
    assert VerificationMemory(entries=0).payload() == {}


def test_current_unknown_is_not_overridden_by_previous_success():
    values = iter(["succeeded", "unknown"])
    model = SimpleNamespace(ask=lambda *a, **k: {"verdict": next(values), "reason": "Observed", "evidence": "Frame"})
    verifier = VisionVerifier(model)
    plan, _ = make_demo()
    observation = {"images": [], "metadata": {"server_monotonic_ns": 1}}
    assert verifier.verify(plan.steps[0], observation, observation)["verdict"] == "succeeded"
    assert verifier.verify(plan.steps[0], observation, observation)["verdict"] == "unknown"
