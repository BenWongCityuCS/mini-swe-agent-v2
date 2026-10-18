from datetime import datetime

from minisweagent.automation.runner import run_automation
from minisweagent.automation.schedule import cron_matches, is_due, parse_interval
from minisweagent.automation.store import Automation, StateStore, discover_automations


def test_parse_interval():
    assert parse_interval("30m") == 1800
    assert parse_interval("2h") == 7200
    assert parse_interval("1d") == 86400
    assert parse_interval(5) == 300


def test_cron_matches():
    assert cron_matches("0 3 * * *", datetime(2026, 1, 1, 3, 0))
    assert not cron_matches("0 3 * * *", datetime(2026, 1, 1, 3, 1))
    assert cron_matches("*/15 * * * *", datetime(2026, 1, 1, 0, 45))
    assert not cron_matches("*/15 * * * *", datetime(2026, 1, 1, 0, 44))
    # 2026-01-05 is a Monday; cron weekday 1 = Monday.
    assert cron_matches("0 9 * * 1", datetime(2026, 1, 5, 9, 0))
    assert not cron_matches("0 9 * * 1", datetime(2026, 1, 6, 9, 0))


def test_is_due_once_per_minute():
    now = datetime(2026, 1, 1, 3, 0, 30)
    assert is_due(schedule="0 3 * * *", interval_seconds=None, last_run=None, now=now)
    ran_this_minute = datetime(2026, 1, 1, 3, 0, 10)
    assert not is_due(schedule="0 3 * * *", interval_seconds=None, last_run=ran_this_minute, now=now)
    ran_before = datetime(2026, 1, 1, 2, 59, 59)
    assert is_due(schedule="0 3 * * *", interval_seconds=None, last_run=ran_before, now=now)


def test_interval_due():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert is_due(schedule=None, interval_seconds=3600, last_run=None, now=now)
    assert not is_due(
        schedule=None, interval_seconds=3600, last_run=datetime(2026, 1, 1, 11, 30), now=now
    )
    assert is_due(
        schedule=None, interval_seconds=3600, last_run=datetime(2026, 1, 1, 10, 59), now=now
    )


def test_discover_and_state(tmp_path):
    directory = tmp_path / "automations"
    directory.mkdir()
    (directory / "nightly.yaml").write_text(
        "name: nightly\ntask: run tests\nschedule: '0 3 * * *'\nenabled: true\n"
        "config:\n  agent:\n    step_limit: 10\n",
        encoding="utf-8",
    )
    (directory / "broken.yaml").write_text("name: broken\n", encoding="utf-8")

    automations = discover_automations(["automations"], tmp_path)
    assert set(automations) == {"nightly"}
    assert automations["nightly"].schedule == "0 3 * * *"
    assert automations["nightly"].config["agent"]["step_limit"] == 10

    state = StateStore(tmp_path / "state.json")
    assert state.last_run("nightly") is None
    state.mark_run("nightly", "Submitted", when=datetime(2026, 1, 1, 3, 0))
    assert state.data["nightly"]["runs"] == 1
    reloaded = StateStore(tmp_path / "state.json")
    assert reloaded.last_run("nightly") == datetime(2026, 1, 1, 3, 0)
    assert reloaded.data["nightly"]["last_status"] == "Submitted"


def test_runner_runs_deterministic_automation(tmp_path):
    automation = Automation(
        name="demo",
        path=tmp_path / "demo.yaml",
        task="do it",
        interval_seconds=60,
        config={
            "model": {
                "model_class": "deterministic",
                "model_name": "deterministic",
                "outputs": [
                    {
                        "role": "assistant",
                        "content": "",
                        "extra": {
                            "actions": [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}],
                            "cost": 0.0,
                        },
                    }
                ],
            },
            "agent": {
                "system_template": "sys",
                "instance_template": "{{task}}",
                "step_limit": 2,
            },
        },
    )
    state = StateStore(tmp_path / "state.json")
    status = run_automation(
        automation,
        workspace=tmp_path,
        state=state,
        runs_root=tmp_path / "runs",
    )
    assert status == "Submitted"
    assert state.last_run("demo") is not None
    assert state.data["demo"]["runs"] == 1
    assert list((tmp_path / "runs" / "demo").glob("*.json"))
    assert list((tmp_path / "runs" / "demo").glob("*.log"))
