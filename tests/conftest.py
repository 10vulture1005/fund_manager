"""pytest fixtures for the shared-infrastructure unit tests.

- puts the repo's src/ on sys.path so `core` / `delta` import cleanly
- pytest-asyncio is in auto mode (pyproject), so async tests just work
- provides a fake-clock / fake-sleep helper used by the rate limiter tests
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeClock:
    """Controllable monotonic clock: sleep() advances time instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> float:
        self.now += seconds
        self.sleeps.append(seconds)
        return self.now


import pytest


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()
