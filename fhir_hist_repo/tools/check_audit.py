#!/usr/bin/env python3 -I
"""Independent checker for the profile-compliance audit drills.

Like ``check.py`` this checker deliberately does **not** import the ``fhist``
package. It drives the real store only through its CLI and compares every
report against a hand-maintained expected finding table that was derived
from the fixture semantics alone — not from the profile parsing/matching
implementation under test.

Run with ``python3 -I tools/check_audit.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "fhist" / "cli.py"
SERVER_IG = "https://fhir.example.org/ig"
TENANT = "tenant-ig"

CANON = "http://synth.example.org"
SD_PATIENT = f"{CANON}/StructureDefinition/synth-patient"
SD_OBS = f"{CANON}/StructureDefinition/synth-observation"
SD_OBS_STRICT = f"{CANON}/StructureDefinition/synth-observation-strict"
SD_DUP = f"{CANON}/StructureDefinition/dup-sd"

FAILURES: list[str] = []


def check(name: str, got, expected) -> None:
    if got == expected:
        print(f"  PASS {name}")
    else:
        FAILURES.append(f"{name}: expected {expected!r}, got {got!r}")
        print(f"  FAIL {name}: expected {expected!r}, got {got!r}")


def run(repo: Path, *args: str, expect_rc: int = 0) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), "--repo", str(repo), *args],
        capture_output=True, text=True,
    )
    if proc.returncode != expect_rc:
        raise AssertionError(
            f"command {args} returned rc={proc.returncode}, expected "
            f"{expect_rc}\n{proc.stderr}\n{proc.stdout}")
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Hand-maintained expected tables (derived from the fixtures by hand)
# ---------------------------------------------------------------------------

# cut -> identity -> (result, sorted finding codes)
EXPECTED_A1 = {
    30: {
        "Observation/o-bad": ("fail", ["choice_type_not_allowed",
                                       "fixed_mismatch", "pattern_mismatch",
                                       "slice_min", "slice_unmatched"]),
        "Observation/o-ok": ("pass", []),
        "Observation/o-strict": ("unsupported",
                                 ["unsupported_fhirpath_constraint",
                                  "unsupported_target_profile",
                                  "unsupported_valueset_filter"]),
        "Observation/o-typed-bad": ("fail", ["slice_unmatched"]),
        "Observation/o-typed-ok": ("pass", []),
        "Observation/o-wrongtype": ("profile_type_mismatch",
                                    ["profile_type_mismatch"]),
        "Patient/p-ambig": ("profile_unknown", ["profile_unknown"]),
        "Patient/p-bad": ("fail", ["code_not_in_valueset", "fixed_mismatch",
                                   "min_cardinality", "slice_min"]),
        "Patient/p-del": ("pass", []),
        "Patient/p-excl": ("fail", ["code_not_in_valueset"]),
        "Patient/p-names": ("fail", ["max_cardinality"]),
        "Patient/p-noprof": ("no_profile", []),
        "Patient/p-ok": ("pass", []),
        "Patient/p-unknown": ("profile_unknown", ["profile_unknown"]),
    },
    31: {},  # filled below: same as 30 except p-ok
    32: {},  # same as 31 minus p-del (deleted)
}
EXPECTED_A1[31] = dict(EXPECTED_A1[30])
EXPECTED_A1[31]["Patient/p-ok"] = ("fail", ["code_not_in_valueset"])
EXPECTED_A1[32] = {k: v for k, v in EXPECTED_A1[31].items()
                   if k != "Patient/p-del"}

EXPECTED_A1_SUMMARY = {
    "30": {"pass": 4, "fail": 5, "unsupported": 1, "no_profile": 1,
           "profile_unknown": 2, "profile_ambiguous": 0,
           "profile_type_mismatch": 1, "deleted": 0},
    "31": {"pass": 3, "fail": 6, "unsupported": 1, "no_profile": 1,
           "profile_unknown": 2, "profile_ambiguous": 0,
           "profile_type_mismatch": 1, "deleted": 0},
    "32": {"pass": 2, "fail": 6, "unsupported": 1, "no_profile": 1,
           "profile_unknown": 2, "profile_ambiguous": 0,
           "profile_type_mismatch": 1, "deleted": 1},
}

# Guideline-version contrast at cut 31: p-excl passes only under 2.0.0;
# p-ok v2 fails under both (content change, not guideline change).
EXPECTED_A2_CUT31 = {
    "Patient/p-excl": ("pass", []),
    "Patient/p-ok": ("fail", ["code_not_in_valueset"]),
    "Patient/p-bad": ("fail", ["code_not_in_valueset", "fixed_mismatch",
                               "min_cardinality", "slice_min"]),
    "Patient/p-names": ("fail", ["max_cardinality"]),
    "Observation/o-strict": ("unsupported",
                             ["unsupported_fhirpath_constraint",
                              "unsupported_target_profile",
                              "unsupported_valueset_filter"]),
    "Observation/o-typed-ok": ("pass", []),
    "Observation/o-typed-bad": ("fail", ["slice_unmatched"]),
    "Observation/o-wrongtype": ("profile_type_mismatch",
                                ["profile_type_mismatch"]),
}

EXPECTED_ROOT_CUT30 = {  # explicit root: Patients evaluated, Observations not
    "pass": 5, "fail": 3, "no_profile": 6,
}

EXPECTED_AMBIG_CUT30 = {
    "profile_ambiguous": 1, "no_profile": 1, "profile_unknown": 12,
}


def resource_map(report: dict) -> dict:
    out = {}
    for r in report["resources"]:
        key = f"{r['identity']['resourceType']}/{r['identity']['id']}"
        out.setdefault(r["cut_seq"], {})[key] = (
            r["result"], sorted(f["code"] for f in r["findings"]))
    return out


def check_expected_table(name: str, report: dict, table: dict) -> None:
    got = resource_map(report)
    for cut, expected in table.items():
        check(f"{name}: resources at cut {cut}", got.get(cut, {}), expected)


# ---------------------------------------------------------------------------
# Drills
# ---------------------------------------------------------------------------

def drill_registration(repo: Path, packages: Path) -> dict:
    print("\n== profile package registration ==")
    digests = {}
    for dirname, expect in (
            ("synth-core-1.0.0", "registered"),
            ("synth-ig-1.0.0", "registered"),
            ("synth-dup-a-1.0.0", "registered"),
            ("synth-dup-b-1.0.0", "registered"),
            ("synth-ambig-1.0.0", "registered")):
        out = run(repo, "profile", "register",
                  "--package-dir", str(packages / dirname))
        check(f"register {dirname}", out["status"], expect)
        digests[out["name"] + "@" + out["version"]] = out["package_digest"]

    # Identical re-registration folds onto the stored package.
    out = run(repo, "profile", "register",
              "--package-dir", str(packages / "synth-ig-1.0.0"))
    check("re-register identical package folds", out["status"],
          "already_registered")

    # Negative registrations must fail loudly with stable statuses.
    negatives = [
        ("bad-missing-dep", "profile_dependency_error"),
        ("bad-self-dep", "profile_dependency_error"),
        ("bad-canonical-clash", "profile_conflict"),
        ("synth-ig-1.0.0-alt", "profile_conflict"),
    ]
    for dirname, status in negatives:
        out = run(repo, "profile", "register",
                  "--package-dir", str(packages / dirname), expect_rc=2)
        check(f"reject {dirname}", out["status"], status)

    listed = run(repo, "profile", "list")
    check("list shows 5 packages", len(listed["packages"]), 5)
    shown = run(repo, "profile", "show", "--package", "synth-ig:1.0.0")
    check("show by name:version", shown["name"], "synth-ig")
    check("show lists canonicals", shown["canonical_count"], 6)
    check("show lists files incl. package.json",
          any(f["relpath"] == "package.json" for f in shown["files"]), True)
    return digests


def drill_interrupted_audit(repo: Path) -> str:
    print("\n== interrupted run, resume, idempotent rerun ==")
    args = ("audit", "run", "--source-server", SERVER_IG, "--tenant", TENANT,
            "--cut", "30", "--cut", "31", "--cut", "32",
            "--package", "synth-ig:1.0.0")
    first = run(repo, *args, "--max-evaluations", "3", expect_rc=3)
    check("limited run interrupted with exit 3", first["status"],
          "interrupted")
    check("interrupted run reports progress",
          first["progress"]["members_recorded"], 3)
    check("no cut finished yet", first["progress"]["cuts_done"], [])
    audit_id = first["audit_id"]

    st = run(repo, "audit", "status", "--audit-id", audit_id)
    check("status shows running", st["state"], "running")

    # A report over a running audit is still rebuildable, marked running.
    partial = run(repo, "audit", "report", "--audit-id", audit_id)
    check("partial report marked running", partial["status"], "running")

    resumed = run(repo, *args)
    check("resumed run completes", resumed["status"], "complete")
    check("resumed flag set", resumed["resumed"], True)
    check("same audit id", resumed["audit_id"], audit_id)

    again = run(repo, *args)
    check("identical request folds", again["status"], "already_complete")
    check("folded run reuses stored summary", again["summary"],
          resumed["summary"])
    return audit_id


def drill_expected_findings(repo: Path, audit_id: str) -> None:
    print("\n== per-resource findings vs hand-maintained table ==")
    report = run(repo, "audit", "report", "--audit-id", audit_id)
    check("report status complete", report["status"], "complete")
    check_expected_table("A1", report, EXPECTED_A1)
    check("A1 per-cut summaries", report["summary"]["cuts"],
          EXPECTED_A1_SUMMARY)
    check("A1 memberships (14+14+13)", report["summary"]["memberships"], 41)
    check("A1 unique evaluations (version reuse across cuts)",
          report["summary"]["unique_evaluations"], 15)
    check("A1 deleted total", report["summary"]["total"]["deleted"], 1)
    deleted = report["deleted"]
    check("deleted entry is p-del v2 at cut 32",
          [(d["resourceType"], d["id"], d["versionId"], d["cut_seq"])
           for d in deleted],
          [("Patient", "p-del", "v2", 32)])

    # Spot-check reviewable expected/actual fields of key findings.
    by_id = {}
    for r in report["resources"]:
        if r["cut_seq"] == 30:
            by_id[r["identity"]["id"]] = r
    bad = {f["code"]: f for f in by_id["p-bad"]["findings"]}
    check("p-bad fixed_mismatch expected/actual",
          (bad["fixed_mismatch"]["expected"], bad["fixed_mismatch"]["actual"]),
          (True, False))
    check("p-bad min_cardinality element",
          bad["min_cardinality"]["element_id"], "Patient.name")
    check("p-bad binding actual coding",
          bad["code_not_in_valueset"]["actual"]["codings"],
          [{"system": "", "code": "other"}])
    check("p-bad selected profile",
          by_id["p-bad"]["selected_profiles"], [f"{SD_PATIENT}|1.0.0"])
    obad = {f["code"]: f for f in by_id["o-bad"]["findings"]}
    check("o-bad choice type actual",
          obad["choice_type_not_allowed"]["actual"]["type"], "String")
    check("o-bad slice_unmatched is the closed-slicing extra",
          obad["slice_unmatched"]["element_id"], "Observation.component")
    strict = {f["code"]: f for f in by_id["o-strict"]["findings"]}
    check("o-strict targetProfile element",
          strict["unsupported_target_profile"]["element_id"],
          "Observation.subject")
    check("o-strict severity of unsupported",
          {f["severity"] for f in by_id["o-strict"]["findings"]},
          {"unsupported"})

    # Determinism: two exports are byte-identical.
    out1 = run(repo, "audit", "report", "--audit-id", audit_id,
               "--out", str(repo.parent / "r1.json"))
    out2 = run(repo, "audit", "report", "--audit-id", audit_id,
               "--out", str(repo.parent / "r2.json"))
    check("report exports written", (out1["status"], out2["status"]),
          ("exported", "exported"))
    b1 = (repo.parent / "r1.json").read_bytes()
    b2 = (repo.parent / "r2.json").read_bytes()
    check("report export is deterministic", b1 == b2, True)


def drill_ig2(repo: Path, packages: Path, audit1_id: str) -> None:
    print("\n== two guideline versions coexist; old audit stays stable ==")
    out = run(repo, "profile", "register",
              "--package-dir", str(packages / "synth-ig-2.0.0"))
    check("register synth-ig 2.0.0", out["status"], "registered")

    second = run(repo, "audit", "run", "--source-server", SERVER_IG,
                 "--tenant", TENANT, "--cut", "31",
                 "--package", "synth-ig:2.0.0")
    check("2.0.0 audit completes", second["status"], "complete")
    report = run(repo, "audit", "report", "--audit-id", second["audit_id"])
    got = resource_map(report).get(31, {})
    for key, expected in EXPECTED_A2_CUT31.items():
        check(f"2.0.0 cut31 {key}", got.get(key), expected)
    check("2.0.0 cut31 pass count",
          report["summary"]["cuts"]["31"]["pass"], 4)

    # Re-running the old 1.0.0 request reuses the stored result unchanged.
    old = run(repo, "audit", "run", "--source-server", SERVER_IG,
              "--tenant", TENANT, "--cut", "30", "--cut", "31", "--cut", "32",
              "--package", "synth-ig:1.0.0")
    check("old audit id stable", old["audit_id"], audit1_id)
    check("old audit folded", old["status"], "already_complete")
    check("old audit summary unchanged", old["summary"]["cuts"],
          EXPECTED_A1_SUMMARY)


def drill_explicit_root(repo: Path) -> None:
    print("\n== explicit root profile selection ==")
    out = run(repo, "audit", "run", "--source-server", SERVER_IG,
              "--tenant", TENANT, "--cut", "30", "--package", "synth-ig:1.0.0",
              "--root-profile", f"{SD_PATIENT}|1.0.0")
    check("explicit-root audit completes", out["status"], "complete")
    summary = out["summary"]["cuts"]["30"]
    for key, expected in EXPECTED_ROOT_CUT30.items():
        check(f"explicit-root cut30 {key}", summary[key], expected)
    report = run(repo, "audit", "report", "--audit-id", out["audit_id"])
    got = resource_map(report)[30]
    check("p-noprof evaluated under explicit root",
          got["Patient/p-noprof"], ("pass", []))
    check("observation not applicable to Patient root",
          got["Observation/o-ok"], ("no_profile", []))


def drill_ambiguity(repo: Path) -> None:
    print("\n== canonical ambiguity is a visible result, never a pass ==")
    out = run(repo, "audit", "run", "--source-server", SERVER_IG,
              "--tenant", TENANT, "--cut", "30", "--package",
              "synth-ambig:1.0.0")
    check("ambiguity audit completes", out["status"], "complete")
    summary = out["summary"]["cuts"]["30"]
    for key, expected in EXPECTED_AMBIG_CUT30.items():
        check(f"ambig cut30 {key}", summary[key], expected)
    report = run(repo, "audit", "report", "--audit-id", out["audit_id"])
    got = resource_map(report)[30]
    check("p-ambig is ambiguous, not passed",
          got["Patient/p-ambig"][0], "profile_ambiguous")


def drill_unknown_cut_and_package(repo: Path) -> None:
    print("\n== stable error statuses ==")
    out = run(repo, "audit", "run", "--source-server", SERVER_IG,
              "--tenant", TENANT, "--cut", "99", "--package",
              "synth-ig:1.0.0", expect_rc=2)
    check("unpublished cut rejected", out["status"], "cut_not_queryable")
    out = run(repo, "audit", "run", "--source-server", SERVER_IG,
              "--tenant", TENANT, "--cut", "30", "--package",
              "no-such:9.9.9", expect_rc=2)
    check("unknown package rejected", out["status"], "unknown_package")
    out = run(repo, "audit", "status", "--audit-id", "0" * 64, expect_rc=2)
    check("unknown audit rejected", out["status"], "unknown_audit")


def main() -> int:
    build = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build"
    fixtures = build / "ig-fixtures"
    repo = build / "ig-handoff"
    if repo.exists():
        shutil.rmtree(repo)
    subprocess.run([sys.executable, "-I",
                    str(ROOT / "tools" / "make_ig_fixtures.py"),
                    str(fixtures)], check=True, capture_output=True)

    sources = {"sources": [{"source_server": SERVER_IG, "aliases": []}]}
    sf = build / "ig-sources.json"
    sf.write_text(json.dumps(sources), encoding="utf-8")
    run(repo, "init", "--sources-file", str(sf))
    run(repo, "stage", "--manifest", str(fixtures / "history"
                                         / "manifest.json"))
    adv = run(repo, "advance", "--source-server", SERVER_IG,
              "--tenant", TENANT)
    check("history advanced to 32", adv["frontier"], 32)

    packages = fixtures / "packages"
    drill_registration(repo, packages)
    audit1_id = drill_interrupted_audit(repo)
    drill_expected_findings(repo, audit1_id)
    drill_ig2(repo, packages, audit1_id)
    drill_explicit_root(repo)
    drill_ambiguity(repo)
    drill_unknown_cut_and_package(repo)

    print("\n== summary ==")
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("ALL AUDIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
