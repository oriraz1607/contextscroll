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
    request_id: int = 0
    generation: int = 0


@dataclass(frozen=True, slots=True)
class ActivityReport:
    active: bool
    generation: int = 0


@dataclass(frozen=True, slots=True)
class RefreshReport:
    request_id: int


@dataclass(frozen=True, slots=True)
class CursorReport:
    x: int
    y: int


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
        "request_id": report.request_id,
        "generation": report.generation,
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
        request_id=request_id(payload),
        generation=generation(payload),
    )


def request_id(payload: dict) -> int:
    value = payload.get("request_id", 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 2**64 - 1
    ):
        raise ValueError("request_id is out of range")
    return value


def generation(payload: dict) -> int:
    value = payload.get("generation", 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 2**64 - 1
    ):
        raise ValueError("generation is out of range")
    return value


def decode_daemon(
    line: bytes,
) -> ActivityReport | RefreshReport | CursorReport:
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("daemon report is too large")
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(payload, dict) or payload.get("v") != PROTOCOL_VERSION:
        raise ValueError("invalid daemon report")
    if payload.get("type") == "activity" and isinstance(
        payload.get("active"), bool
    ):
        return ActivityReport(payload["active"], generation(payload))
    if payload.get("type") == "refresh":
        value = request_id(payload)
        if value > 0:
            return RefreshReport(value)
    if payload.get("type") == "cursor":
        coordinates = []
        for key in ("x", "y"):
            value = payload.get(key)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not -1_000_000 <= value <= 1_000_000
            ):
                raise ValueError(f"{key} is out of range")
            coordinates.append(value)
        return CursorReport(*coordinates)
    raise ValueError("invalid daemon report")


def decode_activity(line: bytes) -> ActivityReport:
    report = decode_daemon(line)
    if not isinstance(report, ActivityReport):
        raise ValueError("invalid activity report")
    return report


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
