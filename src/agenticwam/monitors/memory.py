"""Bounded episode memory: previous judgments are context, never current proof."""

import json
from collections import deque

from agenticwam.core.types import integer


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def excerpt(value, limit=500):
    return {"text": value[:limit], "truncated": len(value) > limit}


class VerificationMemory:
    def __init__(self, *, entries=3, max_bytes=8192):
        integer(entries, "memory entries", 0, 8)
        integer(max_bytes, "memory bytes", 1024, 32768)
        self.entries, self.max_bytes = entries, max_bytes
        self.reset()

    def reset(self):
        self.context = {}
        self.recent = deque(maxlen=self.entries or 1)
        self.step_id = None

    def set_context(self, plan, completed, remaining):
        self.context = {
            "plan_id": plan.plan_id,
            "mission_excerpt": excerpt(plan.mission or plan.summary),
            "completed_count": len(completed),
            "completed_tail": [
                {"step_id": step.step_id, "instruction_excerpt": excerpt(step.instruction, 160)}
                for step in completed[-4:]
            ],
            "remaining_count": len(remaining),
            "upcoming": [
                {"step_id": step.step_id, "instruction_excerpt": excerpt(step.instruction, 160)}
                for step in remaining[1:3]
            ],
        }

    def begin(self, step):
        # A retry, revised step or new episode must not inherit an old attempt's judgments.
        self.step_id = step.step_id
        self.recent.clear()

    def remember(self, stage, observations, verdicts):
        if not self.entries:
            return
        self.recent.append(
            {
                "stage": stage,
                "observations": [
                    {
                        "server_monotonic_ns": observation["metadata"].get("server_monotonic_ns"),
                        "verdict": verdict["verdict"],
                        "reason_excerpt": excerpt(verdict["reason"]),
                        "evidence_excerpt": excerpt(verdict["evidence"]),
                    }
                    for observation, verdict in zip(observations, verdicts, strict=True)
                ],
            }
        )

    def payload(self):
        if not self.entries:
            return {}
        value = {"current_step": self.step_id, "recent_assessments": [], "omitted_fields": []}
        # Keep the newest assessments in a strict byte budget; retain chronological order.
        for item in reversed(self.recent):
            candidate = {**value, "recent_assessments": [item, *value["recent_assessments"]]}
            if len(encoded(candidate).encode()) <= self.max_bytes - 256:
                value = candidate
        for key, item in self.context.items():
            candidate = {**value, key: item}
            if len(encoded(candidate).encode()) <= self.max_bytes - 256:
                value = candidate
            else:
                value["omitted_fields"].append(key)
        return value
