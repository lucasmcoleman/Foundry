"""UI job ownership and process teardown, using tiny CPU-only subprocesses."""
import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app as ui


async def assert_process_dead(pid):
    # SIGKILL delivery and /proc removal race with the assertion. A zombie
    # has exited too; reaping an orphan is the system's responsibility.
    status = Path(f"/proc/{pid}/stat")
    for _ in range(100):
        try:
            if status.read_text().split()[2] == "Z":
                return
        except (FileNotFoundError, ProcessLookupError):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Process {pid} survived teardown")


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    state = ui.PipelineState()
    monkeypatch.setattr(ui, "state", state)
    monkeypatch.setattr(ui, "FOUNDRY_DIR", tmp_path)
    monkeypatch.setattr(ui, "VENV_PYTHON", sys.executable)
    monkeypatch.setattr(ui, "_STOP_GRACE_SECONDS", 0.01)
    return state


async def test_stop_retains_job_reservation_until_group_cleanup(isolated_state, monkeypatch):
    state = isolated_state
    state.running = True
    state.active_proc = SimpleNamespace(pid=321, returncode=None)
    signals = []
    monkeypatch.setattr(ui, "_capture_descendants", lambda proc, known=(): [])
    monkeypatch.setattr(ui.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    assert await ui.stop_pipeline() == {"status": "stopping"}
    assert state.running
    assert state.stop_requested
    assert "error" in await ui.start_pipeline(ui.RunRequest())
    assert "error" in await ui.start_flywheel(ui.FlywheelRequest())
    first_stop = state.stop_task
    await ui.stop_pipeline()
    assert state.stop_task is first_stop
    await first_stop
    assert signals == [(321, signal.SIGTERM), (321, signal.SIGKILL)]


async def test_stop_before_background_job_starts_creates_no_output(isolated_state):
    messages = []

    async def capture(msg):
        messages.append(msg)

    isolated_state.broadcast = capture
    await ui.start_pipeline(ui.RunRequest(training=ui.TrainingCfg(output_dir="output")))
    await ui.stop_pipeline()
    for _ in range(20):
        await asyncio.sleep(0)
        if not isolated_state.running:
            break
    assert not isolated_state.running
    assert not (ui.FOUNDRY_DIR / "output").exists()
    assert messages[-1] == {"type": "pipeline_done", "status": "stopped"}


@pytest.mark.parametrize("new_session", [False, True])
async def test_cancel_script_kills_descendants_and_reaps_parent(isolated_state, tmp_path, new_session):
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session={new_session!r})\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    task = asyncio.create_task(ui.run_script(script, str(tmp_path)))
    try:
        for _ in range(200):
            if pidfile.exists():
                break
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        proc = isolated_state.active_proc
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert proc.returncode is not None
        assert isolated_state.active_proc is None
        await assert_process_dead(int(pidfile.read_text()))
    finally:
        if not task.done():
            task.cancel()
        if isolated_state.active_proc:
            try:
                os.killpg(isolated_state.active_proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_stage_logs_do_not_overwrite_when_stages_finish_in_same_second(isolated_state, tmp_path):
    assert await ui.run_script("print('first')\n", str(tmp_path)) == 0
    assert await ui.run_script("print('second')\n", str(tmp_path)) == 0
    logs = sorted(tmp_path.glob("_stage_*.log"))
    assert len(logs) == 2
    assert {p.read_text().strip() for p in logs} == {"first", "second"}


async def test_stop_kills_nested_session_ignoring_term_after_parent_exits(isolated_state, tmp_path):
    pidfile = tmp_path / "detached.pid"
    child_script = (
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}], start_new_session=True)\n"
        "time.sleep(60)\n"
    )
    isolated_state.running = True
    task = asyncio.create_task(ui.run_script(script, str(tmp_path)))
    child_pid = None
    try:
        for _ in range(200):
            if pidfile.exists():
                child_pid = int(pidfile.read_text())
                break
            await asyncio.sleep(0.01)
        assert child_pid is not None
        proc = isolated_state.active_proc
        await ui.stop_pipeline()
        assert await asyncio.wait_for(task, 3) != 0
        await isolated_state.stop_task
        assert proc.returncode is not None
        await assert_process_dead(child_pid)
    finally:
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()


async def test_socket_disconnect_during_send_does_not_fail_pipeline_logging(isolated_state):
    class GoneSocket:
        async def send_json(self, message):
            isolated_state.ws_clients.remove(self)
            raise ConnectionResetError("disconnected during send")

    isolated_state.ws_clients.append(GoneSocket())
    await isolated_state.log("stage still running")
    assert isolated_state.ws_clients == []


async def test_stalled_socket_is_dropped_without_blocking_other_clients(isolated_state, monkeypatch):
    monkeypatch.setattr(ui, "_WS_SEND_TIMEOUT_SECONDS", 0.01)
    delivered = []

    class StalledSocket:
        async def send_json(self, message):
            await asyncio.Event().wait()

    class WorkingSocket:
        async def send_json(self, message):
            delivered.append(message)

    stalled, working = StalledSocket(), WorkingSocket()
    isolated_state.ws_clients = [stalled, working]
    await asyncio.wait_for(isolated_state.log("still alive"), 1)
    assert delivered[0]["text"] == "still alive"
    assert isolated_state.ws_clients == [working]
