# Author: Tom Sapletta · Part of the ifURI solution.
"""urirun-connector-continuity — obnaża NIEDOPOWIEDZENIA jako jawne fakty URI (`gap://`).

Dziś status „in_progress" UKRYWA prawdę: brak executora + brak acceptance_criteria + brak creds.
Autonomia jest niemożliwa, dopóki te luki są zakopane w logach. Ten connector czyni je pierwszej
klasy faktami URI, żeby pętla sterująca mogła zdecydować POPRAWNIE:

  * ``gap://host/ticket/query/ready``   — czy ticket jest WYKONYWALNY teraz? (executor? creds? oscylacja?)
  * ``gap://host/ticket/query/verify``  — czy „Done" jest WERYFIKOWALNY? (acceptance_criteria + dowód pracy)
  * ``gap://host/ticket/query/analyze`` — pełna luka: czego brakuje + JEDNA następna poprawna akcja
  * ``gap://host/query/scan``           — luki wszystkich otwartych ticketów + luki SYSTEMOWE

Komponuje istniejące connectory diagnostyczne (watchdog: rootcause/oscylacja/dowody pracy;
agents: czy jest executor) + planfile (kryteria/status). Read-only, koperta nie rzuca.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import urirun

CONNECTOR_ID = "continuity"
conn = urirun.connector(CONNECTOR_ID, scheme="gap")

_DONE = {"done", "closed", "cancelled", "canceled"}
_NON_RUNNABLE_STATUS = {"blocked", "waiting_input", "in_progress", "claimed"}
_CRED_HINTS = ("secret://", "creds", "credential", "app-password", "imap", "hasł", "password", "keyring")
_HUMAN_LABELS = ("actor:human", "actor:system")
_HUMAN_PREFIXES = ("needs-human",)
_SIGNAL_REPLY_HINTS = ("odpisz", "reply", "ostatni", "ostatnią", "ponów ifuri-039")
_SIGNAL_LINK_HINTS = (
    "signal-not-linked",
    "signal-cli link",
    "nie jest podlinkowany",
    "niezalinkowany",
    "pusty inbox",
    "empty inbox",
    "realna wysyłka",
)


def _ok(**kw: Any) -> dict[str, Any]:
    return urirun.ok(connector=CONNECTOR_ID, **kw)


def _fail(msg: str, action: str) -> dict[str, Any]:
    return urirun.fail(msg, connector=CONNECTOR_ID, action=action)


def _project(project: str = "") -> str:
    return project or os.environ.get("URIRUN_KORU_PROJECT") or os.path.expanduser("~/github/if-uri")


def _planfile() -> str | None:
    b = os.environ.get("URIRUN_PLANFILE_BIN") or shutil.which("planfile")
    if b:
        return b
    for c in ("~/github/if-uri/venv/bin/planfile", "~/github/semcod/koru/.venv/bin/planfile"):
        from pathlib import Path
        if Path(c).expanduser().is_file():
            return str(Path(c).expanduser())
    return None


def _ticket(project: str, tid: str) -> dict:
    pf = _planfile()
    if not pf:
        return {}
    try:
        cp = subprocess.run([pf, "ticket", "show", tid, "--format", "json"],
                            capture_output=True, text=True, timeout=15, cwd=_project(project))
        raw = cp.stdout
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1]) if "{" in raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _open_tickets(project: str) -> list[dict]:
    pf = _planfile()
    if not pf:
        return []
    try:
        cp = subprocess.run([pf, "ticket", "list", "--format", "json"],
                            capture_output=True, text=True, timeout=15, cwd=_project(project))
        raw = cp.stdout
        data = json.loads(raw[raw.index("["):raw.rindex("]") + 1]) if "[" in raw else []
    except Exception:  # noqa: BLE001
        return []
    return [t for t in data if t.get("status") not in _DONE]


# ---------------------------------------------------------------- czynniki luki (composed)

def _executor_available() -> tuple[bool, list[str]]:
    try:
        from urirun_connector_agents import core as ag
        av = ag._available()
        headless = [k for k, v in av.items() if not (ag._ADAPTERS.get(k) or {}).get("gui")]
        return bool(headless), headless
    except Exception:  # noqa: BLE001
        return False, []


def _ctx(project: str) -> dict:
    """Kosztowne, WSPÓLNE sygnały policzone RAZ (executor + watchdog stuck-map + log) —
    żeby scan(n ticketów) nie był O(n²) subprocessów."""
    exec_ok, tools = _executor_available()
    stuck, lines = {}, []
    try:
        from urirun_connector_watchdog import core as wd
        stuck = {s["id"]: s for s in wd.detect(project=project).get("stuck", [])}
        lines = wd._log_lines(_project(project), 800)
    except Exception:  # noqa: BLE001
        pass
    return {"executor": (exec_ok, tools), "stuck": stuck, "lines": lines}


def _watchdog_signal(project: str, tid: str, ctx: dict | None = None) -> dict:
    if ctx is not None:
        return ctx["stuck"].get(tid, {})
    try:
        from urirun_connector_watchdog import core as wd
        return {s["id"]: s for s in wd.detect(project=project).get("stuck", [])}.get(tid, {})
    except Exception:  # noqa: BLE001
        return {}


def _needs_creds(t: dict) -> bool:
    blob = (str(t.get("name", "")) + " " + str(t.get("description", "")) + " "
            + json.dumps(t.get("outputs", {}), ensure_ascii=False)).lower()
    return any(h in blob for h in _CRED_HINTS)


def _stored_criteria(tid: str) -> list:
    """acceptance_criteria z verify-store (gdy planfile ich nie modeluje) — spina gap↔verify."""
    try:
        from urirun_connector_verify import core as vf
        return vf._load().get(tid) or []
    except Exception:  # noqa: BLE001
        return []


def _labels(t: dict) -> list[str]:
    return [str(x).lower() for x in (t.get("labels") or [])]


def _ticket_blob(t: dict) -> str:
    return (
        str(t.get("name", "")) + " " +
        str(t.get("description", "")) + " " +
        json.dumps(t.get("outputs", {}), ensure_ascii=False)
    ).lower()


def _human_label_block(t: dict) -> str | None:
    labels = _labels(t)
    for lab in labels:
        if lab in _HUMAN_LABELS or any(lab.startswith(prefix) for prefix in _HUMAN_PREFIXES):
            return f"input operatora / {lab}"
    return None


def _known_human_block(t: dict) -> str | None:
    blob = _ticket_blob(t)
    if any(h in blob for h in _SIGNAL_LINK_HINTS):
        return "input operatora / Signal niepodlinkowany (signal-cli link)"
    return None


def _valid_check(c: Any) -> bool:
    # planfile stores acceptance_criteria as plain strings; some callers pass {label, cmd} dicts.
    if isinstance(c, dict):
        cmd = str(c.get("cmd") or "").strip()
        label = str(c.get("label") or "")
    elif isinstance(c, str):
        cmd, label = c.strip(), ""
    else:
        return False
    if not cmd:
        return False
    low = (cmd + " " + label).lower()
    if "todo" in low or "placeholder" in low:
        return False
    return True


def _kvm_signal_delivery_ready(t: dict) -> bool:
    labels = _labels(t)
    blob = _ticket_blob(t)
    if "signal" not in labels and "signal" not in blob:
        return False
    if not any(x in labels for x in ("kvm", "lenovo", "signal-gui", "signal-gui-kvm")) and not any(x in blob for x in ("kvm", "lenovo", "signal-gui", "signal-gui-kvm")):
        return False
    try:
        from importlib import import_module
        goal = import_module("urirun_connector_work.goal")
        ch = goal.signal_channel("lenovo")
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(ch, dict) and ch.get("ready") and str(ch.get("channel", "")).lower() in {"signal-gui-kvm", "signal-gui"})


def _signal_runtime_block(t: dict) -> str | None:
    labels = _labels(t)
    blob = _ticket_blob(t)
    if "signal" not in labels and "signal://" not in blob:
        return None
    if "code" in labels or "connector-gen" in labels:
        return None
    if _kvm_signal_delivery_ready(t):
        return None
    try:
        from urirun_connector_signal import core as sig
    except Exception:  # noqa: BLE001
        return None
    try:
        if sig._mock():
            return "input operatora / Signal działa w trybie mock; realne dostarczenie wymaga signal-cli link"
    except Exception:  # noqa: BLE001
        return None
    if any(h in blob for h in _SIGNAL_REPLY_HINTS):
        try:
            inbox = sig.messages_query_inbox()
            if not inbox.get("last"):
                return "input operatora / Signal bez ostatniej wiadomości (pusty inbox)"
        except Exception:  # noqa: BLE001
            return "input operatora / nie można potwierdzić inbox Signal"
    return None


# Zdolności APP/GUI, których generyczny agent:// (claude/codex) NIE wykona — potrzebny connector app.
_APP_CAPS = ("signal", "whatsapp", "telegram", "slack", "discord", "messenger", "sms", "voice")


def _kvm_signal_ticket(t: dict) -> bool:
    """Ticket wysyłki Signal przez KVM (nie signal-cli na hoście)."""
    labels = _labels(t)
    if any(l in labels for l in ("kvm", "lenovo", "signal-gui")):
        return "signal" in labels or "signal://" in _ticket_blob(t) or "signal" in str(t.get("name", "")).lower()
    return False


def _kvm_signal_channel_ready(t: dict) -> bool:
    """Czy węzeł ma gotowy kanał signal-gui-kvm (Signal Desktop + KVM)."""
    if not _kvm_signal_ticket(t):
        return False
    try:
        from urirun_connector_work.goal import signal_channel
        from urirun_connector_work.signal_kvm import resolve_node
        ch = signal_channel(node=resolve_node(t))
        return ch.get("channel") == "signal-gui-kvm" and bool(ch.get("ready"))
    except Exception:  # noqa: BLE001
        return False


def _missing_capability(t: dict) -> str | None:
    """Czy zadanie wymaga zdolności APP bez wykonawcy (np. signal:// / signal-cli)?"""
    labels = _labels(t)
    # Zadanie KODOWE (napisz/wygeneruj connector) robi agent:// — NIE mylić z zadaniem APP,
    # nawet gdy w nazwie pada 'signal' (np. „Wygeneruj connector signal://").
    if "code" in labels or "connector-gen" in labels:
        return None
    blob = (str(t.get("name", "")) + " " + str(t.get("description", ""))).lower()
    for cap in _APP_CAPS:
        if cap in labels or cap in blob:
            try:
                __import__(f"urirun_connector_{cap}")
                return None
            except Exception:  # noqa: BLE001
                pass
            if shutil.which(f"{cap}-cli"):
                return None
            return f"executor dla '{cap}' (brak connectora {cap}:// ani {cap}-cli — wygeneruj/kvm-drive)"
    return None


# ---------------------------------------------------------------- ready / verify / analyze

def _readiness(t: dict, tid: str, ctx: dict) -> dict[str, Any]:
    if not t:
        return {"id": tid, "ready": False, "blocking": ["ticket nie istnieje"]}
    blocking = []
    status = str(t.get("status") or "").lower()
    execution_state = str(((t.get("execution") or {}).get("state")) or "").lower()
    if status in _NON_RUNNABLE_STATUS:
        blocking.append(f"status {status} — nie uruchamiaj nowego agenta")
    if execution_state in _NON_RUNNABLE_STATUS and execution_state != status:
        blocking.append(f"execution.state {execution_state} — status niespójny, nie uruchamiaj nowego agenta")
    human_block = _human_label_block(t) or _known_human_block(t)
    if human_block:
        blocking.append(human_block)
    if t.get("status") in ("waiting_input",) or (_needs_creds(t) and t.get("status") == "blocked"):
        blocking.append("input operatora / creds (secret://)")
    kvm_signal = _kvm_signal_channel_ready(t)
    cap_block = None if kvm_signal else _missing_capability(t)
    signal_block = None if kvm_signal else _signal_runtime_block(t)
    exec_ok, tools = ctx["executor"]
    if signal_block:
        blocking.append(signal_block)
    elif cap_block:
        blocking.append(cap_block)  # zadanie APP: liczy się wykonawca APP, nie generyczny agent
    elif not exec_ok:
        blocking.append("executor (agent:// / żadne narzędzie headless)")
    sig = ctx["stuck"].get(tid, {})
    if sig.get("dead_loop"):
        blocking.append(f"oscylacja {sig.get('cycles')}× — wymaga diagnozy, nie retry")
    elif sig:
        blocking.append(f"watchdog {sig.get('category') or sig.get('verdict') or 'stuck'} — wymaga korekty, nie retry")
    return {"id": tid, "ready": not blocking, "blocking": blocking,
            "executor_tools": tools, "status": t.get("status"), "execution_state": execution_state}


def _verify(t: dict, tid: str, ctx: dict) -> dict[str, Any]:
    crit = (t or {}).get("acceptance_criteria") or _stored_criteria(tid)  # planfile → verify-store
    valid = [c for c in (crit or []) if _valid_check(c)]
    invalid = len(crit or []) - len(valid)
    real = 0
    try:
        from urirun_connector_watchdog import core as wd
        real = wd._evidence(ctx["lines"], tid).get("real_work", 0)
    except Exception:  # noqa: BLE001
        pass
    warn = ""
    if not crit:
        warn = "brak acceptance_criteria — Done nieweryfikowalny"
    elif not valid:
        warn = "brak sprawdzalnych acceptance_criteria — cmd puste albo placeholder/TODO"
    return {"id": tid, "verifiable": bool(valid), "criteria_count": len(crit or []),
            "valid_criteria_count": len(valid), "invalid_criteria_count": invalid, "criteria": crit,
            "has_real_work": real > 0, "real_work_ops": real,
            "warn": warn}


def readiness(project: str, tid: str) -> dict[str, Any]:
    """Czy ticket jest WYKONYWALNY teraz — i jeśli nie, to CZEGO brakuje (jawnie)."""
    return _readiness(_ticket(project, tid), tid, _ctx(_project(project)))


def verify_ability(project: str, tid: str) -> dict[str, Any]:
    """Czy „Done" jest WERYFIKOWALNY — czy są acceptance_criteria i dowód realnej pracy."""
    return _verify(_ticket(project, tid), tid, _ctx(_project(project)))


def _next_action(ready: dict, verify: dict) -> str:
    for b in ready["blocking"]:
        if "creds" in b or "input" in b:
            return "dostarcz creds/secret:// i Odblokuj"
        if "oscylacja" in b:
            return "circuit-break (diagnoza) — NIE odblokowuj bez fixu rootcause"
        if "watchdog" in b or "status " in b:
            return "wstrzymaj retry — najpierw wyczyść claim/blocker i potwierdź postcondition"
        if "executor" in b:
            return "podłącz agent:// executor (zainstaluj narzędzie headless)"
    if not verify["verifiable"]:
        return "DODAJ acceptance_criteria — bez tego 'Done' jest nieweryfikowalny (fundament autonomii)"
    if ready["ready"]:
        return "GOTOWY — wykonaj agentem (▶ Wykonaj agentem)"
    return "eskaluj do operatora"


def _analyze(t: dict, tid: str, ctx: dict) -> dict[str, Any]:
    r = _readiness(t, tid, ctx)
    v = _verify(t, tid, ctx)
    missing = list(r["blocking"])
    if not v["verifiable"]:
        missing.insert(0, "acceptance_criteria (Done nieweryfikowalny)")
    return {"id": tid, "ready": r["ready"] and v["verifiable"], "missing": missing,
            "labels": [str(x) for x in (t.get("labels") or [])], "name": t.get("name", ""),
            "next_action": _next_action(r, v), "readiness": r, "verify": v}


def analyze(project: str, tid: str) -> dict[str, Any]:
    return _analyze(_ticket(project, tid), tid, _ctx(_project(project)))


def scan(project: str = "") -> dict[str, Any]:
    proj = _project(project)
    ctx = _ctx(proj)  # RAZ: executor + watchdog + log
    tickets = _open_tickets(proj)
    rows = [_analyze(_ticket(proj, t["id"]), t["id"], ctx) for t in tickets if t.get("id")]
    no_criteria = sum(1 for r in rows if "acceptance_criteria" in " ".join(r["missing"]).lower())
    criteria_scope = [r for r in rows if not (r.get("readiness") or {}).get("blocking")]
    no_criteria_runnable = sum(
        1 for r in criteria_scope if "acceptance_criteria" in " ".join(r["missing"]).lower()
    )
    not_ready = [r for r in rows if not r["ready"]]
    systemic = []
    if criteria_scope and no_criteria_runnable == len(criteria_scope):
        systemic.append(
            f"WSZYSTKIE ({no_criteria_runnable}) wykonywalne tickety bez acceptance_criteria "
            "— Done nieweryfikowalny globalnie"
        )
    exec_ok, tools = _executor_available()
    systemic.append(f"executor: {'OK ('+','.join(tools)+')' if exec_ok else 'BRAK headless narzędzia'}")
    return {"tickets": rows, "total": len(rows), "not_ready": len(not_ready),
            "no_criteria": no_criteria, "no_criteria_runnable": no_criteria_runnable,
            "no_criteria_blocked": no_criteria - no_criteria_runnable, "systemic": systemic}


@conn.handler("ticket/query/ready", isolated=False, meta={"label": "Czy ticket jest wykonywalny teraz — i czego brakuje"})
def ticket_query_ready(project: str = "", id: str = "") -> dict[str, Any]:
    try:
        return _ok(action="gap-ready", **readiness(_project(project), (id or "").strip()))
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "gap-ready")


@conn.handler("ticket/query/verify", isolated=False, meta={"label": "Czy Done jest weryfikowalny (kryteria + dowód pracy)"})
def ticket_query_verify(project: str = "", id: str = "") -> dict[str, Any]:
    try:
        return _ok(action="gap-verify", **verify_ability(_project(project), (id or "").strip()))
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "gap-verify")


@conn.handler("ticket/query/analyze", isolated=False, meta={"label": "Pełna luka ticketu: czego brakuje + następna akcja"})
def ticket_query_analyze(project: str = "", id: str = "") -> dict[str, Any]:
    try:
        return _ok(action="gap-analyze", **analyze(_project(project), (id or "").strip()))
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "gap-analyze")


@conn.handler("query/scan", isolated=False, meta={"label": "Luki wszystkich otwartych ticketów + luki systemowe"})
def query_scan(project: str = "") -> dict[str, Any]:
    try:
        return _ok(action="gap-scan", **scan(_project(project)))
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "gap-scan")


def _triage_one(t: dict, done_ids: set, project: str) -> dict[str, Any]:
    """Rekomendacja dla JEDNEGO ticketu: DELETE (stale) / UNBLOCK (gotowy/zrobiony) / KEEP (realny blocker)."""
    import re
    tid = t.get("id", "")
    labs = _labels(t)
    blob = _ticket_blob(t)
    refs = set(re.findall(r"IFURI-\d+", (t.get("name", "") + " " + " ".join(labs)))) - {tid}
    # 1) retry/generacja/blocked_by ticketu który JUŻ done → stale
    if refs & done_ids and any(k in blob for k in ("ponów", "retry", "po generacji", "capretry")):
        d = sorted(refs & done_ids)
        return {"rec": "DELETE", "confidence": "high", "why": f"{d} już done → ten retry/follow-up bezcelowy"}
    # 1b) ticket-diagnoza (transient watchdog) — stan niesie ticket-przedmiot; kaskady = czysty szum
    if t.get("name", "").startswith("DIAGNOZA:") or "diagnosis" in labs or any(l.startswith("loop-diag:") for l in labs):
        return {"rec": "DELETE", "confidence": "high", "why": "ticket-diagnoza (transient watchdog) — stan niesie ticket-przedmiot"}
    # 2) nora, której kluczowy członek (po temacie) jest done → eskalacja nieaktualna
    if "rabbit-hole" in labs:
        key = {"signal": "IFURI-039", "email": "IFURI-033"}.get(
            next((h for h in ("signal", "email") if h in blob), ""), "")
        if key in done_ids:
            return {"rec": "DELETE", "confidence": "high", "why": f"nora rozwiązana ({key} done) → eskalacja zbędna"}
        return {"rec": "KEEP", "confidence": "med", "why": "nora nierozwiązana — realny klaster"}
    # 3) verify zielony → naprawdę zrobione, źle oznaczone → UNBLOCK→done
    v = _stored_criteria(tid)
    if v and all(_valid_check(c) for c in v):
        try:
            from urirun_connector_verify import core as vf
            chk = vf.ticket_query_check(id=tid, cwd=_project(project))
            if chk.get("all_passed"):
                return {"rec": "UNBLOCK→DONE", "confidence": "high", "why": "verify zielony — faktycznie zrobione"}
        except Exception:  # noqa: BLE001
            pass
    # 4) realny blocker human/external → KEEP
    hb = _human_label_block(t) or _known_human_block(t)
    if hb or any(k in blob for k in ("signal-cli", "skan qr", "pypi-token", "zalinkuj", "zainstaluj", "deploy", "wdróż", "nodeadmin")):
        return {"rec": "KEEP/DECYZJA", "confidence": "med",
                "why": f"realny blocker ({hb or 'external/deploy'}) — sprawdź czy nadal potrzebne"}
    # 5) blocked bez blokera human i z kryteriami/executorem → można odblokować
    return {"rec": "REVIEW", "confidence": "low", "why": "brak jednoznacznego sygnału — oceń ręcznie"}


def triage(project: str = "", apply_deletes: bool = False) -> dict[str, Any]:
    """Triaż WSZYSTKICH blocked ticketów → rekomendacje DELETE/UNBLOCK/KEEP + powód. apply_deletes=True
    kasuje jednoznacznie stale (confidence=high, rec=DELETE) — resztę zostawia do decyzji operatora."""
    import subprocess
    all_t = _open_tickets(project)  # non-done
    done_ids = set()
    pf = _planfile()
    if pf:
        try:
            cp = subprocess.run([pf, "ticket", "list", "--format", "json"], capture_output=True,
                                text=True, timeout=15, cwd=_project(project))
            done_ids = {t["id"] for t in json.loads(cp.stdout[cp.stdout.index("["):cp.stdout.rindex("]") + 1])
                        if t.get("status") in ("done", "closed")}
        except Exception:  # noqa: BLE001
            pass
    blocked = [t for t in all_t if t.get("status") == "blocked"]
    rows, deleted = [], []
    for t in blocked:
        rec = _triage_one(t, done_ids, project)
        row = {"id": t.get("id"), "name": (t.get("name") or "")[:60], **rec}
        rows.append(row)
        if apply_deletes and rec["rec"] == "DELETE" and rec["confidence"] == "high" and pf:
            try:
                subprocess.run([pf, "ticket", "delete", t["id"], "--force"], capture_output=True, timeout=15, cwd=_project(project))
                deleted.append(t["id"])
            except Exception:  # noqa: BLE001
                pass
    from collections import Counter
    return {"blocked": len(blocked), "summary": dict(Counter(r["rec"] for r in rows)),
            "recommendations": rows, "auto_deleted": deleted}


@conn.handler("query/triage", isolated=True, meta={"label": "Triaż blocked ticketów: DELETE(stale)/UNBLOCK/KEEP + powód; apply_deletes kasuje stale"})
def query_triage(project: str = "", apply_deletes: bool = False) -> dict[str, Any]:
    try:
        return _ok(action="triage", **triage(_project(project), apply_deletes))
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "triage")


def urirun_bindings() -> dict[str, Any]:
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    return urirun.load_manifest(__package__) or {"id": CONNECTOR_ID}


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=urirun.load_manifest(__package__))


if __name__ == "__main__":
    raise SystemExit(main())
