"""Summarize execution journals without claiming a physical success oracle."""

import json
from collections import Counter, defaultdict
from statistics import median


def summarize(directory):
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    counts = Counter(row["event"] for row in events)
    timings = [row for row in events if row["event"] == "phase_timing"]
    phases = defaultdict(list)
    usage = Counter()
    for row in timings:
        phases[row["phase"]].append(row["elapsed_seconds"])
        usage.update((row.get("model_metrics") or {}).get("usage", {}))
    return {
        "schema": "agenticwam.metrics/v1",
        "execution_status": result["status"],
        "completed_steps": len(result["completed_steps"]),
        "attempted_steps": counts["step_started"],
        "action_batches": counts["batch_completed"],
        "verification_calls": counts["verification"] + counts["completion_check"],
        "verification_requests": len(phases["verify"]) + len(phases["verify_window"]) if timings else None,
        "preflight_requests": len(phases["preflight"]) if timings else None,
        "model_requests": sum(row.get("model_metrics") is not None for row in timings) if timings else None,
        "model_usage": dict(usage) if usage else None,
        "latency_seconds": {
            phase: {"count": len(values), "total": sum(values), "median": median(values), "max": max(values)}
            for phase, values in phases.items()
            if values
        },
        "replans": counts["plan_revised"],
        "elapsed_seconds": (events[-1]["wall_ns"] - events[0]["wall_ns"]) / 1e9 if events else 0,
        "physical_success": None,
    }
