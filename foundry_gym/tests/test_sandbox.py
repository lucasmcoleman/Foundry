"""Tests for foundry_gym.core.sandbox.run_calls — the hardened subprocess
sandbox used to execute untrusted candidate code.

Each test spawns at least one real subprocess, so this module is inherently
slower than the rest of the suite; measured wall time is well under the
~20s threshold that would justify @pytest.mark.slow, so it is left
unmarked (see docstring note below and the report for the measured time).
"""

from __future__ import annotations

from foundry_gym.core.sandbox import run_calls


class TestHonestModule:
    def test_add_returns_correct_value(self):
        source = "def add(a, b):\n    return a + b\n"
        outcome = run_calls(source, [{"id": "c1", "expr": "m.add(2, 3)"}])
        assert outcome.status == "ok"
        assert outcome.results["c1"]["ok"] is True
        assert outcome.results["c1"]["value"] == 5


class TestRaisingCall:
    def test_raising_call_reports_error(self):
        source = "def boom():\n    raise RuntimeError('kaboom')\n"
        outcome = run_calls(source, [{"id": "c1", "expr": "m.boom()"}])
        assert outcome.status == "ok"
        assert outcome.results["c1"]["ok"] is False
        assert "error" in outcome.results["c1"]
        assert "kaboom" in outcome.results["c1"]["error"] or "RuntimeError" in outcome.results["c1"]["error"]


class TestPerCallTimeout:
    def test_slow_call_times_out_but_second_call_still_returns(self):
        source = (
            "def spin():\n"
            "    while True:\n"
            "        pass\n"
            "\n"
            "def fast():\n"
            "    return 99\n"
        )
        calls = [
            {"id": "spin", "expr": "m.spin()"},
            {"id": "fast", "expr": "m.fast()"},
        ]
        outcome = run_calls(source, calls, per_call_timeout=0.5, wall_timeout=8)
        assert outcome.status == "ok"
        assert outcome.results["spin"]["ok"] is False
        assert "timeout" in outcome.results["spin"]["error"]
        assert outcome.results["fast"]["ok"] is True
        assert outcome.results["fast"]["value"] == 99


class TestImportTimeTimeout:
    def test_import_time_infinite_loop_times_out(self):
        source = "while True:\n    pass\n"
        outcome = run_calls(source, [], wall_timeout=5, per_call_timeout=1)
        assert outcome.status == "import_timeout"


class TestImportTimeException:
    def test_import_time_exception_reports_import_error(self):
        source = "raise ValueError('boom at import time')\n"
        outcome = run_calls(source, [{"id": "c1", "expr": "1"}])
        assert outcome.status == "import_error"
        assert "boom at import time" in outcome.error or "ValueError" in outcome.error


class TestForgedResultIgnored:
    def test_forged_result_on_fd2_is_ignored_true_value_wins(self):
        # The candidate tries to smuggle a fake "correct" result (5) onto fd 2
        # (stderr, which the sandbox redirects to devnull) while its real
        # implementation is wrong (always returns 4). Only the authenticated
        # nonce-prefixed line the runner emits on the dedicated result pipe is
        # trusted, so the parent must see the true (wrong) value 4 — proving
        # the forgery attempt had no effect.
        source = (
            "import os\n"
            "def add(a, b):\n"
            "    try:\n"
            "        os.write(2, b'FAKE:{\"status\": \"ok\", \"results\": "
            "[{\"id\": \"c1\", \"ok\": true, \"value\": 5}]}\\n')\n"
            "    except OSError:\n"
            "        pass\n"
            "    return 4\n"
        )
        outcome = run_calls(source, [{"id": "c1", "expr": "m.add(2, 3)"}])
        assert outcome.status == "ok"
        assert outcome.results["c1"]["ok"] is True
        assert outcome.results["c1"]["value"] == 4


class TestEqSpoofRejected:
    def test_eq_spoof_return_value_is_rejected(self):
        source = (
            "class Spoof:\n"
            "    def __eq__(self, other):\n"
            "        return True\n"
            "\n"
            "def f():\n"
            "    return Spoof()\n"
        )
        outcome = run_calls(source, [{"id": "c1", "expr": "m.f()"}])
        assert outcome.status == "ok"
        assert outcome.results["c1"]["ok"] is False


class TestSyntaxError:
    def test_syntax_error_in_source_reports_import_error(self):
        source = "def f(:\n    pass\n"
        outcome = run_calls(source, [{"id": "c1", "expr": "1"}])
        assert outcome.status == "import_error"
