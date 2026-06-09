"""Regression tests for the FMU simulation wall-clock timeout boundary.

WHY THIS SUITE EXISTS
---------------------
``fmpy.simulate_fmu`` loads and executes the attacker-supplied FMU's native
shared library directly in the worker process. Before the timeout boundary was
added, a malicious or pathological model (an infinite loop, a busy wait) could
hang the Cloud Run Job forever with no in-process way to interrupt the native
code. The fix runs the simulation in a killable child process and enforces a
hard wall-clock budget (``FMU_SIMULATION_TIMEOUT_SECONDS``), mirroring the
SHACL backend's killable-subprocess pattern.

These tests pin that security boundary: a simulation that overruns the budget
MUST be terminated and reported as a timeout, never allowed to run to
completion. They run on the ``fork`` start method so the child inherits the
parent's monkeypatched ``simulate_fmu`` (a sleep stand-in for a runaway native
model) — production stays on ``spawn``.
"""

from __future__ import annotations

import sys
import time

import pytest

from validator_backends.fmu import runner


# Skip on platforms without a "fork" start method (e.g. native Windows). The
# production path uses "spawn"; "fork" is purely a test convenience that lets
# the child inherit the parent's monkeypatch instead of needing a real sleeping
# FMU on disk.
_FORK_UNAVAILABLE = sys.platform == "win32"


@pytest.mark.skipif(_FORK_UNAVAILABLE, reason="fork start method unavailable on this platform")
def test_simulation_over_budget_is_terminated_and_reported_as_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulation exceeding the budget is killed and raises a timeout error.

    WHY IT MATTERS: this is the core security guarantee. ``simulate_fmu`` stands
    in for runaway native FMU code by sleeping well past the (shrunk) budget. We
    assert the call raises ``FMUSimulationTimeoutError`` promptly — proving the
    parent terminated the child on the deadline rather than waiting for the long
    sleep to finish. If this regresses, a hostile FMU could hang the job again.
    """
    # Shrink the budget and grace period so the test is fast but still exercises
    # the real terminate/join/kill path. Use "fork" so the child inherits the
    # patched ``simulate_fmu`` below.
    monkeypatch.setattr(runner, "FMU_SIMULATION_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(runner, "_TERMINATE_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(runner, "_MP_START_METHOD", "fork")

    def _hang(*_args: object, **_kwargs: object) -> object:
        """Simulate a runaway FMU: sleep far longer than the budget."""
        time.sleep(30)
        return None  # pragma: no cover — never reached; the child is killed first

    monkeypatch.setattr(runner, "simulate_fmu", _hang)

    started = time.monotonic()
    with pytest.raises(runner.FMUSimulationTimeoutError):
        runner._run_simulation_with_timeout(
            fmu_path=runner.Path("/tmp/does-not-matter.fmu"),
            start_time=0.0,
            stop_time=1.0,
            step_size=None,
            requested_outputs=[],
            start_values={},
        )
    elapsed = time.monotonic() - started

    # The child must be killed near the deadline, not after the 30s sleep. Allow
    # generous slack for spawn/terminate/join overhead while still proving we did
    # not block on the full sleep.
    max_acceptable_seconds = 15.0
    assert elapsed < max_acceptable_seconds, (
        f"timeout path took {elapsed:.1f}s; child was not terminated on the deadline"
    )


@pytest.mark.skipif(_FORK_UNAVAILABLE, reason="fork start method unavailable on this platform")
def test_fast_simulation_returns_result_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulation that finishes within the budget returns its result normally.

    WHY IT MATTERS: the timeout boundary must not break the happy path. This
    guards against a regression where the killable-child plumbing drops the
    result, swallows the fmpy log messages, or always reports a timeout.
    """
    monkeypatch.setattr(runner, "FMU_SIMULATION_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(runner, "_MP_START_METHOD", "fork")

    sentinel = {"y": 1.5}

    def _quick(*_args: object, logger=None, **_kwargs: object) -> object:
        """Return immediately, emitting one log line via fmpy's logger callback."""
        if logger is not None:
            logger("ok")
        return sentinel

    monkeypatch.setattr(runner, "simulate_fmu", _quick)

    result, log_messages = runner._run_simulation_with_timeout(
        fmu_path=runner.Path("/tmp/does-not-matter.fmu"),
        start_time=0.0,
        stop_time=1.0,
        step_size=None,
        requested_outputs=["y"],
        start_values={},
    )

    assert result == sentinel
    assert log_messages == ["ok"]
