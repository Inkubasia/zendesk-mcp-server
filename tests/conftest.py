"""Shared pytest fixtures for the test suite."""
import pytest


@pytest.fixture(autouse=True)
def _reset_sse_appstatus():
    """Reset sse_starlette's process-global exit event between tests.

    ``AppStatus.should_exit_event`` is a module-level ``anyio.Event`` bound to
    the first event loop that touches it. Each HTTP test here runs its own
    server under a fresh ``anyio.run`` loop, so a leftover event from a prior
    test's loop makes the next server raise "bound to a different event loop".
    Clearing it to None forces sse_starlette to recreate it on the current loop.
    """
    try:
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit_event = None
    except Exception:
        pass
    yield
