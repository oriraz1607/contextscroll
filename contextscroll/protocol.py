"""Bounded newline-delimited JSON protocol for session context reports."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .classifier import Decision

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 2048
MAX_CONTEXT_AGE_SECONDS = 0.75


@dataclass(frozen=True, slots=True)
class ContextReport:
    decision: Decision
    role: str = ""
    application: str = ""
    name: str = ""
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True, slots=True)
class ActivityReport:
    active: bool


def encode(report: ContextReport) -> bytes:
    payload = {
        "v": PROTOCOL_VERSION,
        "type": "context",
        "decision": report.decision.value,
        "role": report.role[:80],
        "application": report.application[:120],
        "name": report.name[:160],
        "x": report.x,
        "y": report.y,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes) -> ContextReport:
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("context report is too large")
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("report must be an object")
    if payload.get("v") != PROTOCOL_VERSION or payload.get("type") != "context":
        raise ValueError("unsupported protocol message")
    try:
        decision = Decision(payload["decision"])
    except (KeyError, ValueError) as error:
        raise ValueError("invalid decision") from error

    def text(key: str, maximum: int) -> str:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        return "".join(char for char in value if char.isprintable())[:maximum]

    def coordinate(key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or not -1_000_000 <= value <= 1_000_000:
            raise ValueError(f"{key} is out of range")
        return value

    return ContextReport(
        decision=decision,
        role=text("role", 80),
        application=text("application", 120),
        name=text("name", 160),
        x=coordinate("x"),
        y=coordinate("y"),
    )


def decode_activity(line: bytes) -> ActivityReport:
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("activity report is too large")
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error
    if (
        not isinstance(payload, dict)
        or payload.get("v") != PROTOCOL_VERSION
        or payload.get("type") != "activity"
        or not isinstance(payload.get("active"), bool)
    ):
        raise ValueError("invalid activity report")
    return ActivityReport(payload["active"])


class ContextRegistry:
    """Latest report from each authenticated desktop-session helper."""

    def __init__(self, maximum_age: float = MAX_CONTEXT_AGE_SECONDS):
        self.maximum_age = maximum_age
        self._reports: dict[object, tuple[float, ContextReport]] = {}

    def update(
        self, client: object, report: ContextReport, now: float | None = None
    ) -> None:
        self._reports[client] = (
            time.monotonic() if now is None else now,
            report,
        )

    def remove(self, client: object) -> None:
        self._reports.pop(client, None)

    def current(self, now: float | None = None) -> ContextReport:
        current_time = time.monotonic() if now is None else now
        fresh = [
            report
            for received, report in self._reports.values()
            if current_time - received <= self.maximum_age
        ]
        # Multiple active seats cannot be matched reliably to a raw evdev
        # device. Native wins because it is the non-destructive outcome.
        for decision in (Decision.NATIVE, Decision.SCROLL):
            for report in fresh:
                if report.decision is decision:
                    return report
        return ContextReport(Decision.UNKNOWN)
