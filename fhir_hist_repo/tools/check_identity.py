#!/usr/bin/env python3 -I
"""Independent checker for the cross-source patient identity dimension.

Like tools/check.py this file does **not** import the ``fhist`` package. It

* generates fixtures with tools/make_identity_fixtures.py and drives the real
  CLI only,
* re-reads the handoff NDJSON itself to learn Patient availability,
* keeps hand-written expected tables for the headline walkthrough,
* contains a tiny reference engine (~120 lines) that recomputes group
  membership day by day from the raw adjudication list, and compares it with
  the CLI output for every business date of several releases.

Run with ``python3 -I tools/check_identity.py build``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://fhir.example.org"
ALPHA = f"{BASE}/alpha"
BRAVO = f"{BASE}/bravo"
CHARLIE = f"{BASE}/charlie"
DELTA = f"{BASE}/delta"
TENANT = "tenant-care"

FAILURES: list[str] = []
NOTES: list[str] = []


def note(msg: str) -> None:
    NOTES.append(msg)
    print(f"  NOTE {msg}")


def check(name: str, got, expected) -> None:
    if got == expected:
        print(f"  PASS {name}")
    else:
        FAILURES.append(f"{name}: expected {expected!r}, got {got!r}")
        print(f"  FAIL {name}: expected {expected!r}, got {got!r}")


def run(repo: Path, *args: str, check_rc: bool = True) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "src" / "fhist" / "cli.py"),
         "--repo", str(repo), *args],
        capture_output=True, text=True,
    )
    if check_rc and proc.returncode != 0:
        raise AssertionError(
            f"command failed ({args}): rc={proc.returncode}\n"
            f"{proc.stderr}\n{proc.stdout}")
    if not proc.stdout.strip():
        return {"_rc": proc.returncode, "_stderr": proc.stderr}
    out = json.loads(proc.stdout)
    out["_rc"] = proc.returncode
    return out


def k(server: str, pid: str) -> str:
    return f"{server}|{TENANT}|{pid}"


def case(server: str, pid: str) -> dict:
    return {"source_server": server, "tenant": TENANT, "id": pid}


def unified(server: str, pid: str) -> str:
    return json.dumps([server, TENANT, pid], ensure_ascii=False,
                      separators=(",", ":"))


def the_key(raw: str) -> str:
    return raw


def write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
        for r in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures: initialise the repository and ingest every campus
# ---------------------------------------------------------------------------

def init_repo(build: Path) -> Path:
    fixtures = build / "identity-fixtures"
    if fixtures.exists():
        shutil.rmtree(fixtures)
    subprocess.run([sys.executable, "-I",
                    str(ROOT / "tools" / "make_identity_fixtures.py"),
                    str(fixtures)], check=True, capture_output=True)
    repo = build / "identity-repo"
    if repo.exists():
        shutil.rmtree(repo)
    sources = {"sources": [{"source_server": s, "aliases": []}
                          for s in (ALPHA, BRAVO, CHARLIE, DELTA)]}
    sf = build / "identity-sources.json"
    sf.write_text(json.dumps(sources, indent=2), encoding="utf-8")
    run(repo, "init", "--sources-file", str(sf))
    returns = {"alpha": 10, "bravo": 20, "charlie": 30, "delta": 40}
    for name, seq in returns.items():
        stage = run(repo, "stage", "--manifest",
                    str(fixtures / name / "manifest.json"))
        check(f"stage {name}", stage["_rc"], 0)
        adv = run(repo, "advance", "--source-server",
                  f"{BASE}/{name}", "--tenant", TENANT)
        check(f"advance {name} to baseline {seq}", adv["frontier"], seq)
    return repo


def publish(repo: Path, release: int, parent, cuts: dict[str, int],
            start: str, end: str, corrections: list[dict] | None = None,
            interrupt: str | None = None) -> dict:
    manifest = repo.parent / f"corrections-{release}.ndjson"
    write_ndjson(manifest, corrections or [])
    args = ["identity", "publish", "--release", str(release),
            "--from-date", start, "--to-date", end,
            "--corrections", str(manifest)]
    if parent is not None:
        args += ["--parent", str(parent)]
    if interrupt is not None:
        args += ["--interrupt", interrupt]
    for name, cut in cuts.items():
        args += ["--cut", f"{BASE}/{name}|{TENANT}|{cut}"]
    return run(repo, *args, check_rc=False)


def assign(repo: Path, release: int, server: str, pid: str, day: str) -> dict:
    return run(repo, "identity", "assign", "--release", str(release),
               "--date", day, "--key", k(server, pid), check_rc=False)


def verify(repo: Path, release: int, day: str, left: tuple[str, str],
           right: tuple[str, str]) -> dict:
    return run(repo, "identity", "verify", "--release", str(release),
               "--date", day, "--left", k(*left), "--right", k(*right),
               check_rc=False)


def members(repo: Path, release: int, unified_key: str, day: str) -> dict:
    return run(repo, "identity", "members", "--release", str(release),
               "--unified-key", unified_key, "--date", day, check_rc=False)


# ---------------------------------------------------------------------------
# Hand-written expected table for the headline walkthrough
# ---------------------------------------------------------------------------

HEADLINE = [
    ("d-ab", 1, "set", "same", (ALPHA, "a1"), (BRAVO, "b1"),
     "2026-01-01", "2026-02-01"),
    ("d-bc", 1, "set", "same", (BRAVO, "b1"), (CHARLIE, "c1"),
     "2026-01-10", "2026-01-20"),
    ("d-ac", 1, "set", "different", (ALPHA, "a1"), (CHARLIE, "c1"),
     "2026-01-15", "2026-01-18"),
]

# day -> {key: unified key or None for conflict}
EXPECTED_DAY = {
    "2026-01-09": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(CHARLIE, "c1")},
    "2026-01-10": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(ALPHA, "a1")},
    "2026-01-14": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(ALPHA, "a1")},
    "2026-01-15": {("alpha", "a1"): None, ("bravo", "b1"): None,
                   ("charlie", "c1"): None},
    "2026-01-16": {("alpha", "a1"): None, ("bravo", "b1"): None,
                   ("charlie", "c1"): None},
    "2026-01-17": {("alpha", "a1"): None, ("bravo", "b1"): None,
                   ("charlie", "c1"): None},
    "2026-01-18": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(ALPHA, "a1")},
    "2026-01-19": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(ALPHA, "a1")},
    "2026-01-20": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(CHARLIE, "c1")},
    "2026-01-31": {("alpha", "a1"): unified(ALPHA, "a1"),
                   ("bravo", "b1"): unified(ALPHA, "a1"),
                   ("charlie", "c1"): unified(CHARLIE, "c1")},
}

SERVER_OF = {"alpha": ALPHA, "bravo": BRAVO, "charlie": CHARLIE}


def drill_headline(repo: Path, build: Path) -> None:
    print("\n== headline walkthrough ==")
    out = publish(repo, 1, None,
                  {"alpha": 10, "bravo": 20, "charlie": 30},
                  "2026-01-01", "2026-02-01",
                  [{"decision_id": did, "revision": rev, "op": op,
                    "kind": kind,
                    "left": case(*left), "right": case(*right),
                    "interval": {"start": start, "end": end}}
                   for did, rev, op, kind, left, right, start, end
                   in HEADLINE])
    check("release 1 published", out["status"], "published")
    check("release 1 parent null", out["parent"], None)
    check("release 1 cut vector", [
        (c["source_server"], c["cut"]) for c in out["cuts"]],
        [(ALPHA, 10), (BRAVO, 20), (CHARLIE, 30)])

    # Hand table day by day, via per-key assign.
    for day, table in EXPECTED_DAY.items():
        for (name, pid), want in table.items():
            got = assign(repo, 1, SERVER_OF[name], pid, day)
            status_ok = (got["status"] in ("assigned", "conflict"))
            check(f"assign {name}|{pid} @ {day} status", status_ok, True)
            check(f"assign {name}|{pid} @ {day} unified",
                  got.get("unified_key"), want)
            if want is None:
                check(f"assign {name}|{pid} @ {day} is conflict",
                      got["status"], "conflict")

    # Day 19 the same(b,c) bridge is over: c is alone but a-b remains.
    check("day 19 charlie alone",
          assign(repo, 1, CHARLIE, "c1", "2026-01-19")["unified_key"],
          unified(ALPHA, "a1"))
    check("day 20 charlie key moves to its own key",
          assign(repo, 1, CHARLIE, "c1", "2026-01-20")["unified_key"],
          unified(CHARLIE, "c1"))

    # Reverse index: enumerate complete members.
    mem = members(repo, 1, unified(ALPHA, "a1"), "2026-01-14")
    check("members 14th status", mem["status"], "ok")
    check("members 14th complete", sorted(
        (m["source_server"], m["id"]) for m in mem["members"]),
        sorted([(ALPHA, "a1"), (BRAVO, "b1"), (CHARLIE, "c1")]))
    mem16 = members(repo, 1, unified(ALPHA, "a1"), "2026-01-16")
    check("conflict group is reported as conflict, not enumerable",
          mem16["status"], "conflict")
    check("conflict group has no unified membership", mem16["members"], [])
    mem20 = members(repo, 1, unified(CHARLIE, "c1"), "2026-01-20")
    check("20th charlie group enumerated", [
        (m["source_server"], m["id"]) for m in mem20["members"]],
        [(CHARLIE, "c1")])

    # Verify: same with a shortest a-b-c path (no direct a-c same edge).
    same = verify(repo, 1, "2026-01-14", (ALPHA, "a1"), (CHARLIE, "c1"))
    check("verify same on 14th", same["status"], "same")
    check("verify path uses two decisions", len(same["path"]), 2)
    check("verify path decision ids", [h["decision_id"] for h in same["path"]],
          ["d-ab", "d-bc"])
    check("verify path endpoints carry versions",
          [h["from"]["version"]["version_id"] for h in same["path"]],
          ["v1", "v1"])
    check("verify path carries seq coordinates",
          [h["from"]["version"]["seq"] for h in same["path"]], [10, 20])

    conflict = verify(repo, 1, "2026-01-16", (ALPHA, "a1"), (CHARLIE, "c1"))
    check("verify conflict on 16th", conflict["status"], "conflict")
    ev = conflict["evidence"]
    check("conflict evidence decision is the different edge",
          ev["decision"]["decision_id"], "d-ac")
    check("conflict evidence revision", ev["decision"]["revision"], 1)
    check("conflict evidence paths through b",
          [h["decision_id"] for h in ev["contradicting_same_path"]],
          ["d-ab", "d-bc"])
    check("conflict evidence both endpoints present",
          [e["availability"] for e in ev["decision"].values()
           if isinstance(e, dict) and "availability" in e],
          ["available", "available"])

    # distinct query failures
    missing_key = assign(repo, 1, ALPHA, "ghost", "2026-01-14")
    check("never-seen key is absent", missing_key["status"], "absent")
    out_of_range = assign(repo, 1, ALPHA, "a1", "2026-02-01")
    check("date outside range rejected", out_of_range["status"],
          "date_out_of_range")
    unknown_rel = assign(repo, 999, ALPHA, "a1", "2026-01-14")
    check("unknown release rejected", unknown_rel["status"], "unknown_release")


def drill_status_runs(repo: Path) -> None:
    print("\n== status range runs (merged adjacent results) ==")
    out = run(repo, "identity", "status", "--release", "1",
              "--key", k(CHARLIE, "c1"),
              "--from-date", "2026-01-08", "--to-date", "2026-01-22")
    check("status range rc", out["status"], "ok")
    runs = out["runs"]
    check("status runs merged by result",
          [(r["status"], r["unified_key"], r["from"], r["to"]) for r in runs],
          [("assigned", unified(CHARLIE, "c1"), "2026-01-08", "2026-01-10"),
           ("assigned", unified(ALPHA, "a1"), "2026-01-10", "2026-01-15"),
           ("conflict", None, "2026-01-15", "2026-01-18"),
           ("assigned", unified(ALPHA, "a1"), "2026-01-18", "2026-01-20"),
           ("assigned", unified(CHARLIE, "c1"), "2026-01-20", "2026-01-22")])
    check("run boundaries are consecutive half-open intervals",
          all(runs[i]["to"] == runs[i + 1]["from"]
              for i in range(len(runs) - 1)), True)
    window = run(repo, "identity", "status", "--release", "1",
                 "--key", k(CHARLIE, "c1"),
                 "--from-date", "2026-01-20", "--to-date", "2026-01-21")
    check("single-day window still gives a run",
          [(r["status"], r["from"], r["to"]) for r in window["runs"]],
          [("assigned", "2026-01-20", "2026-01-21")])


def drill_release2(repo: Path) -> None:
    print("\n== release 2: replace one same() and keep release 1 frozen ==")
    out = publish(repo, 2, 1,
                  {"alpha": 10, "bravo": 20, "charlie": 30},
                  "2026-01-01", "2026-02-01",
                  [{"decision_id": "d-bc", "revision": 2, "op": "set",
                    "kind": "same", "left": case(BRAVO, "b1"),
                    "right": case(CHARLIE, "c1"),
                    "interval": {"start": "2026-01-12", "end": "2026-01-16"}}])
    check("release 2 published", out["status"], "published")
    check("release 2 parent", out["parent"], 1)

    expected2 = {
        "2026-01-10": {("alpha", "a1"): unified(ALPHA, "a1"),
                       ("bravo", "b1"): unified(ALPHA, "a1"),
                       ("charlie", "c1"): unified(CHARLIE, "c1")},
        "2026-01-11": {("alpha", "a1"): unified(ALPHA, "a1"),
                       ("bravo", "b1"): unified(ALPHA, "a1"),
                       ("charlie", "c1"): unified(CHARLIE, "c1")},
        "2026-01-12": {("alpha", "a1"): unified(ALPHA, "a1"),
                       ("bravo", "b1"): unified(ALPHA, "a1"),
                       ("charlie", "c1"): unified(ALPHA, "a1")},
        "2026-01-15": {("alpha", "a1"): None, ("bravo", "b1"): None,
                       ("charlie", "c1"): None},
        "2026-01-16": {("alpha", "a1"): unified(ALPHA, "a1"),
                       ("bravo", "b1"): unified(ALPHA, "a1"),
                       ("charlie", "c1"): unified(CHARLIE, "c1")},
        "2026-01-19": {("alpha", "a1"): unified(ALPHA, "a1"),
                       ("bravo", "b1"): unified(ALPHA, "a1"),
                       ("charlie", "c1"): unified(CHARLIE, "c1")},
    }
    for day, table in expected2.items():
        for (name, pid), want in table.items():
            got = assign(repo, 2, SERVER_OF[name], pid, day)
            check(f"r2 {name}|{pid} @ {day}", got.get("unified_key"), want)

    dec = run(repo, "identity", "decision", "--release", "2",
              "--decision-id", "d-bc", "--date", "2026-01-15")
    check("d-bc revision reported", dec["revision"], 2)
    check("d-bc active on 15th", dec["active_on_date"], True)
    check("d-bc interval updated", [dec["date_start"], dec["date_end"]],
          ["2026-01-12", "2026-01-16"])
    dec17 = run(repo, "identity", "decision", "--release", "2",
                "--decision-id", "d-bc", "--date", "2026-01-17")
    check("d-bc inactive after its new end", dec17["active_on_date"], False)

    # Release 1 is frozen: the 16th is still a conflict there.
    for (name, pid) in (("alpha", "a1"), ("bravo", "b1"), ("charlie", "c1")):
        got = assign(repo, 1, SERVER_OF[name], pid, "2026-01-16")
        check(f"r1 frozen conflict {name}@16", got["unified_key"], None)
    check("r1 still same on the 19th",
          assign(repo, 1, CHARLIE, "c1", "2026-01-19")["unified_key"],
          unified(ALPHA, "a1"))
    # The old unified key means something new in release 2: it now lists
    # only the a-b pair, and c1 lives under its own key.
    old_members = members(repo, 2, unified(ALPHA, "a1"), "2026-01-16")
    check("old unified key resolves to release 2's members",
          sorted((m["source_server"], m["id"]) for m in old_members["members"]),
          sorted([(ALPHA, "a1"), (BRAVO, "b1")]))
    check("r2 old unified key on 16th is a different patient",
          assign(repo, 2, CHARLIE, "c1", "2026-01-16")["unified_key"],
          unified(CHARLIE, "c1"))
    # A key whose source is registered but not participating is refused.
    outside = run(repo, "identity", "assign", "--release", "2",
                  "--date", "2026-01-16", "--key", k(DELTA, "m"),
                  check_rc=False)
    check("non-participating source key refused", outside["status"],
          "key_not_participating")



def drill_retract(repo: Path) -> None:
    print("\n== release 3: retract the different() and re-check ==")
    out = publish(repo, 3, 2,
                  {"alpha": 10, "bravo": 20, "charlie": 30},
                  "2026-01-01", "2026-02-01",
                  [{"decision_id": "d-ac", "revision": 2, "op": "retract"}])
    check("release 3 published", out["status"], "published")
    dec = run(repo, "identity", "decision", "--release", "3",
              "--decision-id", "d-ac", check_rc=False)
    check("retracted decision is not effective", dec["status"],
          "unknown_decision")
    check("16th no longer conflicts in release 3",
          assign(repo, 3, ALPHA, "a1", "2026-01-16")["unified_key"],
          unified(ALPHA, "a1"))
    # In release 2 the bridge already ended on the 16th; only a-b remain
    # together and the different(a,c) edge no longer joins a group.
    check("16th in release 2 is a-b only, no conflict",
          assign(repo, 2, ALPHA, "a1", "2026-01-16")["status"], "assigned")
    check("16th in release 2 keeps c separate",
          assign(repo, 2, CHARLIE, "c1", "2026-01-16")["unified_key"],
          unified(CHARLIE, "c1"))


def drill_parallel_and_paths(repo: Path) -> None:
    print("\n== same-name keys, equal paths, parallel edges ==")
    out = publish(repo, 4, None,
                  {"alpha": 10, "delta": 40},
                  "2026-03-01", "2026-04-01",
                  [{"decision_id": "p1", "revision": 1, "op": "set",
                    "kind": "same", "left": case(DELTA, "m"),
                    "right": case(DELTA, "n"),
                    "interval": {"start": "2026-03-01", "end": "2026-04-01"}},
                   {"decision_id": "p2", "revision": 1, "op": "set",
                    "kind": "same", "left": case(DELTA, "n"),
                    "right": case(DELTA, "p"),
                    "interval": {"start": "2026-03-01", "end": "2026-04-01"}},
                   {"decision_id": "p3", "revision": 1, "op": "set",
                    "kind": "same", "left": case(DELTA, "m"),
                    "right": case(DELTA, "o"),
                    "interval": {"start": "2026-03-01", "end": "2026-04-01"}},
                   {"decision_id": "p4", "revision": 1, "op": "set",
                    "kind": "same", "left": case(DELTA, "o"),
                    "right": case(DELTA, "p"),
                    "interval": {"start": "2026-03-01", "end": "2026-04-01"}}])
    check("independent release 4 published", out["status"], "published")
    check("release 4 has no parent", out["parent"], None)
    check("alpha|a1 never merges with delta|a1",
          assign(repo, 4, ALPHA, "a1", "2026-03-10")["unified_key"],
          unified(ALPHA, "a1"))
    check("delta|a1 own group",
          assign(repo, 4, DELTA, "a1", "2026-03-10")["unified_key"],
          unified(DELTA, "a1"))

    # Equal-length m-n-p and m-o-p: both length two. Lexicographic tie-break
    # on the decision-id sequence picks (p1, p2) over (p3, p4) because
    # "p1" < "p3" at the first differing byte.
    eq = verify(repo, 4, "2026-03-10", (DELTA, "m"), (DELTA, "p"))
    check("equal-length path is same", eq["status"], "same")
    check("equal-length tie goes to first decision id",
          [h["decision_id"] for h in eq["path"]], ["p1", "p2"])

    # Parallel edges between n and p (p2 and a second same) - add a parallel
    # adjudication and republish in a new release to check tie on length one.
    out2 = publish(repo, 5, 4,
                   {"alpha": 10, "delta": 40},
                   "2026-03-01", "2026-04-01",
                   [{"decision_id": "p0", "revision": 1, "op": "set",
                     "kind": "same", "left": case(DELTA, "n"),
                     "right": case(DELTA, "p"),
                     "interval": {"start": "2026-03-01", "end": "2026-04-01"}}])
    check("release 5 published", out2["status"], "published")
    par = verify(repo, 5, "2026-03-10", (DELTA, "n"), (DELTA, "p"))
    check("parallel edges: shortest chosen", par["status"], "same")
    check("parallel edges tie-break on decision id",
          [h["decision_id"] for h in par["path"]], ["p0"])
    check("release 4 unaffected by later parallel edge",
          [h["decision_id"] for h in verify(
              repo, 4, "2026-03-10", (DELTA, "n"), (DELTA, "p"))["path"]],
          ["p2"])


def drill_outage_and_restore(repo: Path, build: Path) -> None:
    print("\n== cut advance to a tombstone, pending decisions, then restore ==")
    # Bring bravo to seq 21 (b1 tombstone).
    fixtures = build / "identity-fixtures"
    run(repo, "stage", "--manifest", str(fixtures / "bravo-advance" /
                                          "manifest.json"))
    adv = run(repo, "advance", "--source-server", BRAVO, "--tenant", TENANT,
              "--to-seq", "21")
    check("bravo advances to tombstone seq21", adv["frontier"], 21)

    out = publish(repo, 6, 3,
                  {"alpha": 10, "bravo": 21, "charlie": 30},
                  "2026-01-01", "2026-02-01",
                  [{"decision_id": "d-ac", "revision": 3, "op": "set",
                    "kind": "different", "left": case(ALPHA, "a1"),
                    "right": case(CHARLIE, "c1"),
                    "interval": {"start": "2026-01-15",
                                 "end": "2026-01-18"}}])
    check("release 6 published over tombstone", out["status"], "published")
    dec = run(repo, "identity", "decision", "--release", "6",
              "--decision-id", "d-ab", "--date", "2026-01-14")
    check("d-ab pending at tombstone cut", dec["pending"], True)
    check("d-ab endpoint availability reports tombstone",
          sorted(e["availability"] for e in dec["endpoints"]),
          ["available", "deleted"])
    check("d-ab not participating", dec["participates"], False)
    check("d-bc pending too", run(
        repo, "identity", "decision", "--release", "6", "--decision-id",
        "d-bc", "--date", "2026-01-14")["pending"], True)

    # Group-wise different edges coexist: a1 and c1 are two separate
    # conflict-free groups while a different() spans them.
    span = verify(repo, 6, "2026-01-14", (ALPHA, "a1"), (CHARLIE, "c1"))
    check("different between two separate groups", span["status"], "different")
    check("both separate groups keep their own keys",
          (assign(repo, 6, ALPHA, "a1", "2026-01-14")["unified_key"],
           assign(repo, 6, CHARLIE, "c1", "2026-01-14")["unified_key"]),
          (unified(ALPHA, "a1"), unified(CHARLIE, "c1")))
    check("a1 independent while b1 tombstoned",
          assign(repo, 6, ALPHA, "a1", "2026-01-14")["unified_key"],
          unified(ALPHA, "a1"))
    check("c1 independent while b1 tombstoned",
          assign(repo, 6, CHARLIE, "c1", "2026-01-14")["unified_key"],
          unified(CHARLIE, "c1"))
    tom = assign(repo, 6, BRAVO, "b1", "2026-01-14")
    check("tombstoned key is deleted, not missing", tom["status"], "deleted")
    never = assign(repo, 6, BRAVO, "nope", "2026-01-14")
    check("never-seen key distinct from tombstone", never["status"], "absent")

    # A pair whose endpoints are both available but unrelated is
    # "different"; pending is reserved for an unavailable endpoint.
    pair = verify(repo, 6, "2026-01-14", (ALPHA, "a1"), (CHARLIE, "c1"))
    check("a1/c1 unrelated without the a-b bridge", pair["status"], "different")
    tomb_pair = verify(repo, 6, "2026-01-14", (ALPHA, "a1"), (BRAVO, "b1"))
    check("verify reports pending when an endpoint is a tombstone",
          tomb_pair["status"], "pending")
    check("pending carries per-endpoint availability",
          [(i["available"], i["availability"]) for i in tomb_pair["items"]],
          [(True, "available"), (False, "deleted")])

    # Restore b1: seq 23 PUT.
    adv = run(repo, "advance", "--source-server", BRAVO, "--tenant", TENANT)
    check("bravo advances to seq23 (restore)", adv["frontier"], 23)
    out = publish(repo, 7, 6,
                  {"alpha": 10, "bravo": 23, "charlie": 30},
                  "2026-01-01", "2026-02-01")
    check("release 7 published after restore", out["status"], "published")
    dec = run(repo, "identity", "decision", "--release", "7",
              "--decision-id", "d-ab", "--date", "2026-01-14")
    check("d-ab active again after restore", dec["pending"], False)
    check("d-ab participates after restore", dec["participates"], True)
    check("group re-forms after restore (a1)",
          assign(repo, 7, ALPHA, "a1", "2026-01-14")["unified_key"],
          unified(ALPHA, "a1"))
    check("group re-forms after restore (c1 joined by same)",
          assign(repo, 7, CHARLIE, "c1", "2026-01-14")["unified_key"],
          unified(ALPHA, "a1"))
    # d-bc revision 2 ends on the 16th, so the 15th is the conflict day.
    check("conflict returns on the 15th",
          assign(repo, 7, ALPHA, "a1", "2026-01-15")["status"], "conflict")
    check("the 16th is a-b only again",
          assign(repo, 7, ALPHA, "a1", "2026-01-16")["status"], "assigned")
    check("d-bc revision still 2 (not recopied)",
          run(repo, "identity", "decision", "--release", "7",
              "--decision-id", "d-bc")["revision"], 2)
    check("restored different keeps revision 3",
          run(repo, "identity", "decision", "--release", "7",
              "--decision-id", "d-ac")["revision"], 3)


def drill_refresh_counts(repo: Path, build: Path) -> None:
    print("\n== refresh counts: small change never triggers a full rewrite ==")
    st = run(repo, "identity", "stats", "--release", "7")
    stats = st["stats"]
    check("stats expose seen versions per source",
          sorted(s["source_server"].rsplit("/", 1)[-1] for s in stats["sources"]),
          ["alpha", "bravo", "charlie"])
    check("stats expose recomputed and reused keys",
          ("recomputed_keys" in stats and "reused_keys" in stats), True)
    # Release 7 only changed bravo availability; alpha/charlie unchanged.
    # The unchanged a-b group is touched (b changed) but delta x1/x2 groups
    # are reused from the parent.
    check("some keys reused from parent", stats["reused_keys"] > 0, True)
    check("no full rewrite: reused + recomputed covers all member keys",
          stats["reused_keys"] + stats["recomputed_keys"] >=
          stats["membership_rows"] // 31, True)

    # Retract one edge of the m/n/o/p group on its own lineage: release 8
    # touches only that group; the unrelated a1/x3/x4/z1 groups are reused.
    out = publish(repo, 8, 5,
                  {"alpha": 10, "delta": 40},
                  "2026-03-01", "2026-04-01",
                  [{"decision_id": "p3", "revision": 2, "op": "retract"}])
    check("release 8 published", out["status"], "published")
    st8 = run(repo, "identity", "stats", "--release", "8")["stats"]
    check("release 8 recomputed only the touched group",
          st8["recomputed_keys"], 4)
    check("release 8 reused the untouched groups", st8["reused_keys"], 6)
    check("m/n/o/p still connected after retracting one edge",
          assign(repo, 8, DELTA, "m", "2026-03-10")["unified_key"],
          unified(DELTA, "m"))


def drill_rejections(repo: Path, build: Path) -> None:
    print("\n== atomic rejection: every bad publication is refused whole ==")
    base_cuts = {"alpha": 10, "bravo": 23, "charlie": 30}

    def attempt(name: str, want_status: str, **kwargs) -> None:
        out = publish(repo, kwargs.pop("release"), kwargs.pop("parent"),
                      kwargs.pop("cuts", base_cuts),
                      kwargs.pop("start", "2026-01-01"),
                      kwargs.pop("end", "2026-02-01"),
                      kwargs.pop("corrections", []))
        check(f"reject {name}", out["status"], want_status)

    # Reusing a published number with changed content.
    attempt("reused release number", "release_rejected", release=7, parent=7,
            corrections=[{"decision_id": "zz", "revision": 1, "op": "set",
                          "kind": "same", "left": case(ALPHA, "a1"),
                          "right": case(ALPHA, "x1"),
                          "interval": {"start": "2026-01-01",
                                       "end": "2026-01-02"}}])
    # Stale parent (7 already has successor 8's lineage? use 1 which has 2/3/6).
    attempt("stale parent", "release_rejected", release=60, parent=1)
    # Old revision for an existing decision (d-bc is at revision 2 in the
    # release 7 ancestry).
    attempt("old revision", "release_rejected", release=61, parent=7,
            corrections=[{"decision_id": "d-bc", "revision": 1, "op": "set",
                          "kind": "same", "left": case(BRAVO, "b1"),
                          "right": case(CHARLIE, "c1"),
                          "interval": {"start": "2026-01-01",
                                       "end": "2026-01-02"}}])
    # Duplicate decision id inside the batch.
    attempt("duplicate decision id", "release_rejected", release=62, parent=7,
            corrections=[{"decision_id": "dup", "revision": 1, "op": "set",
                          "kind": "same", "left": case(ALPHA, "a1"),
                          "right": case(BRAVO, "b1"),
                          "interval": {"start": "2026-01-01",
                                       "end": "2026-01-02"}},
                         {"decision_id": "dup", "revision": 2, "op": "set",
                          "kind": "same", "left": case(ALPHA, "a1"),
                          "right": case(BRAVO, "b1"),
                          "interval": {"start": "2026-01-03",
                                       "end": "2026-01-04"}}])
    # Illegal interval (outside range, empty, or reversed).
    attempt("empty interval", "release_rejected", release=63, parent=7,
            corrections=[{"decision_id": "e", "revision": 1, "op": "set",
                          "kind": "same", "left": case(ALPHA, "a1"),
                          "right": case(BRAVO, "b1"),
                          "interval": {"start": "2026-01-05",
                                       "end": "2026-01-05"}}])
    attempt("out-of-range interval", "release_rejected", release=64, parent=7,
            corrections=[{"decision_id": "e", "revision": 1, "op": "set",
                          "kind": "same", "left": case(ALPHA, "a1"),
                          "right": case(BRAVO, "b1"),
                          "interval": {"start": "2025-12-01",
                                       "end": "2026-01-01"}}])
    # Unknown source in an endpoint.
    attempt("unknown source in decision", "release_rejected", release=65,
            parent=7,
            corrections=[{"decision_id": "u", "revision": 1, "op": "set",
                          "kind": "same", "left": case("https://nope/x", "a1"),
                          "right": case(BRAVO, "b1"),
                          "interval": {"start": "2026-01-01",
                                       "end": "2026-01-02"}}])
    # Unpublished cut.
    attempt("unpublished cut", "release_rejected", release=66, parent=7,
            cuts={"alpha": 10, "bravo": 999, "charlie": 30})
    # Retract of a decision absent from the parent.
    attempt("retract absent decision", "release_rejected", release=67,
            parent=7,
            corrections=[{"decision_id": "not-there", "revision": 1,
                          "op": "retract"}])
    # First release may not have a parent.
    attempt("first release with parent", "release_rejected", release=68,
            parent=999, corrections=[],
            cuts={"delta": 40})

    # Nothing above became visible: release list length unchanged.
    lst = run(repo, "identity", "list")
    check("no rejected release became visible", len(lst["releases"]), 8)
    # A rejected publish must not have left a half-written release 60..68.
    for release in (60, 61, 62, 63, 64, 65, 66, 67, 68):
        check(f"rejected release {release} absent", run(
            repo, "identity", "show", "--release", str(release),
            check_rc=False)["status"], "unknown_release")


def drill_retry_and_race(repo: Path, build: Path) -> None:
    print("\n== idempotent retry, interrupted publish, parent race ==")
    # A byte-identical re-publication folds onto the original.
    again = publish(repo, 8, 5, {"alpha": 10, "delta": 40},
                    "2026-03-01", "2026-04-01",
                    [{"decision_id": "p3", "revision": 2, "op": "retract"}])
    check("identical retry folds", again["status"], "folded")
    check("fold returns original id", again["release"], 8)
    check("fold returns original stats",
          again["stats"]["recomputed_keys"], 4)
    # The fold works even after later releases exist (head moved on).
    later = publish(repo, 9, 8, {"alpha": 10, "delta": 40},
                    "2026-03-01", "2026-04-01",
                    [{"decision_id": "q2", "revision": 1, "op": "set",
                      "kind": "different", "left": case(DELTA, "m"),
                      "right": case(DELTA, "p"),
                      "interval": {"start": "2026-03-01",
                                   "end": "2026-04-01"}}])
    check("release 9 published", later["status"], "published")
    again8 = publish(repo, 8, 5, {"alpha": 10, "delta": 40},
                     "2026-03-01", "2026-04-01",
                     [{"decision_id": "p3", "revision": 2, "op": "retract"}])
    check("retry of 8 still folds after head moved", again8["status"],
          "folded")
    check("release 9 shows conflict from the different edge",
          assign(repo, 9, DELTA, "m", "2026-03-10")["status"], "conflict")

    # Parent race: two publications from the same parent. With release 9 as
    # the current head of the delta lineage, 10 and 11 both name 9 as parent.
    first = publish(repo, 10, 9, {"alpha": 10, "delta": 40},
                    "2026-03-01", "2026-04-01")
    second = publish(repo, 11, 9, {"alpha": 10, "delta": 40},
                     "2026-03-01", "2026-04-01")
    check("first child of parent 9 wins", first["status"], "published")
    check("second child of same parent refused", second["status"],
          "release_rejected")
    # Release 10 did not change anything: every member key is reused.
    st10 = run(repo, "identity", "stats", "--release", "10")["stats"]
    check("no-change release reuses all member keys",
          (st10["reused_keys"], st10["recomputed_keys"]), (10, 0))

    # Interrupted publish: the process dies after writing every row but
    # before COMMIT. The release must be invisible, the parent must still be
    # the head, and an identical retry must then succeed.
    killed = publish(repo, 12, 10, {"alpha": 10, "delta": 40},
                     "2026-03-01", "2026-04-01",
                     [{"decision_id": "r12", "revision": 1, "op": "set",
                       "kind": "same", "left": case(DELTA, "m"),
                       "right": case(DELTA, "n"),
                       "interval": {"start": "2026-03-01",
                                    "end": "2026-04-01"}}],
                     interrupt="exit")
    check("interrupted publish exits abnormally", killed["_rc"], 17)
    gone = run(repo, "identity", "show", "--release", "12", check_rc=False)
    check("interrupted release is not visible", gone["status"],
          "unknown_release")
    retry = publish(repo, 12, 10, {"alpha": 10, "delta": 40},
                    "2026-03-01", "2026-04-01",
                    [{"decision_id": "r12", "revision": 1, "op": "set",
                      "kind": "same", "left": case(DELTA, "m"),
                      "right": case(DELTA, "n"),
                      "interval": {"start": "2026-03-01",
                                   "end": "2026-04-01"}}])
    check("retry after interruption succeeds", retry["status"], "published")
    check("retried release 12 visible", run(
        repo, "identity", "show", "--release", "12")["release"], 12)
    check("retry applied the decision that was interrupted",
          run(repo, "identity", "decision", "--release", "12",
              "--decision-id", "r12")["kind"], "same")

    # A failed publish (unknown cut) leaves no trace either.
    broken = publish(repo, 14, 12, {"alpha": 10, "delta": 999},
                     "2026-03-01", "2026-04-01")
    check("publish on unpublished cut rejected", broken["status"],
          "release_rejected")
    check("rejected number is still free", run(
        repo, "identity", "show", "--release", "14",
        check_rc=False)["status"], "unknown_release")


def drill_query_snapshot(repo: Path, build: Path) -> None:
    print("\n== query snapshot while a source cut advances ==")
    # Release 7 pins bravo at cut 23, so b2 (seq22) is already available but
    # has no adjudication yet. While a reader holds release 7's snapshot open,
    # a new release 13 adds same(a1,b2). The in-flight reader must stay on
    # release 7's committed groups; a fresh query sees release 13.
    proc = subprocess.Popen(
        [sys.executable, "-I", str(ROOT / "src" / "fhist" / "cli.py"),
         "--repo", str(repo), "identity", "assign", "--release", "7",
         "--date", "2026-01-14", "--key", k(ALPHA, "a1"),
         "--hold-open", "2.0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.6)
    note("sync point: identity reader open on release 7, writer publishing 13")
    pub = publish(repo, 13, 7, {"alpha": 10, "bravo": 23, "charlie": 30},
                  "2026-01-01", "2026-02-01",
                  [{"decision_id": "d-ab2", "revision": 1, "op": "set",
                    "kind": "same", "left": case(ALPHA, "a1"),
                    "right": case(BRAVO, "b2"),
                    "interval": {"start": "2026-01-01",
                                 "end": "2026-02-01"}}])
    check("writer publishes release 13 concurrently", pub["status"],
          "published")
    out, err = proc.communicate(timeout=20)
    payload = json.loads(out)
    check("in-flight identity query stayed on release 7",
          payload["unified_key"], unified(ALPHA, "a1"))
    check("b2 is available in release 7 but not yet bridged",
          assign(repo, 7, BRAVO, "b2", "2026-01-14")["unified_key"],
          unified(BRAVO, "b2"))
    check("release 13 sees b2 available",
          assign(repo, 13, BRAVO, "b2", "2026-01-14")["availability"],
          "available")
    check("release 13 joins a1 and b2",
          assign(repo, 13, BRAVO, "b2", "2026-01-14")["unified_key"],
          unified(ALPHA, "a1"))
    check("frozen release 7 keeps its own attribution for b1",
          assign(repo, 7, BRAVO, "b1", "2026-01-14")["unified_key"],
          unified(ALPHA, "a1"))


# ---------------------------------------------------------------------------
# Tiny independent reference engine (no import of fhist, no graph library)
# ---------------------------------------------------------------------------

def load_availability(fixtures: Path, campus: str) -> dict[str, str]:
    """(logical id -> last method at the baseline) re-read from NDJSON."""

    manifest = json.loads((fixtures / campus / "manifest.json").read_text(
        encoding="utf-8"))
    last: dict[str, str] = {}
    path = fixtures / campus / manifest["baseline"]["path"]
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            last[row["id"]] = "available" if row["method"] == "PUT" else "deleted"
    return last


def reference_engine(days: list[str], available: dict[tuple[str, str], bool],
                     decisions: list[dict]) -> dict[str, dict[tuple[str, str], set | None]]:
    """Recompute, day by day, which keys group together (and conflicts)."""

    out: dict[str, dict[tuple[str, str], set | None]] = {}
    for day in days:
        active = [d for d in decisions
                  if d["date_start"] <= day < d["date_end"]
                  and available.get((d["left"][0], d["left"][1]))
                  and available.get((d["right"][0], d["right"][1]))]
        keys = sorted(k for k, ok in available.items() if ok)
        # Union via a plain label-propagation loop (no library, no recursion).
        label = {k: i for i, k in enumerate(keys)}
        for d in active:
            if d["kind"] != "same":
                continue
            a, b = (d["left"][0], d["left"][1]), (d["right"][0], d["right"][1])
            la, lb = label[a], label[b]
            if la != lb:
                low = min(la, lb)
                for k in keys:
                    if label[k] in (la, lb):
                        label[k] = low
        groups: dict[int, set] = {}
        for k in keys:
            groups.setdefault(label[k], set()).add(k)
        conflict_groups: set[int] = set()
        for d in active:
            if d["kind"] != "different":
                continue
            a, b = (d["left"][0], d["left"][1]), (d["right"][0], d["right"][1])
            if label[a] == label[b]:
                conflict_groups.add(label[a])
        result: dict[tuple[str, str], set | None] = {}
        for root, members in groups.items():
            result_set = None if root in conflict_groups else members
            for k in members:
                result[k] = result_set
        out[day] = result
    return out


def expected_unified(group: set | None, coord: dict[str, str] | None = None
                     ) -> str | None:
    """The unified key a conflict-free group must carry."""

    if group is None:
        return None
    best = min(group, key=lambda k: (k[0].encode(), TENANT.encode(),
                                     k[1].encode()))
    return unified(best[0], best[1])


def cross_check_engine(repo: Path, build: Path) -> None:
    print("\n== independent reference engine vs CLI, day by day ==")
    fixtures = build / "identity-fixtures"
    coord = {"alpha": ALPHA, "bravo": BRAVO, "charlie": CHARLIE}
    campus_of = {ALPHA: "alpha", BRAVO: "bravo", CHARLIE: "charlie"}
    avail: dict[tuple[str, str], bool] = {}
    for campus, server in coord.items():
        for pid, ok in load_availability(fixtures, campus).items():
            avail[(server, pid)] = ok == "available"
    days = [f"2026-01-{d:02d}" for d in range(1, 32)]

    def decisions(overrides: dict[str, tuple[str, str]],
                  drop: tuple[str, ...] = ()) -> list[dict]:
        base = [
            ("d-ab", "same", (ALPHA, "a1"), (BRAVO, "b1"), "2026-01-01",
             "2026-02-01"),
            ("d-bc", "same", (BRAVO, "b1"), (CHARLIE, "c1"), "2026-01-10",
             "2026-01-20"),
            ("d-ac", "different", (ALPHA, "a1"), (CHARLIE, "c1"),
             "2026-01-15", "2026-01-18"),
        ]
        out = []
        for did, kind, left, right, start, end in base:
            if did in drop:
                continue
            start, end = overrides.get(did, (start, end))
            out.append({"kind": kind, "left": left, "right": right,
                        "date_start": start, "date_end": end})
        return out

    for release, overrides, drop in (
            (1, {}, ()),
            (2, {"d-bc": ("2026-01-12", "2026-01-16")}, ()),
            (3, {"d-bc": ("2026-01-12", "2026-01-16")}, ("d-ac",))):
        expected = reference_engine(days, avail, decisions(overrides, drop))
        mismatches = []
        for day in days:
            for (server, pid), group in expected[day].items():
                want = expected_unified(group, coord)
                got = assign(repo, release, server, pid, day)
                if got.get("unified_key") != want:
                    mismatches.append((day, campus_of[server], pid, want,
                                       got.get("unified_key")))
        check(f"reference engine agrees for release {release} "
              f"({len(days)} days x {len(avail)} keys)", mismatches, [])


def drill_scale(repo: Path, build: Path) -> None:
    print("\n== scale: one small group change on a 200-patient source ==")
    echo = f"{BASE}/echo"
    scale = build / "identity-scale"
    subprocess.run([sys.executable, "-I",
                    str(ROOT / "tools" / "make_identity_scale.py"),
                    str(scale), "--groups", "40", "--per-group", "5"],
                   check=True, capture_output=True)
    sf = build / "identity-scale-sources.json"
    sf.write_text(json.dumps({"sources": [
        {"source_server": ALPHA, "aliases": []},
        {"source_server": echo, "aliases": []}]}, indent=2), encoding="utf-8")
    scale_repo = build / "identity-scale-repo"
    if scale_repo.exists():
        shutil.rmtree(scale_repo)
    run(scale_repo, "init", "--sources-file", str(sf))
    run(scale_repo, "stage", "--manifest",
        str(build / "identity-fixtures" / "alpha" / "manifest.json"))
    run(scale_repo, "advance", "--source-server", ALPHA, "--tenant", TENANT)
    run(scale_repo, "stage", "--manifest", str(scale / "manifest.json"))
    adv = run(scale_repo, "advance", "--source-server", echo,
              "--tenant", TENANT)
    check("scale source advances to 200 patients", adv["frontier"], 50)

    def group_decisions(groups: int) -> list[dict]:
        out = []
        for g in range(groups):
            for m in range(4):
                out.append({
                    "decision_id": f"s{g:03d}-{m}", "revision": 1,
                    "op": "set", "kind": "same",
                    "left": case(echo, f"g{g:03d}p{m:02d}"),
                    "right": case(echo, f"g{g:03d}p{m + 1:02d}"),
                    "interval": {"start": "2026-06-01", "end": "2026-06-11"}})
        return out

    out = publish(scale_repo, 1, None, {"alpha": 10, "echo": 50},
                  "2026-06-01", "2026-06-11", group_decisions(40))
    check("scale release 1 published", out["status"], "published")
    st = run(scale_repo, "identity", "stats", "--release", "1")["stats"]
    check("first release recomputes every member", st["recomputed_keys"], 202)
    check("first release reuses nothing", st["reused_keys"], 0)
    check("200 echo keys form 40 groups of 5",
          assign(scale_repo, 1, echo, "g007p03", "2026-06-05")["unified_key"],
          unified(echo, "g007p00"))

    # Change one group only: split g007 by retracting one of its four edges.
    out = publish(scale_repo, 2, 1, {"alpha": 10, "echo": 50},
                  "2026-06-01", "2026-06-11",
                  [{"decision_id": "s007-1", "revision": 2, "op": "retract"}])
    check("scale release 2 published", out["status"], "published")
    st2 = run(scale_repo, "identity", "stats", "--release", "2")["stats"]
    check("only the touched group is recomputed", st2["recomputed_keys"], 5)
    check("the other 197 keys are reused", st2["reused_keys"], 197)
    check("touched group split into 2 + 3",
          [assign(scale_repo, 2, echo, f"g007p0{i}", "2026-06-05")["unified_key"]
           for i in range(5)],
          [unified(echo, "g007p00"), unified(echo, "g007p00"),
           unified(echo, "g007p02"), unified(echo, "g007p02"),
           unified(echo, "g007p02")])
    # seen_versions counts the new source versions inspected by this release.
    by_source = {e["source_server"]: e for e in st2["sources"]}
    check("scale release 2 saw no new versions (cut unchanged)",
          (by_source[echo]["seen_versions"], by_source[echo]["changed_keys"]),
          (0, 0))
    check("release 7 saw bravo's two new versions when the cut advanced",
          [(e["seen_versions"], e["changed_keys"])
           for e in run(repo, "identity", "stats", "--release", "7"
                        )["stats"]["sources"] if e["source_server"] == BRAVO],
          [(2, 2)])
    # A different unrelated group is untouched and keeps its parent key.
    check("unrelated group untouched by the split",
          assign(scale_repo, 2, echo, "g020p04", "2026-06-05")["unified_key"],
          unified(echo, "g020p00"))
    # The pure alpha key is reused as well.
    check("alpha key reused from parent",
          assign(scale_repo, 2, ALPHA, "a1", "2026-06-05")["unified_key"],
          unified(ALPHA, "a1"))

    # A no-op release reuses the whole table.
    out = publish(scale_repo, 3, 2, {"alpha": 10, "echo": 50},
                  "2026-06-01", "2026-06-11", [])
    st3 = run(scale_repo, "identity", "stats", "--release", "3")["stats"]
    check("no-op release reuses every key",
          (st3["recomputed_keys"], st3["reused_keys"]), (0, 202))
    check("no-op release changes no day", st3["days_changed"], 0)

    # Persisted day statistics are on disk, not only in memory.
    db = sqlite3.connect(scale_repo / "meta" / "history.sqlite3")
    rows = db.execute("SELECT COUNT(*) FROM idr_day WHERE release_no=2"
                      ).fetchone()[0]
    db.close()
    check("per-day stats persisted for every business day", rows, 10)


def main() -> int:
    build = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build"
    build.mkdir(parents=True, exist_ok=True)
    repo = init_repo(build)
    drill_headline(repo, build)
    drill_status_runs(repo)
    drill_release2(repo)
    drill_retract(repo)
    drill_parallel_and_paths(repo)
    drill_outage_and_restore(repo, build)
    drill_refresh_counts(repo, build)
    drill_rejections(repo, build)
    drill_retry_and_race(repo, build)
    drill_query_snapshot(repo, build)
    cross_check_engine(repo, build)
    drill_scale(repo, build)

    print("\n== summary ==")
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
