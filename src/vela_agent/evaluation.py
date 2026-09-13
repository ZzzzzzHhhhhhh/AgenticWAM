"""Summarize execution journals without claiming a physical success oracle."""

import json
from collections import Counter


def summarize(directory):
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    counts = Counter(row["event"] for row in events)
    return {
        "schema": "vela.agent.metrics/v1",
        "execution_status": result["status"],
        "completed_steps": len(result["completed_steps"]),
        "attempted_steps": counts["step_started"],
        "action_batches": counts["batch_completed"],
        "verification_calls": counts["verification"] + counts["completion_check"],
        "replans": counts["plan_revised"],
        "elapsed_seconds": (events[-1]["wall_ns"] - events[0]["wall_ns"]) / 1e9 if events else 0,
        "physical_success": None,
    }
