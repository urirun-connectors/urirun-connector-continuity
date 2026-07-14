# Author: Tom Sapletta · Part of the ifURI solution.
from __future__ import annotations

from urirun_connector_continuity import core


def test_bindings():
    b = core.urirun_bindings()["bindings"]
    for r in ("gap://host/ticket/query/ready", "gap://host/ticket/query/verify",
              "gap://host/ticket/query/analyze", "gap://host/query/scan"):
        assert r in b


def test_needs_creds():
    assert core._needs_creds({"description": "needs IMAP creds via secret://"})
    assert not core._needs_creds({"name": "Refactor render module"})


def test_verify_unverifiable_without_criteria(monkeypatch):
    monkeypatch.setattr(core, "_ticket", lambda p, t: {"id": t, "acceptance_criteria": []})
    monkeypatch.setattr(core, "_stored_criteria", lambda tid: [])  # brak też w verify-store
    monkeypatch.setattr("urirun_connector_watchdog.core._log_lines", lambda project, n=800: [], raising=False)
    v = core.verify_ability("/x", "IFURI-033")
    assert v["verifiable"] is False and "nieweryfikowalny" in v["warn"]


def test_verify_rejects_placeholder_criteria(monkeypatch):
    monkeypatch.setattr(core, "_ticket", lambda p, t: {"id": t, "acceptance_criteria": []})
    monkeypatch.setattr(core, "_stored_criteria", lambda tid: [
        {"label": "postcondition", "cmd": "echo TODO-signal-postcondition"},
        {"label": "delivered"},
    ])
    monkeypatch.setattr("urirun_connector_watchdog.core._log_lines", lambda project, n=800: [], raising=False)
    v = core.verify_ability("/x", "IFURI-039")
    assert v["verifiable"] is False
    assert v["invalid_criteria_count"] == 2
    assert "placeholder" in v["warn"]


def test_analyze_next_action_prioritises_criteria(monkeypatch):
    monkeypatch.setattr(core, "_ticket", lambda p, t: {"id": t, "status": "open", "acceptance_criteria": []})
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_watchdog_signal", lambda p, t: {})
    monkeypatch.setattr(core, "_stored_criteria", lambda tid: [])  # izoluj od realnego verify-store
    a = core.analyze("/x", "IFURI-034")
    assert not a["ready"]
    assert "acceptance_criteria" in a["next_action"]


def test_ready_when_executor_and_no_blockers(monkeypatch):
    monkeypatch.setattr(core, "_ticket", lambda p, t: {"id": t, "status": "open",
                                                       "acceptance_criteria": [{"label": "tests", "cmd": "pytest -q"}]})
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_watchdog_signal", lambda p, t: {})
    r = core.readiness("/x", "T-1")
    assert r["ready"] is True and not r["blocking"]
    a = core.analyze("/x", "T-1")
    assert a["ready"] and "Wykonaj agentem" in a["next_action"]


def test_kvm_signal_ticket_bypasses_signal_runtime_block(monkeypatch):
    monkeypatch.setattr(core, "_ticket", lambda p, t: {
        "id": t,
        "status": "open",
        "labels": ["signal", "kvm", "lenovo", "signal-gui"],
        "name": "Send signal message",
        "acceptance_criteria": [{"label": "sent", "cmd": "python -c 'assert True'"}],
    })
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_watchdog_signal", lambda p, t: {})

    class FakeGoal:
        @staticmethod
        def signal_channel(node="lenovo"):
            return {"channel": "signal-gui-kvm", "node": node, "ready": True}

    monkeypatch.setitem(__import__("sys").modules, "urirun_connector_work.goal", FakeGoal)

    r = core.readiness("/x", "T-signal-kvm")
    assert r["ready"] is True
    assert not any("Signal działa" in b for b in r["blocking"])
    assert not any("brak connectora signal" in b for b in r["blocking"])


def test_blocked_and_in_progress_are_not_runnable(monkeypatch):
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_stored_criteria", lambda tid: [{"label": "tests", "cmd": "pytest -q"}])
    for status in ("blocked", "in_progress"):
        monkeypatch.setattr(core, "_ticket", lambda p, t, status=status: {
            "id": t,
            "status": status,
            "acceptance_criteria": [],
        })
        r = core.readiness("/x", "T-1")
        assert r["ready"] is False
        assert any(status in b for b in r["blocking"])


def test_execution_state_blocked_overrides_open_status(monkeypatch):
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_ticket", lambda p, t: {
        "id": t,
        "status": "open",
        "execution": {"state": "blocked"},
        "acceptance_criteria": [{"label": "tests", "cmd": "pytest -q"}],
    })
    r = core.readiness("/x", "T-1")
    assert r["ready"] is False
    assert any("execution.state blocked" in b for b in r["blocking"])


def test_human_labels_are_not_runnable(monkeypatch):
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_ticket", lambda p, t: {
        "id": t,
        "status": "open",
        "labels": ["needs-human:pypi-token"],
        "acceptance_criteria": [{"label": "published", "cmd": "python -c 'assert True'"}],
    })
    r = core.readiness("/x", "T-2")
    assert r["ready"] is False
    assert any("needs-human:pypi-token" in b for b in r["blocking"])


def test_scan_does_not_raise_global_criteria_alarm_for_blocked_tickets(monkeypatch):
    monkeypatch.setattr(core, "_open_tickets", lambda p: [{"id": "T-1", "status": "blocked"}])
    monkeypatch.setattr(core, "_ticket", lambda p, t: {
        "id": t,
        "status": "blocked",
        "acceptance_criteria": [],
    })
    monkeypatch.setattr(core, "_ctx", lambda p: {"executor": (True, ["claude"]), "stuck": {}, "lines": []})
    monkeypatch.setattr(core, "_executor_available", lambda: (True, ["claude"]))
    monkeypatch.setattr(core, "_stored_criteria", lambda tid: [])

    scan = core.scan("/x")

    assert scan["no_criteria"] == 1
    assert scan["no_criteria_runnable"] == 0
    assert scan["no_criteria_blocked"] == 1
    assert not any("WSZYSTKIE" in item for item in scan["systemic"])
