import threading

from ssrename import system

RUNNING = """\
com.sefk.ssrename = {
	state = running
	pid = 12452
	last exit code = (never exited)
	...
		state = active
}
"""

CRASHED = """\
com.sefk.ssrename = {
	state = not running
	last exit code = 2
}
"""


def _fields(monkeypatch, output, returncode=0):
    class Result:
        def __init__(self):
            self.returncode = returncode
            self.stdout = output

    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: Result())


def test_status_running(monkeypatch):
    _fields(monkeypatch, RUNNING)
    assert system.agent_status() == "running (pid 12452)"
    assert system.agent_is_running()


def test_status_crashed_reports_the_exit_code(monkeypatch):
    _fields(monkeypatch, CRASHED)
    status = system.agent_status()
    assert "NOT running" in status and "2" in status
    assert not system.agent_is_running()


def test_status_not_loaded(monkeypatch):
    _fields(monkeypatch, "", returncode=1)
    assert system.agent_status() == "not loaded"
    assert not system.agent_is_running()


def test_first_state_wins_over_nested_ones(monkeypatch):
    """`launchctl print` repeats `state =` for each nested endpoint."""
    _fields(monkeypatch, RUNNING)
    assert system.agent_fields()["state"] == "running"


def test_plist_disables_output_buffering():
    env = system.plist_contents()["EnvironmentVariables"]
    assert env["PYTHONUNBUFFERED"] == "1"


def test_wait_for_agent_reports_a_missing_log(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "LOG_PATH", tmp_path / "missing.log")
    monkeypatch.setattr(system, "agent_is_running", lambda: False)
    ok, detail = system.wait_for_agent(timeout=0)
    assert not ok and "no log file" in detail


def test_wait_for_agent_reports_a_dead_agent(monkeypatch, tmp_path):
    log = tmp_path / "ssrename.log"
    log.write_text("old\n")
    monkeypatch.setattr(system, "LOG_PATH", log)
    monkeypatch.setattr(system, "agent_is_running", lambda: False)
    monkeypatch.setattr(system, "agent_status", lambda: "not running, exit 2")
    ok, detail = system.wait_for_agent(timeout=0)
    assert not ok and "not running" in detail


def test_wait_for_agent_succeeds_when_the_log_grows(monkeypatch, tmp_path):
    log = tmp_path / "ssrename.log"
    log.write_text("")
    monkeypatch.setattr(system, "LOG_PATH", log)
    monkeypatch.setattr(system, "agent_is_running", lambda: True)
    monkeypatch.setattr(system, "agent_status", lambda: "running (pid 1)")

    # The agent writes its startup line shortly after launchd starts it.
    timer = threading.Timer(0.2, lambda: log.write_text("watching ...\n"))
    timer.start()
    try:
        ok, detail = system.wait_for_agent(timeout=5)
    finally:
        timer.cancel()
    assert ok and "running" in detail
