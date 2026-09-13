"""Configuration for measured events offered by the Vela runtime."""

import math


def completion_settings(value=None):
    value = {"type": "vision"} if value is None else dict(value)
    if value == {"type": "vision"}:
        return value
    defaults = {
        "type": "gripper_release",
        "closed_fraction": 0.4,
        "open_fraction": 0.8,
        "closed_stable_ms": 100,
        "open_stable_ms": 250,
        "max_feedback_age_ms": 500,
    }
    if value.get("type") != "gripper_release" or set(value) - set(defaults):
        raise ValueError("unsupported completion trigger")
    defaults.update(value)
    for key in ("closed_fraction", "open_fraction"):
        if type(defaults[key]) not in (int, float) or not math.isfinite(defaults[key]):
            raise ValueError("gripper thresholds must be finite numbers")
    if not 0 <= defaults["closed_fraction"] < defaults["open_fraction"] <= 1:
        raise ValueError("gripper thresholds require 0 <= closed < open <= 1")
    for key in ("closed_stable_ms", "open_stable_ms", "max_feedback_age_ms"):
        if type(defaults[key]) is not int or not 1 <= defaults[key] <= 2000:
            raise ValueError("gripper signal durations must be integer milliseconds in 1..2000")
    return defaults
