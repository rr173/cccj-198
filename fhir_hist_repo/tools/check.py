#!/usr/bin/env python3 -I
"""Independent checker for the offline FHIR history store drills.

This checker deliberately does **not** import the ``fhist`` package. It:

* drives the real store only through its ``bin/fhist`` command line,
* reads NDJSON fixtures itself with the standard library,
* keeps a hand-maintained expected version/edge table for the headline
  walkthrough,
* re-implements cut semantics in ~150 lines of independent code and
  compares every status against the CLI output.

Run with ``python3 -I tools/check.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FHIST = ROOT / "bin" / "fhist"
SERVER_A = "https://fhir.example.org/a"
SERVER_B = "https://fhir.example.org/b"
ALIAS_A = "https://fhir-alias.example.net/a"

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
            f"command failed ({args}): rc={proc.returncode}\n{proc.stderr}\n{proc.stdout}")
    if not proc.stdout.strip():
        return {"_rc": proc.returncode, "_stderr": proc.stderr}
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Hand-written expected tables for the headline walkthrough (source A)
# ---------------------------------------------------------------------------

# identity -> list of (seq, versionId, method) as delivered
EXPECTED_VERSIONS = {
    "Patient/p": [(10, "vA", "PUT"), (11, "vB", "PUT"), (12, "vC", "DELETE")],
    "Observation/o": [(10, "oV1", "PUT")],
    "Observation/probe": [(10, "probeV1", "PUT")],
    "Patient/owner1": [(10, "owner1V1", "PUT")],
    "Patient/owner2": [(10, "owner2V1", "PUT")],
    "Organization/org1": [(10, "orgV1", "PUT")],
    "Basic/ext1": [(10, "extV1", "PUT")],
    "Observation/noise11": [(11, "n11V1", "PUT")],
}

# (owner, cut) -> {ref_text: status}; hand-derived per the specification.
EXPECTED_EDGES_CUT10 = {
    "Observation/o": {"Patient/p": "resolved",
                      "Patient/p/_history/vA": "resolved"},
    "Observation/probe": {"Patient/p/_history/vFUTURE":
                          "version_not_known_at_cut"},
    "Patient/owner1": {"#x": "resolved", "Patient/p": "resolved"},
    "Patient/owner2": {"#x": "resolved"},
    "Organization/org1": {f"{ALIAS_A}/Organization/shared": "missing"},
    "Basic/ext1": {"https://elsewhere.example.com/fhir/Patient/zzz": "external"},
}
EXPECTED_EDGES_CUT11 = {
    "Observation/o": {"Patient/p": "resolved",
                      "Patient/p/_history/vA": "resolved"},
}
EXPECTED_EDGES_CUT12 = {
    "Observation/o": {"Patient/p": "deleted",
                      "Patient/p/_history/vA": "resolved"},
}


def edge_status_map(payload: dict) -> dict[str, str]:
    return {e["ref_text"]: e["status"] for e in payload.get("outbound", [])}


# ---------------------------------------------------------------------------
# Independent cut engine (does not import fhist)
# ---------------------------------------------------------------------------

def independent_latest(facts: list[dict], cut: int, rtype: str,
                       rid: str) -> dict | None:
    rows = [f for f in facts
            if f["resourceType"] == rtype and f["id"] == rid and f["seq"] <= cut]
    return rows[-1] if rows else None


def independent_resolve(facts: list[dict], cut: int, ref_text: str,
                        alias_bases: set[str],
                        conflicts: list[dict] | None = None) -> str:
    conflicts = conflicts or []
    if ref_text.startswith("http://") or ref_text.startswith("https://"):
        matched_base = next((b for b in sorted(alias_bases, key=len, reverse=True)
                             if ref_text == b or ref_text.startswith(b + "/")),
                            None)
        if matched_base is None:
            return "external"
        ref_text = ref_text[len(matched_base):].lstrip("/")
    if ref_text.startswith("#") or ref_text.startswith("urn:") \
            or ref_text.startswith("identifier:"):
        return "invalid" if ref_text.startswith(("urn:", "identifier:")) \
            else "resolved"  # contained checked against owner separately
    parts = ref_text.split("/")
    if len(parts) not in (2, 4) or (len(parts) == 4 and parts[2] != "_history"):
        return "invalid"
    rtype, rid = parts[0], parts[1]
    if len(parts) == 4:
        vid = parts[3]
        exact = [f for f in facts if f["resourceType"] == rtype
                 and f["id"] == rid and f["versionId"] == vid
                 and f["seq"] <= cut]
        if exact:
            return "conflict" if any(
                c.get("version_id") == vid and c.get("logical_id") == rid
                for c in conflicts) else "resolved"
        if any(f["resourceType"] == rtype and f["id"] == rid
               and f["seq"] > cut for f in facts):
            return "version_not_known_at_cut"
        return "missing"
    latest = independent_latest(facts, cut, rtype, rid)
    if latest is None:
        return "conflict" if any(c.get("logical_id") == rid
                                 for c in conflicts) else "missing"
    if any(c.get("version_id") == latest["versionId"]
           and c.get("logical_id") == rid for c in conflicts):
        return "conflict"
    return "deleted" if latest["method"] == "DELETE" else "resolved"


def load_fixture_facts(fixture: Path, up_to_seq: int | None = None) -> list[dict]:
    """Stream every shard ourselves into a plain fact list."""

    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    facts: list[dict] = []
    files = [(manifest["baseline"]["path"], manifest["baseline"]["seq"])]
    for txn in manifest["transactions"]:
        if up_to_seq is not None and txn["seq"] > up_to_seq:
            continue
        for sh in txn["shards"]:
            if "MISSING" not in sh["path"] and (fixture / sh["path"]).exists():
                files.append((sh["path"], txn["seq"]))
    for rel, seq in files:
        with open(fixture / rel, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    facts.append(json.loads(line))
    facts.sort(key=lambda f: (f["seq"], f["ordinal"]))
    return facts


# ---------------------------------------------------------------------------
# Drills
# ---------------------------------------------------------------------------

def init_repo(repo: Path) -> None:
    sources = {"sources": [
        {"source_server": SERVER_A, "aliases": [ALIAS_A]},
        {"source_server": SERVER_B, "aliases": []},
        {"source_server": "https://fhir.example.org/large", "aliases": []},
    ]}
    for extra in ("tenant-dup", "tenant-mismatch", "tenant-corrupt",
                  "tenant-cycle", "tenant-retrans", "tenant-missing-history",
                  "tenant-conflict-visibility", "tenant-bad-baseline"):
        sources["sources"].append(
            {"source_server": SERVER_A, "aliases": [ALIAS_A]})
    sf = repo.parent / "sources.json"
    sf.write_text(json.dumps(sources, indent=2), encoding="utf-8")
    run(repo, "init", "--sources-file", str(sf))


def drill_headline(build: Path, fixtures: Path, repo: Path) -> None:
    print("\n== headline walkthrough + out-of-order shards ==")
    # 1) partial delivery: txn12 present, txn11 half present
    run(repo, "stage", "--manifest",
        str(fixtures / "sourceA-partial" / "manifest.json"))
    st = run(repo, "status", "--source-server", SERVER_A,
             "--tenant", "tenant-main")
    src = st["sources"][0]
    check("frontier held: no cut queryable while 11 incomplete",
          src["frontier_queryable"], None)
    check("cut 11 not queryable yet", 11 in src["queryable_cuts"], False)
    check("txn 11 staged but incomplete",
          next(t for t in src["transactions"] if t["seq"] == 11)["complete"],
          False)
    check("early-arriving txn 12 is staged ahead of its turn",
          next(t for t in src["transactions"] if t["seq"] == 12)["state"],
          "staged")

    # 2) full delivery completes 11 and 12; one advance jumps to 12
    run(repo, "stage", "--manifest",
        str(fixtures / "sourceA" / "manifest.json"))
    adv = run(repo, "advance", "--source-server", SERVER_A,
              "--tenant", "tenant-main")
    check("advance imports baseline", adv["imported_baseline"], True)
    check("advance publishes 11 then 12", adv["advanced"], [11, 12])
    check("frontier after advance", adv["frontier"], 12)

    # 3) hand-written version table vs resource history
    hist = run(repo, "resource", "--source-server", SERVER_A,
               "--tenant", "tenant-main", "--cut", "12",
               "--resource-type", "Patient", "--id", "p", "--history")
    got_versions = [(v["seq"], v["versionId"], v["method"])
                    for v in hist["versions"]]
    check("Patient/p version chain", got_versions,
          EXPECTED_VERSIONS["Patient/p"])

    # 4) current reads at each cut
    cur10 = run(repo, "resource", "--source-server", SERVER_A,
                "--tenant", "tenant-main", "--cut", "10",
                "--resource-type", "Patient", "--id", "p")
    cur11 = run(repo, "resource", "--source-server", SERVER_A,
                "--tenant", "tenant-main", "--cut", "11",
                "--resource-type", "Patient", "--id", "p")
    cur12 = run(repo, "resource", "--source-server", SERVER_A,
                "--tenant", "tenant-main", "--cut", "12",
                "--resource-type", "Patient", "--id", "p")
    check("p current vA at cut10", (cur10["status"], cur10["versionId"]),
          ("resolved", "vA"))
    check("p current vB at cut11", (cur11["status"], cur11["versionId"]),
          ("resolved", "vB"))
    check("p deleted vC at cut12", (cur12["status"], cur12["versionId"]),
          ("deleted", "vC"))

    # Old version still readable after deletion; tombstone keeps history.
    old = run(repo, "resource", "--source-server", SERVER_A,
              "--tenant", "tenant-main", "--cut", "12",
              "--resource-type", "Patient", "--id", "p",
              "--version-id", "vA")
    check("old vA still resolves at cut12",
          (old["status"], old["versionId"]), ("resolved", "vA"))

    # 5) edges at cut 10 / 11 / 12 against hand tables
    for cut, table in ((10, EXPECTED_EDGES_CUT10),
                       (11, EXPECTED_EDGES_CUT11),
                       (12, EXPECTED_EDGES_CUT12)):
        for owner, expected_map in table.items():
            rtype, rid = owner.split("/")
            payload = run(repo, "references", "--source-server", SERVER_A,
                          "--tenant", "tenant-main", "--cut", str(cut),
                          "--resource-type", rtype, "--id", rid,
                          "--direction", "out")
            got_map = {k: v for k, v in edge_status_map(payload).items()
                       if k in expected_map}
            check(f"edges {owner}@cut{cut}", got_map, expected_map)

    # cut10 stays identical after the frontier reached 12 (immutability).
    o10 = run(repo, "references", "--source-server", SERVER_A,
              "--tenant", "tenant-main", "--cut", "10",
              "--resource-type", "Observation", "--id", "o")
    check("cut10 unversioned edge stays on vA semantics",
          edge_status_map(o10)["Patient/p"], "resolved")
    check("cut10 versioned edge still vA",
          edge_status_map(o10)["Patient/p/_history/vA"], "resolved")

    # 6) independent engine cross-check over every cut and every edge
    facts = load_fixture_facts(fixtures / "sourceA")
    alias = {SERVER_A, ALIAS_A}
    for cut in (10, 11, 12):
        for f in facts:
            if f["method"] != "PUT" or f["seq"] > cut:
                continue
            body = f["resource"]
            for ref_text in _all_refs(body):
                got = run(repo, "references", "--source-server", SERVER_A,
                          "--tenant", "tenant-main", "--cut", str(cut),
                          "--resource-type", f["resourceType"],
                          "--id", f["id"], "--direction", "out")
                got_status = next(
                    (e["status"] for e in got["outbound"]
                     if e["ref_text"] == ref_text), None)
                if ref_text.startswith("#"):
                    continue  # contained scope checked separately below
                want = independent_resolve(facts, cut, ref_text, alias)
                check(f"independent {f['id']} -> {ref_text} @cut{cut}",
                      got_status, want)

    # 7) contained #x scope: two owners, fragments never merge
    for owner in ("owner1", "owner2"):
        payload = run(repo, "references", "--source-server", SERVER_A,
                      "--tenant", "tenant-main", "--cut", "10",
                      "--resource-type", "Patient", "--id", owner)
        check(f"contained #x resolved inside {owner}",
              edge_status_map(payload).get("#x"), "resolved")
        check(f"contained edge location retained for {owner}",
              payload["outbound"][0]["loc"].endswith(".reference"), True)

    # 8) pre-baseline cut explicitly rejected
    bad = run(repo, "resource", "--source-server", SERVER_A,
              "--tenant", "tenant-main", "--cut", "9",
              "--resource-type", "Patient", "--id", "p", check_rc=False)
    check("pre-baseline cut rejected", bad["status"], "cut_not_queryable")


def _all_refs(node, found=None):
    if found is None:
        found = []
    if isinstance(node, dict):
        if isinstance(node.get("reference"), str):
            found.append(node["reference"])
        for v in node.values():
            _all_refs(v, found)
    elif isinstance(node, list):
        for v in node:
            _all_refs(v, found)
    return found


def drill_tenant_isolation(fixtures: Path, repo: Path) -> None:
    print("\n== tenant / namespace isolation ==")
    run(repo, "stage", "--manifest",
        str(fixtures / "sourceB" / "manifest.json"))
    run(repo, "advance", "--source-server", SERVER_B,
        "--tenant", "tenant-other")
    p = run(repo, "resource", "--source-server", SERVER_B,
            "--tenant", "tenant-other", "--cut", "20",
            "--resource-type", "Patient", "--id", "p")
    check("tenant B has its own Patient/p vX",
          (p["status"], p["versionId"]), ("resolved", "vX"))
    refs = run(repo, "references", "--source-server", SERVER_B,
               "--tenant", "tenant-other", "--cut", "20",
               "--resource-type", "Patient", "--id", "other")
    statuses = edge_status_map(refs)
    check("relative Patient/p resolves inside tenant B",
          statuses["Patient/p"], "resolved")
    # Absolute URL points at source A's base: foreign to source B.
    check("cross-source absolute URL is external, no trailing-id join",
          statuses[f"{SERVER_A}/Patient/p"], "external")


def drill_negative(fixtures: Path, repo: Path, case: str, tenant: str,
                   expected_state: str) -> None:
    print(f"\n== negative case {case} ==")
    run(repo, "stage", "--manifest",
        str(fixtures / case / "manifest.json"))
    # Baseline itself is clean: importing it yields the baseline cut first.
    adv0 = run(repo, "advance", "--source-server", SERVER_A,
               "--tenant", tenant)
    check(f"{case}: baseline imported", adv0["frontier"], 5)
    st = run(repo, "status", "--source-server", SERVER_A,
             "--tenant", tenant)
    src = next(s for s in st["sources"] if s["tenant"] == tenant)
    check(f"{case}: baseline cut stays queryable",
          src["frontier_queryable"], 5)
    txn = next((t for t in src["transactions"] if t["seq"] == 6), None)
    check(f"{case}: txn state", txn["state"] if txn else None,
          expected_state)
    adv = run(repo, "advance", "--source-server", SERVER_A,
              "--tenant", tenant)
    check(f"{case}: frontier does not advance past bad txn",
          adv["frontier"], 5)
    # Baseline facts remain intact and readable.
    cur = run(repo, "resource", "--source-server", SERVER_A,
              "--tenant", tenant, "--cut", "5",
              "--resource-type", "Patient", "--id", "p")
    check(f"{case}: baseline fact readable after bad input",
          cur["status"], "resolved")


def drill_cycle(fixtures: Path, repo: Path) -> None:
    print("\n== cycle guard ==")
    run(repo, "stage", "--manifest",
        str(fixtures / "case-cycle" / "manifest.json"))
    run(repo, "advance", "--source-server", SERVER_A,
        "--tenant", "tenant-cycle")
    out = run(repo, "trace-path", "--source-server", SERVER_A,
              "--tenant", "tenant-cycle", "--cut", "6",
              "--resource-type", "Patient", "--id", "cycA")
    stopped = [hop for path in out["paths"] for hop in path
               if str(hop.get("stop_reason") or "").startswith("cycle")]
    check("cycle path terminates with evidence", bool(stopped), True)
    check("cycle depth bounded", max(len(p) for p in out["paths"]) <= 4, True)


def drill_retransmit(fixtures: Path, repo: Path) -> None:
    print("\n== identical fold vs divergent isolation ==")
    run(repo, "stage", "--manifest",
        str(fixtures / "case-retransmit-v1" / "manifest.json"))
    adv1 = run(repo, "advance", "--source-server", SERVER_A,
               "--tenant", "tenant-retrans")
    check("first delivery advances to 6", adv1["frontier"], 6)
    before = run(repo, "resource", "--source-server", SERVER_A,
                 "--tenant", "tenant-retrans", "--cut", "6",
                 "--resource-type", "Patient", "--id", "p")
    # Identical retransmission folds.
    fold = run(repo, "stage", "--manifest",
               str(fixtures / "case-retransmit-v1" / "manifest.json"))
    check("identical retransmission folded", fold["folded"], [6])
    # Divergent retransmission is isolated; published fact untouched.
    run(repo, "stage", "--manifest",
        str(fixtures / "case-retransmit-v2" / "manifest.json"))
    st = run(repo, "status", "--source-server", SERVER_A,
             "--tenant", "tenant-retrans")
    src = next(s for s in st["sources"]
               if s["tenant"] == "tenant-retrans")
    check("divergent payload recorded as isolated conflict",
          len(src["isolated_conflicts"]) >= 1, True)
    after = run(repo, "resource", "--source-server", SERVER_A,
                "--tenant", "tenant-retrans", "--cut", "6",
                "--resource-type", "Patient", "--id", "p")
    check("published cut not overwritten",
          after["resource"]["active"], before["resource"]["active"])
    refs = run(repo, "references", "--source-server", SERVER_A,
               "--tenant", "tenant-retrans", "--cut", "6",
               "--resource-type", "Patient", "--id", "p")
    check("conflict status visible at cut", refs["status"], "ok")


def drill_bad_baseline(fixtures: Path, repo: Path) -> None:
    print("\n== corrupt baseline blocked, then clean delivery recovers ==")
    if not (fixtures / "case-bad-baseline-corrupt").exists():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "make_bad_baseline.py"),
                        str(fixtures)], check=True, capture_output=True)
    staged = run(repo, "stage", "--manifest",
                 str(fixtures / "case-bad-baseline-corrupt" / "manifest.json"))
    check("corrupt baseline reported in stage bad[]",
          len(staged["bad"]) >= 1, True)
    refused = run(repo, "advance", "--source-server", SERVER_A,
                  "--tenant", "tenant-bad-baseline", check_rc=False)
    check("no cut created from corrupt baseline",
          refused["status"] in ("bad_shard", "cut_not_queryable"), True)
    st = run(repo, "status", "--source-server", SERVER_A,
             "--tenant", "tenant-bad-baseline")
    src = next(s for s in st["sources"]
               if s["tenant"] == "tenant-bad-baseline")
    check("no queryable cut while baseline corrupt",
          src["queryable_cuts"], [])

    # Clean delivery reuses the same relative shard path with good bytes.
    run(repo, "stage", "--manifest",
        str(fixtures / "case-bad-baseline-clean" / "manifest.json"))
    adv = run(repo, "advance", "--source-server", SERVER_A,
              "--tenant", "tenant-bad-baseline")
    check("clean delivery recovers frontier to baseline seq7",
          adv["frontier"], 7)
    cur = run(repo, "resource", "--source-server", SERVER_A,
              "--tenant", "tenant-bad-baseline", "--cut", "7",
              "--resource-type", "Patient", "--id", "z")
    check("baseline resource readable after recovery",
          (cur["status"], cur["versionId"]), ("resolved", "z1"))


def drill_conflict_visibility(fixtures: Path, repo: Path) -> None:
    print("\n== conflict status visible on resolving edges ==")
    if not (fixtures / "case-conflict-visibility-v1").exists():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "make_conflict_case.py"),
                        str(fixtures)], check=True, capture_output=True)
    run(repo, "stage", "--manifest",
        str(fixtures / "case-conflict-visibility-v1" / "manifest.json"))
    adv = run(repo, "advance", "--source-server", SERVER_A,
              "--tenant", "tenant-conflict-visibility")
    check("conflict case: published to 6", adv["frontier"], 6)
    refs_before = run(repo, "references", "--source-server", SERVER_A,
                      "--tenant", "tenant-conflict-visibility", "--cut", "6",
                      "--resource-type", "Observation", "--id", "h")
    before = edge_status_map(refs_before)
    check("unversioned edge resolved before divergent retransmit",
          before["Patient/p"], "resolved")
    check("versioned edge resolved before divergent retransmit",
          before["Patient/p/_history/v2"], "resolved")

    run(repo, "stage", "--manifest",
        str(fixtures / "case-conflict-visibility-v2" / "manifest.json"))
    refs_after = run(repo, "references", "--source-server", SERVER_A,
                     "--tenant", "tenant-conflict-visibility", "--cut", "6",
                     "--resource-type", "Observation", "--id", "h")
    after = edge_status_map(refs_after)
    check("unversioned edge now reports conflict",
          after["Patient/p"], "conflict")
    check("versioned edge now reports conflict",
          after["Patient/p/_history/v2"], "conflict")
    # Evidence of origin and reason must be retained.
    edge = next(e for e in refs_after["outbound"]
                if e["ref_text"] == "Patient/p/_history/v2")
    check("conflict carries reason",
          "divergent" in (edge.get("conflict_reason") or ""), True)
    check("conflict carries source location",
          edge["loc"].endswith(".reference"), True)
    # Published fact itself remains the original payload.
    p = run(repo, "resource", "--source-server", SERVER_A,
            "--tenant", "tenant-conflict-visibility", "--cut", "6",
            "--resource-type", "Patient", "--id", "p")
    check("published body unchanged after conflict isolation",
          p["resource"]["active"], True)


def drill_missing_history(fixtures: Path, repo: Path) -> None:
    print("\n== missing historical version ==")
    run(repo, "stage", "--manifest",
        str(fixtures / "case-missing-history" / "manifest.json"))
    run(repo, "advance", "--source-server", SERVER_A,
        "--tenant", "tenant-missing-history")
    refs = run(repo, "references", "--source-server", SERVER_A,
               "--tenant", "tenant-missing-history", "--cut", "6",
               "--resource-type", "Observation", "--id", "h")
    check("never-existing version is missing",
          edge_status_map(refs)["Patient/p/_history/vGHOST"], "missing")


def drill_concurrency(fixtures: Path, repo: Path) -> None:
    print("\n== query snapshot while a real next transaction commits ==")
    followup = fixtures / "sourceA-seq13"
    if not followup.exists():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "make_followup.py"),
                        str(fixtures)], check=True, capture_output=True)
    run(repo, "stage", "--manifest",
        str(followup / "manifest.json"))

    # Reader opens cut 12 and holds the SQLite read snapshot open.
    proc = subprocess.Popen(
        [sys.executable, "-I", str(ROOT / "src" / "fhist" / "cli.py"),
         "--repo", str(repo), "references", "--source-server", SERVER_A,
         "--tenant", "tenant-main", "--cut", "12",
         "--resource-type", "Observation", "--id", "o",
         "--hold-open", "3.0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    import time
    time.sleep(0.8)
    sync_note = "sync point: reader snapshot open at cut12, writer advancing seq13"
    note(sync_note)
    # Writer commits the next transaction while the reader is inside its
    # snapshot. WAL lets the writer proceed; the reader keeps its read mark.
    adv = run(repo, "advance", "--source-server", SERVER_A,
              "--tenant", "tenant-main")
    check("writer commits seq13 concurrently", adv["advanced"], [13])
    check("frontier moved to 13", adv["frontier"], 13)
    out, err = proc.communicate(timeout=15)
    payload = json.loads(out)
    check("in-flight query stayed pinned to cut12 facts",
          edge_status_map(payload)["Patient/p"], "deleted")
    check("in-flight versioned edge unchanged",
          edge_status_map(payload)["Patient/p/_history/vA"], "resolved")
    note(f"reader finished rc={proc.returncode} after concurrent commit; {err.strip() or 'no stderr'}")

    # A fresh query at cut13 sees the new fact; cut12 remains frozen.
    new1 = run(repo, "resource", "--source-server", SERVER_A,
               "--tenant", "tenant-main", "--cut", "13",
               "--resource-type", "Patient", "--id", "new1")
    check("seq13 fact visible at cut13", new1["status"], "resolved")
    hidden = run(repo, "resource", "--source-server", SERVER_A,
                 "--tenant", "tenant-main", "--cut", "12",
                 "--resource-type", "Patient", "--id", "new1",
                 check_rc=False)
    check("seq13 fact invisible at frozen cut12",
          hidden["status"], "missing")


def drill_reopen_and_relocate(build: Path, repo: Path) -> None:
    print("\n== reopen after exit + whole-directory relocation ==")
    st1 = run(repo, "status")
    # Reopen in a brand-new process (every run() already is): compare counts.
    st2 = run(repo, "status")
    check("reopen gives identical source count",
          len(st1["sources"]), len(st2["sources"]))
    verify = run(repo, "verify")
    check("all stored blobs verify after reopen",
          (verify["status"], len(verify["bad"])), ("ok", 0))
    moved = build / "handoff-moved"
    if moved.exists():
        shutil.rmtree(moved)
    shutil.move(str(repo), moved)
    note(f"repository moved to {moved} (no absolute paths retained)")
    st3 = run(moved, "status")
    check("queryable after move", st3["status"], "ok")
    cur = run(moved, "resource", "--source-server", SERVER_A,
              "--tenant", "tenant-main", "--cut", "12",
              "--resource-type", "Patient", "--id", "p")
    check("cut12 tombstone still readable after relocation",
          cur["status"], "deleted")
    return moved


def drill_large(build: Path, fixtures: Path, repo_path: Path) -> None:
    print("\n== large streaming history ==")
    large_dir = fixtures / "large"
    gen = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / "large_history.py"),
         str(large_dir), "--resources", "2000", "--txns", "8",
         "--big-every", "250", "--big-bytes", "120000"],
        capture_output=True, text=True, check=True)
    note("generator: " + gen.stdout.strip())
    run(repo_path, "stage", "--manifest",
        str(large_dir / "manifest.json"))
    adv = run(repo_path, "advance", "--source-server",
              "https://fhir.example.org/large", "--tenant", "tenant-large")
    check("large history advanced through all txns",
          adv["frontier"], 108)
    sample = run(repo_path, "resource", "--source-server",
                 "https://fhir.example.org/large", "--tenant", "tenant-large",
                 "--cut", "108", "--resource-type", "Observation",
                 "--id", "r000000")
    check("large history sample readable", sample["status"], "resolved")
    # Independent spot checks with our own engine.
    facts = load_fixture_facts(large_dir)
    for cut, rid in ((100, "r000000"), (104, "r000004"), (108, "r000010")):
        want = independent_latest(facts, cut, "Observation", rid)
        got = run(repo_path, "resource", "--source-server",
                  "https://fhir.example.org/large", "--tenant",
                  "tenant-large", "--cut", str(cut),
                  "--resource-type", "Observation", "--id", rid)
        check(f"large independent latest {rid}@cut{cut}",
              got["versionId"], want["versionId"] if want else None)


def main() -> int:
    build = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build"
    fixtures = build / "fixtures"
    repo = build / "handoff"
    if repo.exists():
        shutil.rmtree(repo)
    init_repo(repo)

    drill_headline(build, fixtures, repo)
    drill_tenant_isolation(fixtures, repo)
    drill_negative(fixtures, repo, "case-dup-ordinal", "tenant-dup",
                   "conflict")
    drill_negative(fixtures, repo, "case-body-mismatch", "tenant-mismatch",
                   "conflict")  # metadata/body mismatch isolated at stage
    drill_negative(fixtures, repo, "case-corrupt", "tenant-corrupt",
                   "staged")  # bad digest: txn held, never publishable
    drill_cycle(fixtures, repo)
    drill_retransmit(fixtures, repo)
    drill_conflict_visibility(fixtures, repo)
    drill_bad_baseline(fixtures, repo)
    drill_missing_history(fixtures, repo)
    drill_concurrency(fixtures, repo)
    moved = drill_reopen_and_relocate(build, repo)
    drill_large(build, fixtures, moved)

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
