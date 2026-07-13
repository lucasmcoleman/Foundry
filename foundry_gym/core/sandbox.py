"""Hardened subprocess sandbox for executing untrusted candidate Python code.

Threat model: a policy model reward-hacking the verifier (not weaponized
malware). The decisive design choice: **expected outputs never enter the child
process.** The child imports the candidate and reports the *actual* value of
each hidden test call; the parent compares against expected values the child
never saw. In-process attacks (stack inspection via sys._getframe,
gc.get_objects(), monkeypatching the runner) can therefore reveal only test
*inputs* — knowing inputs without outputs is just... solving the task.

Additional layers:
- payload (nonce + candidate source + test calls) is written into a pipe and
  fully drained by the runner before the candidate executes: nothing sensitive
  on disk, in argv, or in the environment.
- result line must carry the nonce prefix; anything else on the result fd is
  ignored (candidate writing garbage or premature "results" earns nothing).
- rlimits: CPU, address space, file size, open files, NPROC=0 (fork fails).
- socket module stubbed before candidate import (best effort; ctypes bypass is
  accepted residual risk and documented in docs/verifier-audits.md).
- parent-side wall-clock deadline kills the whole process group.
- child runs `python -S -s -B` with a scrubbed env and PYTHONHASHSEED=0.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Maximum bytes accepted from the child's result pipe before we kill it.
_MAX_RESULT_BYTES = 2_000_000

_RUNNER_SOURCE = r'''
import json, os, resource, signal, sys, types

def main():
    payload_fd = int(sys.argv[1])
    result_fd = int(sys.argv[2])

    # Drain the entire payload BEFORE the candidate can run: once consumed,
    # the pipe is empty and the payload is unrecoverable by candidate code.
    chunks = []
    with os.fdopen(payload_fd, "rb") as f:
        chunks.append(f.read())
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    nonce = payload["nonce"]
    source = payload["source"]
    calls = payload["calls"]
    per_call_timeout = float(payload.get("per_call_timeout", 2.0))
    limits = payload.get("limits", {})

    out = os.fdopen(result_fd, "w")

    def emit(obj):
        out.write(nonce + ":" + json.dumps(obj) + "\n")
        out.flush()

    # ---- resource limits (set before candidate code runs) ----
    try:
        cpu = int(limits.get("cpu_seconds", 5))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        mem = int(limits.get("memory_bytes", 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))  # no fork
        except (ValueError, OSError):
            pass
    except (ValueError, OSError):
        pass

    # ---- best-effort network block ----
    try:
        import socket
        def _blocked(*a, **k):
            raise OSError("network disabled in sandbox")
        socket.socket = _blocked
        socket.create_connection = _blocked
        socket.getaddrinfo = _blocked
    except Exception:
        pass

    # ---- canonicalization (exact builtin types only; mirrors checkers.canonical) ----
    def canonical(value, depth=0):
        if depth > 32:
            raise ValueError("too deep")
        t = type(value)
        if t is bool or t is str or value is None:
            return value
        if t is int:
            if abs(value) > 10**60:
                raise ValueError("int too large")
            return value
        if t is float:
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError("non-finite")
            return value
        if t is list or t is tuple:
            if len(value) > 100000:
                raise ValueError("too long")
            return [canonical(v, depth + 1) for v in value]
        if t is dict:
            out = {}
            for k, v in value.items():
                if type(k) is not str:
                    raise ValueError("non-str key")
                out[k] = canonical(v, depth + 1)
            return out
        raise ValueError("unsupported type: %s" % t.__name__)

    # ---- import candidate in a fresh namespace ----
    # stdout/stderr of candidate go to devnull: the result channel is the fd.
    devnull = open(os.devnull, "w")
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = devnull
    sys.stderr = devnull

    class _Alarm(Exception):
        pass

    def _on_alarm(signum, frame):
        raise _Alarm()

    signal.signal(signal.SIGALRM, _on_alarm)

    ns = {"__name__": "candidate", "__builtins__": __builtins__}
    try:
        signal.setitimer(signal.ITIMER_REAL, per_call_timeout * 2)
        code = compile(source, "<candidate>", "exec")
        exec(code, ns)
        signal.setitimer(signal.ITIMER_REAL, 0)
    except _Alarm:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        emit({"status": "import_timeout", "results": []})
        return
    except BaseException as e:
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout, sys.stderr = real_stdout, real_stderr
        emit({"status": "import_error", "error": "%s: %s" % (type(e).__name__, e),
              "results": []})
        return

    m = types.SimpleNamespace(**{k: v for k, v in ns.items() if not k.startswith("__")})

    results = []
    for c in calls:
        rec = {"id": c["id"]}
        try:
            signal.setitimer(signal.ITIMER_REAL, per_call_timeout)
            value = eval(c["expr"], {"m": m, "__builtins__": __builtins__})
            signal.setitimer(signal.ITIMER_REAL, 0)
            rec["ok"] = True
            cv = canonical(value)
            enc = json.dumps(cv)
            if len(enc) > 200000:
                rec = {"id": c["id"], "ok": False, "error": "result too large"}
            else:
                rec["value"] = cv
        except _Alarm:
            rec["ok"] = False
            rec["error"] = "timeout"
        except BaseException as e:
            signal.setitimer(signal.ITIMER_REAL, 0)
            rec["ok"] = False
            rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        results.append(rec)

    sys.stdout, sys.stderr = real_stdout, real_stderr
    emit({"status": "ok", "results": results})

main()
'''


@dataclass
class SandboxOutcome:
    """Outcome of a sandboxed run.

    status: "ok" | "import_error" | "import_timeout" | "wall_timeout" |
            "no_result" | "sandbox_error"
    results: call id -> {"ok": bool, "value": Any} or {"ok": False, "error": str}
    """

    status: str
    results: Dict[str, dict] = field(default_factory=dict)
    error: str = ""


def run_calls(
    source: str,
    calls: List[dict],
    *,
    wall_timeout: float = 10.0,
    cpu_seconds: int = 5,
    memory_mb: int = 512,
    per_call_timeout: float = 2.0,
) -> SandboxOutcome:
    """Execute ``source`` in a hardened subprocess and evaluate ``calls``.

    Each call is {"id": str, "expr": str}; expressions reference the candidate
    module namespace as ``m`` (e.g. ``m.merge_intervals([[1,3],[2,4]])``).
    Returns actual (canonicalized) values only — the caller compares against
    expected values, which never enter the child process.
    """
    nonce = secrets.token_hex(16)
    payload = json.dumps({
        "nonce": nonce,
        "source": source,
        "calls": calls,
        "per_call_timeout": per_call_timeout,
        "limits": {"cpu_seconds": cpu_seconds, "memory_bytes": memory_mb * 1024 * 1024},
    }).encode("utf-8")

    payload_r, payload_w = os.pipe()
    result_r, result_w = os.pipe()
    # Empty throwaway cwd: candidate file writes (within RLIMIT_FSIZE) land in
    # a scratch dir, never in the caller's working tree (audit finding SBX-1).
    scratch = tempfile.mkdtemp(prefix="gym_sbx_")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-S", "-s", "-B", "-c", _RUNNER_SOURCE,
             str(payload_r), str(result_w)],
            pass_fds=(payload_r, result_w),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=scratch,
            env={"PYTHONHASHSEED": "0", "PATH": os.environ.get("PATH", "/usr/bin")},
            start_new_session=True,
        )
    except OSError as e:
        for fd in (payload_r, payload_w, result_r, result_w):
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)
        return SandboxOutcome(status="sandbox_error", error=f"spawn failed: {e}")

    try:
        # Parent closes the child's ends.
        os.close(payload_r)
        os.close(result_w)

        # Feed the payload from a thread (payload may exceed the pipe buffer).
        def _feed():
            try:
                os.write(payload_w, payload)
            except OSError:
                pass
            finally:
                try:
                    os.close(payload_w)
                except OSError:
                    pass

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()

        # Collect result bytes with a cap.
        buf = bytearray()
        overflow = False

        def _collect():
            nonlocal overflow
            try:
                while True:
                    chunk = os.read(result_r, 65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > _MAX_RESULT_BYTES:
                        overflow = True
                        break
            except OSError:
                pass

        collector = threading.Thread(target=_collect, daemon=True)
        collector.start()

        timed_out = False
        try:
            proc.wait(timeout=wall_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        if overflow:
            _kill_group(proc)
        collector.join(timeout=2.0)
        try:
            os.close(result_r)
        except OSError:
            pass
        feeder.join(timeout=2.0)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if timed_out:
        return SandboxOutcome(status="wall_timeout", error="wall-clock timeout")
    if overflow:
        return SandboxOutcome(status="sandbox_error", error="result stream overflow")

    # Only lines bearing the nonce are trusted.
    text = buf.decode("utf-8", errors="replace")
    trusted: Optional[dict] = None
    prefix = nonce + ":"
    for line in text.splitlines():
        if line.startswith(prefix):
            try:
                trusted = json.loads(line[len(prefix):])
            except (json.JSONDecodeError, ValueError):
                continue
    if trusted is None:
        return SandboxOutcome(status="no_result",
                              error="child produced no authenticated result line")

    status = trusted.get("status", "sandbox_error")
    results: Dict[str, dict] = {}
    for rec in trusted.get("results", []):
        if isinstance(rec, dict) and "id" in rec:
            results[str(rec["id"])] = rec
    return SandboxOutcome(status=status, results=results,
                          error=str(trusted.get("error", "")))


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
