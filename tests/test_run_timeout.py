"""L-cli-timeout: _run honors a per-stage timeout and reaps the child.

Real subprocess (a short `sleep`); no GPU. Verifies the deadline kills a wedged
stage and returns non-zero.
"""

import os
import sys
import time

import pipeline


def _noop_log(msg, level="info"):
    pass


def test_run_times_out_and_reaps_child():
    # Sleep far longer than the timeout; _run must kill it.
    start = time.monotonic()
    rc = pipeline._run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        _noop_log, timeout=1.0,
    )
    elapsed = time.monotonic() - start
    assert rc != 0
    assert elapsed < 15, f"timeout did not fire promptly (took {elapsed:.1f}s)"


def test_run_without_timeout_completes_normally():
    rc = pipeline._run(
        [sys.executable, "-c", "print('ok')"],
        _noop_log,
    )
    assert rc == 0


def test_run_returns_child_exit_code():
    rc = pipeline._run(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        _noop_log,
    )
    assert rc == 3
