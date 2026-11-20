#!/usr/bin/env python3 -I
"""Independent checker for bed daily-report publishing and loading.

Deliberately does **not** import ``fhist``. It:

* drives the real implementation only through the ``bin/fhist`` CLI,
* keeps hand-written expected fact tables for the fixed scenarios
  (headline numbers, mid-day ward change, local-day boundaries, both
  New York DST transitions, out-of-calendar occupancy, foreign beds),
* re-implements the whole calendar/occupancy/slice/diff semantics from
  scratch (~200 lines) and cross-checks every published fact, exception
  and unassigned diagnostic against the raw fixture packages,
* verifies release manifests by recomputing every digest and identity
  itself (file sha256, release id, facts identity),
* proves the correction chain loaded into SQLite equals a from-scratch
  full publish at the target cut+calendar,
* attacks the loader with a tampered local pre-state, corrupt and
  truncated deliveries, missing files, wrong-series manifests, skipped
  batches and interrupted publishes.

Run with ``python3 -I tools/check_bedreport.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE_A = "https://fhir.example.org/a"
ALIAS_A = "https://fhir-alias.example.net/a"
BASE_B = "https://fhir.example.org/b"
BASE_L = "https://fhir.example.org/largeward"

FAILURES: list[str] = []


def check(name: str, got, expected) -> None:
    if got == expected:
        print(f"  PASS {name}")
    else:
        FAILURES.append(f"{name}: expected {expected!r}, got {got!r}")
        print(f"  FAIL {name}: expected {expected!r}, got {got!r}")


def run(repo: Path | None, *args: str, check_rc: bool = True) -> dict:
    cmd = [sys.executable, "-I", str(ROOT / "src" / "fhist" / "cli.py")]
    if repo is not None:
        cmd += ["--repo", str(repo)]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise AssertionError(
            f"command failed ({args}): rc={proc.returncode}\n"
            f"{proc.stderr}\n{proc.stdout}")
    if not proc.stdout.strip():
        return {"_rc": proc.returncode, "_stderr": proc.stderr}
    out = json.loads(proc.stdout)
    out["_rc"] = proc.returncode
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_ndjson(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Independent re-implementation of the report semantics (no fhist code)
# ---------------------------------------------------------------------------

TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})$")


def ind_parse_ts(text: str) -> int:
    m = TS_RE.match(text or "")
    if not m:
        raise ValueError(f"bad timestamp {text!r}")
    y, mo, d, hh, mm, ss, off = m.groups()
    if off == "Z":
        tz = timezone.utc
    else:
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[4:6])
        tz = timezone(sign * timedelta(hours=oh, minutes=om))
    return int(datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss),
                        tzinfo=tz).timestamp())


def ind_day_bounds(tz_name: str, d_from: str, d_to: str):
    tz = timezone.utc if tz_name == "UTC" else ZoneInfo(tz_name)
    d0, d1 = date.fromisoformat(d_from), date.fromisoformat(d_to)
    days = []
    d = d0
    while d < d1:
        s = datetime.combine(d, time.min, tzinfo=tz)
        e = datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz)
        days.append((d.isoformat(), int(s.timestamp()), int(e.timestamp())))
        d += timedelta(days=1)
    return days


def ind_calendar(path: Path):
    """bed -> [(start, end, ward)] with same-ward unions; rejects overlaps."""

    import csv
    per_bed: dict[str, list[tuple[int, int, str]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            bed = rec["bed_id"].strip()
            per_bed.setdefault(bed, []).append(
                (ind_parse_ts(rec["start"].strip()),
                 ind_parse_ts(rec["end"].strip()), rec["ward_code"].strip()))
    out: dict[str, list[tuple[int, int, str]]] = {}
    for bed, ivs in per_bed.items():
        by_ward: dict[str, list[tuple[int, int]]] = {}
        for s, e, w in ivs:
            by_ward.setdefault(w, []).append((s, e))
        merged: list[tuple[int, int, str]] = []
        for w, spans in by_ward.items():
            spans.sort()
            cur: list | None = None
            for s, e in spans:
                if cur and s <= cur[1]:
                    cur[1] = max(cur[1], e)
                else:
                    if cur:
                        merged.append((cur[0], cur[1], w))
                    cur = [s, e]
            if cur:
                merged.append((cur[0], cur[1], w))
        merged.sort()
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if a[2] != b[2] and a[0] < b[1] and b[0] < a[1]:
                    raise AssertionError(f"calendar overlap across wards: {bed}")
        out[bed] = merged
    return out


def ind_fixture_facts(fixture: Path) -> list[dict]:
    """All PUT/DELETE operations of a fixture package, in commit order."""

    manifest = json.loads((fixture / "manifest.json").read_text())
    ops: list[dict] = []
    files = [(manifest["baseline"]["path"], manifest["baseline"]["seq"])]
    for txn in manifest["transactions"]:
        for sh in txn["shards"]:
            files.append((sh["path"], txn["seq"]))
    for rel, seq in files:
        with open(fixture / rel, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    ops.append(json.loads(line))
    ops.sort(key=lambda o: (o["seq"], o["ordinal"]))
    return ops


def ind_at_cut(ops: list[dict], cut: int):
    """(locations_alive, encounters) current at the cut."""

    latest: dict[tuple[str, str], dict] = {}
    for op in ops:
        if op["seq"] <= cut:
            latest[(op["resourceType"], op["id"])] = op
    locations = {rid for (rt, rid), op in latest.items()
                 if rt == "Location" and op["method"] == "PUT"}
    encounters = [op["resource"] for (rt, _), op in sorted(
        latest.items(), key=lambda kv: kv[0][1])
        if rt == "Encounter" and op["method"] == "PUT"]
    return locations, encounters


def ind_occupancy(encounters, locations, aliases, window):
    """bed -> [(s, e, enc_id)]; plus the exception kinds per encounter."""

    occ: dict[str, list[tuple[int, int, str]]] = {}
    exceptions: list[tuple[str, str]] = []  # (encounter id, kind)
    for enc in encounters:
        eid = enc["id"]
        for entry in enc.get("location", []):
            status = entry.get("status")
            if status not in ("active", "completed"):
                continue
            ref = (entry.get("location") or {}).get("reference") or ""
            bed = None
            for base in sorted(aliases, key=len, reverse=True):
                if ref.startswith(base + "/"):
                    ref = ref[len(base) + 1:]
                    break
            else:
                if "://" in ref:
                    exceptions.append((eid, "location_ref_external"))
                    continue
            m = re.match(r"^Location/([A-Za-z0-9\-.]+)$", ref)
            if m:
                bed = m.group(1)
            elif ref.startswith("Location/"):
                exceptions.append((eid, "location_ref_versioned"))
                continue
            else:
                exceptions.append((eid, "location_ref_invalid"))
                continue
            if bed not in locations:
                exceptions.append((eid, "location_unresolvable"))
                continue
            period = entry.get("period") or {}
            start_raw, end_raw = period.get("start"), period.get("end")
            if start_raw is None:
                exceptions.append((eid, "period_missing_start"))
                continue
            try:
                s = ind_parse_ts(start_raw)
            except ValueError:
                exceptions.append((eid, "period_bad_timestamp"))
                continue
            if end_raw is None:
                if status == "completed":
                    exceptions.append((eid, "period_missing_end"))
                    continue
                e = window[1]
                if e <= s:
                    continue
            else:
                try:
                    e = ind_parse_ts(end_raw)
                except ValueError:
                    exceptions.append((eid, "period_bad_timestamp"))
                    continue
                if e <= s:
                    exceptions.append((eid, "period_reversed_or_empty"))
                    continue
            cs, ce = max(s, window[0]), min(e, window[1])
            if ce > cs:
                occ.setdefault(bed, []).append((cs, ce, eid))
    return occ, exceptions


def ind_facts(calendar, occupancy, days, window):
    """{(date, ward): [open, empty, single, conflict]} + unassigned list."""

    facts: dict[tuple[str, str], list[int]] = {}
    unassigned: list[tuple[str, int, int, str]] = []
    beds = sorted(set(calendar) | set(occupancy))
    for bed in beds:
        open_iv = [(max(s, window[0]), min(e, window[1]), w)
                   for s, e, w in calendar.get(bed, [])
                   if min(e, window[1]) > max(s, window[0])]
        occ_iv = occupancy.get(bed, [])
        points = sorted({p for s, e, _ in open_iv for p in (s, e)}
                        | {p for s, e, _ in occ_iv for p in (s, e)})
        for a, b in zip(points, points[1:]):
            wards = [w for s, e, w in open_iv if s <= a and e >= b]
            encs = {eid for s, e, eid in occ_iv if s <= a and e >= b}
            for iso, ds, de in days:
                lo, hi = max(a, ds), min(b, de)
                if hi <= lo:
                    continue
                seconds = hi - lo
                if wards:
                    row = facts.setdefault((iso, wards[0]), [0, 0, 0, 0])
                    row[0] += seconds
                    row[1 + (0 if not encs else 1 if len(encs) == 1 else 2)] \
                        += seconds
                elif encs:
                    unassigned.append((bed, lo, hi, sorted(encs)[0]))
    return facts, unassigned


def ind_facts_digest(rows: dict[tuple[str, str], list[int]]) -> str:
    h = hashlib.sha256()
    for (d, w), v in sorted(rows.items()):
        h.update(f"{d}|{w}|{v[0]}|{v[1]}|{v[2]}|{v[3]}\n".encode())
    return h.hexdigest()


def ind_release_id(manifest: dict) -> str:
    core = json.loads(json.dumps(manifest))
    core["release"].pop("seq", None)
    core["release"].pop("release_id", None)
    return hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()).hexdigest()


def facts_of_release(release_dir: Path) -> dict[tuple[str, str], list[int]]:
    out = {}
    for row in read_ndjson(release_dir / "evidence" / "facts-full.ndjson"):
        out[(row["local_date"], row["ward_code"])] = [
            row["open_seconds"], row["empty_seconds"],
            row["single_seconds"], row["conflict_seconds"]]
    return out


def db_facts(db_path: Path, series_id: str) -> dict[tuple[str, str], list[int]]:
    conn = sqlite3.connect(str(db_path))
    out = {}
    for r in conn.execute(
            "SELECT local_date, ward_code, open_seconds, empty_seconds, "
            "single_seconds, conflict_seconds FROM bed_facts "
            "WHERE series_id=?", (series_id,)):
        out[(r[0], r[1])] = [r[2], r[3], r[4], r[5]]
    conn.close()
    return out


def verify_manifest_independently(release_dir: Path) -> list[str]:
    problems = []
    m = json.loads((release_dir / "manifest.json").read_text())
    if ind_release_id(m) != m["release"]["release_id"]:
        problems.append("release_id recompute mismatch")
    for entry in m["delivery"]["files"] + m["evidence"]["files"]:
        p = release_dir / entry["path"]
        if not p.is_file():
            problems.append(f"missing {entry['path']}")
            continue
        if sha256_file(p) != entry["sha256"]:
            problems.append(f"digest {entry['path']}")
        if entry.get("rows") is not None and entry["path"].endswith(".ndjson"):
            if len(read_ndjson(p)) != entry["rows"]:
                problems.append(f"rows {entry['path']}")
    facts = facts_of_release(release_dir)
    if ind_facts_digest(facts) != m["release"]["target"]["facts_sha256"]:
        problems.append("target facts identity")
    if len(facts) != m["release"]["target"]["fact_rows"]:
        problems.append("target fact_rows")
    return problems


# ---------------------------------------------------------------------------
# Hand-written expectation tables
# ---------------------------------------------------------------------------

H = 3600
HEADLINE_R1 = {("2026-03-10", "W1"): [24 * H, 20 * H, 3 * H, 1 * H],
               ("2026-03-10", "W2"): [24 * H, 24 * H, 0, 0]}
HEADLINE_R2 = {("2026-03-10", "W1"): [24 * H, 23 * H, 1 * H, 0],
               ("2026-03-10", "W2"): [24 * H, 24 * H, 0, 0]}
HEADLINE_R3 = {("2026-03-10", "W3"): [24 * H, 23 * H, 1 * H, 0],
               ("2026-03-10", "W2"): [24 * H, 24 * H, 0, 0]}
EXTENDED = {("2026-03-15", "W1"): [48 * H, 28 * H, 20 * H, 0],
            ("2026-03-15", "W2"): [12 * H, 6 * H, 6 * H, 0]}
NY_SPRING = {("2026-03-07", "W4"): [24 * H, 24 * H, 0, 0],
             ("2026-03-08", "W4"): [23 * H, 21.5 * H, 1.5 * H, 0],
             ("2026-03-09", "W4"): [24 * H, 21.5 * H, 2.5 * H, 0]}
NY_FALL = {("2026-10-31", "W4"): [24 * H, 23.5 * H, 0.5 * H, 0],
           ("2026-11-01", "W4"): [25 * H, 20.5 * H, 4.5 * H, 0]}
ISO_B = {("2026-03-10", "WB"): [24 * H, 22 * H, 2 * H, 0]}


def norm(table):
    return {k: [int(x) for x in v] for k, v in table.items()}


# ---------------------------------------------------------------------------
# Drills
# ---------------------------------------------------------------------------

def publish(repo, server, tenant, cut, tz, d_from, d_to, calendar,
            root=None, base=None, check_rc=True):
    args = ["bedreport", "publish", "--source-server", server,
            "--tenant", tenant, "--cut", str(cut), "--timezone", tz,
            "--from-date", d_from, "--to-date", d_to,
            "--calendar", str(calendar)]
    if root:
        args += ["--reports-root", str(root)]
    if base:
        args += ["--base-release", str(base)]
    return run(repo, *args, check_rc=check_rc)


def drill_headline(repo: Path, fixtures: Path, build: Path) -> None:
    print("\n== headline: fixed numbers, evidence, correction chain ==")
    cals = fixtures / "calendars"
    r1 = publish(repo, BASE_A, "tenant-ward", 10, "UTC",
                 "2026-03-10", "2026-03-11", cals / "headline-v1.csv")
    check("R1 published", r1["status"], "published")
    r1_dir = Path(r1["release_dir"])
    check("R1 facts match hand table",
          facts_of_release(r1_dir), norm(HEADLINE_R1))
    check("R1 manifest verifies independently",
          verify_manifest_independently(r1_dir), [])

    # Evidence: the conflict slice names both encounters with versions.
    ev = run(None, "bedreport", "evidence", "--release", str(r1_dir),
             "--section", "slices", "--bed", "bed-1", "--kind", "conflict")
    check("one conflict slice", len(ev["rows"]), 1)
    slice_row = ev["rows"][0]
    check("conflict slice is 03:00-04:00 UTC",
          (slice_row["start_utc"], slice_row["end_utc"], slice_row["seconds"]),
          ("2026-03-10T03:00:00+00:00", "2026-03-10T04:00:00+00:00", 3600))
    parties = sorted(o["encounter_id"] for o in slice_row["occupants"])
    check("conflict parties recoverable from evidence", parties, ["E1", "E2"])
    versions = {o["encounter_id"]: o["version_id"]
                for o in slice_row["occupants"]}
    check("conflict party versions", versions, {"E1": "v1", "E2": "v1"})
    check("conflict slice cites calendar line", slice_row["calendar_lines"],
          [2])

    # Empty evidence also traces back to the calendar.
    ev_empty = run(None, "bedreport", "evidence", "--release", str(r1_dir),
                   "--section", "slices", "--bed", "bed-2",
                   "--kind", "empty")
    check("bed-2 empty all day, calendar line cited",
          [(r["seconds"], r["calendar_lines"]) for r in ev_empty["rows"]],
          [(86400, [3])])

    # Exceptions of E6 are retained per item with location indexes.
    exc = run(None, "bedreport", "evidence", "--release", str(r1_dir),
              "--section", "exceptions")
    kinds = sorted((r["location_index"], r["kind"])
                   for r in exc["rows"] if r["encounter_id"] == "E6")
    check("E6 per-item exceptions", kinds, [
        (0, "period_missing_end"), (1, "period_missing_start"),
        (2, "period_reversed_or_empty"), (3, "period_bad_timestamp"),
        (4, "location_unresolvable"), (5, "location_ref_external"),
        (6, "location_ref_versioned")])

    # bed-del exists at cut 10 but is not in the calendar: unassigned.
    una = run(None, "bedreport", "evidence", "--release", str(r1_dir),
              "--section", "unassigned")
    check("cut10 unassigned is E6 on bed-del",
          [(r["bed_id"], r["seconds"],
            r["occupants"][0]["encounter_id"]) for r in una["rows"]],
          [("bed-del", 3600, "E6")])

    # R2: cut 11 (E2 deleted, E1 corrected, bed-del tombstoned), same calendar.
    r2 = publish(repo, BASE_A, "tenant-ward", 11, "UTC",
                 "2026-03-10", "2026-03-11", cals / "headline-v1.csv",
                 base=r1_dir)
    check("R2 published", r2["status"], "published")
    r2_dir = Path(r2["release_dir"])
    check("R2 facts match hand table",
          facts_of_release(r2_dir), norm(HEADLINE_R2))
    up2 = read_ndjson(r2_dir / "delivery" / "facts-upsert.ndjson")
    re2 = read_ndjson(r2_dir / "delivery" / "facts-retract.ndjson")
    check("R2 delivers exactly the changed W1 row",
          up2, [{"conflict_seconds": 0, "empty_seconds": 82800,
                 "local_date": "2026-03-10", "open_seconds": 86400,
                 "single_seconds": 3600, "ward_code": "W1"}])
    check("R2 retracts nothing (W2 unchanged row not delivered)", re2, [])
    check("R2 manifest verifies independently",
          verify_manifest_independently(r2_dir), [])
    exc2 = run(None, "bedreport", "evidence", "--release", str(r2_dir),
               "--section", "exceptions")
    check("cut11 bed-del tombstone becomes an exception",
          sorted(r["kind"] for r in exc2["rows"]
                 if r["encounter_id"] == "E6" and r["location_index"] == 8),
          ["location_unresolvable"])

    # R3: same cut, corrected calendar moves bed-1 to ward W3.
    r3 = publish(repo, BASE_A, "tenant-ward", 11, "UTC",
                 "2026-03-10", "2026-03-11", cals / "headline-v2.csv",
                 base=r2_dir)
    r3_dir = Path(r3["release_dir"])
    check("R3 facts match hand table",
          facts_of_release(r3_dir), norm(HEADLINE_R3))
    up3 = read_ndjson(r3_dir / "delivery" / "facts-upsert.ndjson")
    re3 = read_ndjson(r3_dir / "delivery" / "facts-retract.ndjson")
    check("R3 writes the same measures under the new ward key",
          up3, [{"conflict_seconds": 0, "empty_seconds": 82800,
                 "local_date": "2026-03-10", "open_seconds": 86400,
                 "single_seconds": 3600, "ward_code": "W3"}])
    check("R3 retracts the old ward key (not a zero row)",
          re3, [{"local_date": "2026-03-10", "ward_code": "W1"}])
    check("R3 manifest verifies independently",
          verify_manifest_independently(r3_dir), [])

    # Determinism: identical re-run folds onto the same release.
    r2_again = publish(repo, BASE_A, "tenant-ward", 11, "UTC",
                       "2026-03-10", "2026-03-11",
                       cals / "headline-v1.csv", base=r1_dir)
    check("identical re-run folds", r2_again["status"], "folded")
    check("folded release keeps identity",
          (r2_again["release_id"], r2_again["release_seq"]),
          (r2["release_id"], r2["release_seq"]))
    check("no extra release dir created by the fold",
          sorted(p.name for p in r1_dir.parent.iterdir()
                 if p.name.startswith("rel-")),
          sorted(["rel-0001-" + r1["release_id"][:12],
                  "rel-0002-" + r2["release_id"][:12],
                  "rel-0003-" + r3["release_id"][:12]]))

    # Loader chain with attack drills.
    db = build / "bedload-headline.sqlite3"
    for p in db.parent.glob(db.name + "*"):
        p.unlink()
    sid = r1["series_id"]
    out = run(None, "bedload", "--db", str(db), "--release", str(r1_dir))
    check("load R1", out["status"], "loaded")
    check("db after R1", db_facts(db, sid), norm(HEADLINE_R1))
    out = run(None, "bedload", "--db", str(db), "--release", str(r1_dir))
    check("replay R1 is a no-op", out["status"], "already_loaded")
    check("db unchanged by replay", db_facts(db, sid), norm(HEADLINE_R1))
    out = run(None, "bedload", "--db", str(db), "--release", str(r3_dir),
              check_rc=False)
    check("skipped batch rejected", out["status"], "base_mismatch")
    check("db unchanged by skipped batch", db_facts(db, sid),
          norm(HEADLINE_R1))
    out = run(None, "bedload", "--db", str(db), "--release", str(r2_dir))
    check("load R2", out["status"], "loaded")
    check("db after R2", db_facts(db, sid), norm(HEADLINE_R2))

    # Tamper one local row: the next correction must be refused and the
    # already loaded results must survive untouched.
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE bed_facts SET empty_seconds = empty_seconds + 1 "
                 "WHERE ward_code='W1'")
    conn.commit()
    conn.close()
    out = run(None, "bedload", "--db", str(db), "--release", str(r3_dir),
              check_rc=False)
    check("tampered local pre-state rejected", out["status"],
          "local_state_mismatch")
    conn = sqlite3.connect(str(db))
    tampered = conn.execute(
        "SELECT empty_seconds FROM bed_facts WHERE ward_code='W1'"
    ).fetchone()[0]
    conn.close()
    check("tampered row not silently repaired or half-updated",
          tampered, 82801)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE bed_facts SET empty_seconds = empty_seconds - 1 "
                 "WHERE ward_code='W1'")
    conn.commit()
    conn.close()
    out = run(None, "bedload", "--db", str(db), "--release", str(r3_dir))
    check("load R3 after repair", out["status"], "loaded")
    check("db after full chain", db_facts(db, sid), norm(HEADLINE_R3))
    out = run(None, "bedload", "--db", str(db), "--release", str(r3_dir))
    check("replay R3 is a no-op", out["status"], "already_loaded")

    # The chained result equals a from-scratch full publish at cut 11 +
    # calendar v2 (fresh reports root -> fresh series -> initial release).
    fresh_root = build / "bed-reports-fresh"
    shutil.rmtree(fresh_root, ignore_errors=True)
    full = publish(repo, BASE_A, "tenant-ward", 11, "UTC",
                   "2026-03-10", "2026-03-11", cals / "headline-v2.csv",
                   root=fresh_root)
    check("fresh full publish is initial", full["kind"], "initial")
    check("chained corrections equal full publish at target cut+calendar",
          db_facts(db, sid), facts_of_release(Path(full["release_dir"])))
    check("full publish facts identity equals R3 target",
          full["facts_sha256"], r3["facts_sha256"])

    # Corrupt delivery: flip a byte in a copied release.
    broken = build / "attack-corrupt"
    shutil.rmtree(broken, ignore_errors=True)
    shutil.copytree(r2_dir, broken)
    target = broken / "delivery" / "facts-upsert.ndjson"
    data = target.read_bytes()
    target.write_bytes(data[:20] + b"X" + data[21:])
    out = run(None, "bedload", "--db", str(build / "x1.sqlite3"),
              "--release", str(broken), check_rc=False)
    check("corrupt delivery rejected", out["status"], "bad_delivery")

    # Missing delivery file.
    missing = build / "attack-missing"
    shutil.rmtree(missing, ignore_errors=True)
    shutil.copytree(r2_dir, missing)
    (missing / "delivery" / "facts-retract.ndjson").unlink()
    out = run(None, "bedload", "--db", str(build / "x2.sqlite3"),
              "--release", str(missing), check_rc=False)
    check("missing delivery file rejected", out["status"], "bad_delivery")

    # Wrong series: tamper the tenant inside a copied manifest.
    wrong = build / "attack-series"
    shutil.rmtree(wrong, ignore_errors=True)
    shutil.copytree(r2_dir, wrong)
    m = json.loads((wrong / "manifest.json").read_text())
    m["series"]["tenant"] = "tenant-other"
    (wrong / "manifest.json").write_text(json.dumps(m, indent=2))
    out = run(None, "bedload", "--db", str(build / "x3.sqlite3"),
              "--release", str(wrong), check_rc=False)
    check("tampered series manifest rejected", out["status"], "bad_manifest")

    # A moved release directory still verifies and loads.
    moved = build / "moved-release"
    shutil.rmtree(moved, ignore_errors=True)
    shutil.copytree(r3_dir, moved)
    out = run(None, "bedreport", "verify", "--release", str(moved))
    check("moved release verifies", out["status"], "ok")
    db2 = build / "bedload-moved.sqlite3"
    for p in db2.parent.glob(db2.name + "*"):
        p.unlink()
    run(None, "bedload", "--db", str(db2), "--release", str(r1_dir))
    run(None, "bedload", "--db", str(db2), "--release", str(r2_dir))
    out = run(None, "bedload", "--db", str(db2), "--release", str(moved))
    check("moved release loads", out["status"], "loaded")
    check("moved-load final facts", db_facts(db2, sid), norm(HEADLINE_R3))

    # verify spots corruption in a copied release.
    shutil.rmtree(build / "verify-corrupt", ignore_errors=True)
    shutil.copytree(r2_dir, build / "verify-corrupt")
    ev_target = build / "verify-corrupt" / "evidence" / "slices.ndjson"
    ev_target.write_bytes(ev_target.read_bytes() + b"\n")
    out = run(None, "bedreport", "verify",
              "--release", str(build / "verify-corrupt"))
    check("verify flags corrupted release", out["status"], "corrupt")
    check("verify reports the damaged file",
          any("slices" in p for p in out["problems"]), True)

    # Crafted deliveries that pass digest checks (manifest re-signed) but
    # fail semantically: the load must roll back and stay retryable.
    def re_sign(release_dir: Path) -> None:
        mpath = release_dir / "manifest.json"
        m = json.loads(mpath.read_text())
        for entry in m["delivery"]["files"] + m["evidence"]["files"]:
            p = release_dir / entry["path"]
            entry["sha256"] = sha256_file(p)
            if entry.get("rows") is not None \
                    and entry["path"].endswith(".ndjson"):
                entry["rows"] = len(read_ndjson(p))
        m["release"]["release_id"] = ind_release_id(m)
        mpath.write_text(json.dumps(m, ensure_ascii=False, indent=2,
                                    sort_keys=True) + "\n")

    crafted = build / "attack-badrow"
    shutil.rmtree(crafted, ignore_errors=True)
    shutil.copytree(r2_dir, crafted)
    up = crafted / "delivery" / "facts-upsert.ndjson"
    rows = read_ndjson(up)
    rows[0]["empty_seconds"] += 60  # breaks open == empty+single+conflict
    up.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                          for r in rows))
    re_sign(crafted)
    db3 = build / "bedload-crafted.sqlite3"
    for p in db3.parent.glob(db3.name + "*"):
        p.unlink()
    run(None, "bedload", "--db", str(db3), "--release", str(r1_dir))
    out = run(None, "bedload", "--db", str(db3), "--release", str(crafted),
              check_rc=False)
    check("semantically invalid row rejected mid-load", out["status"],
          "bad_delivery")
    check("failed load left the partition untouched",
          db_facts(db3, sid), norm(HEADLINE_R1))
    out = run(None, "bedload", "--db", str(db3), "--release", str(r2_dir))
    check("correct retry after failed load succeeds", out["status"], "loaded")
    check("retry reached the target facts", db_facts(db3, sid),
          norm(HEADLINE_R2))

    # Crafted delivery whose rows are valid but incomplete vs the target
    # identity: the post-load check must roll the transaction back.
    crafted2 = build / "attack-target"
    shutil.rmtree(crafted2, ignore_errors=True)
    shutil.copytree(r2_dir, crafted2)
    up2p = crafted2 / "delivery" / "facts-upsert.ndjson"
    extra = dict(read_ndjson(up2p)[0])
    extra["ward_code"] = "W9"  # adds a row the target identity forbids
    with open(up2p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(extra, sort_keys=True) + "\n")
    re_sign(crafted2)
    db4 = build / "bedload-crafted2.sqlite3"
    for p in db4.parent.glob(db4.name + "*"):
        p.unlink()
    run(None, "bedload", "--db", str(db4), "--release", str(r1_dir))
    out = run(None, "bedload", "--db", str(db4), "--release", str(crafted2),
              check_rc=False)
    check("post-load target identity enforced", out["status"],
          "target_mismatch")
    check("target-mismatch load rolled back",
          db_facts(db4, sid), norm(HEADLINE_R1))

    # Interrupted publish: a stale temp dir is cleaned, old batches untouched.
    junk = r1_dir.parent / ".tmp-9999-dead"
    junk.mkdir(exist_ok=True)
    (junk / "staging.sqlite3").write_bytes(b"junk")
    before = {p.name: sha256_file(p / "manifest.json")
              for p in r1_dir.parent.iterdir() if p.name.startswith("rel-")}
    r3_again = publish(repo, BASE_A, "tenant-ward", 11, "UTC",
                       "2026-03-10", "2026-03-11",
                       cals / "headline-v2.csv", base=r2_dir)
    check("recompute after interruption folds", r3_again["status"], "folded")
    check("stale temp dir cleaned", junk.exists(), False)
    after = {p.name: sha256_file(p / "manifest.json")
             for p in r1_dir.parent.iterdir() if p.name.startswith("rel-")}
    check("old base batches not rewritten", before, after)


def drill_extended(repo: Path, fixtures: Path, build: Path) -> None:
    print("\n== extended: mid-day ward change, open gaps, active open end ==")
    out = publish(repo, BASE_A, "tenant-ward", 11, "UTC",
                  "2026-03-15", "2026-03-16",
                  fixtures / "calendars" / "extended.csv",
                  root=build / "bed-reports-ext")
    r_dir = Path(out["release_dir"])
    check("extended facts match hand table",
          facts_of_release(r_dir), norm(EXTENDED))
    una = run(None, "bedreport", "evidence", "--release", str(r_dir),
              "--section", "unassigned")
    check("out-of-calendar tail kept as diagnostic with evidence",
          [(r["bed_id"], r["seconds"], r["occupants"][0]["encounter_id"])
           for r in una["rows"]],
          [("bed-5", 4 * 3600, "E5")])
    check("extended manifest verifies independently",
          verify_manifest_independently(r_dir), [])


def drill_dst(repo: Path, fixtures: Path, build: Path) -> None:
    print("\n== New York DST: 23h and 25h local days, local day boundary ==")
    sp = publish(repo, BASE_A, "tenant-ward", 11, "America/New_York",
                 "2026-03-07", "2026-03-10",
                 fixtures / "calendars" / "ny-spring.csv",
                 root=build / "bed-reports-ny")
    check("spring-forward facts match hand table",
          facts_of_release(Path(sp["release_dir"])), norm(NY_SPRING))
    fa = publish(repo, BASE_A, "tenant-ward", 11, "America/New_York",
                 "2026-10-31", "2026-11-02",
                 fixtures / "calendars" / "ny-fall.csv",
                 root=build / "bed-reports-ny2")
    check("fall-back facts match hand table",
          facts_of_release(Path(fa["release_dir"])), norm(NY_FALL))


def drill_isolation(repo: Path, fixtures: Path, build: Path) -> None:
    print("\n== same-named bed outside this source ==")
    out = publish(repo, BASE_B, "tenant-b", 20, "UTC",
                  "2026-03-10", "2026-03-11",
                  fixtures / "calendars" / "iso-b.csv",
                  root=build / "bed-reports-b")
    check("source B facts match hand table (no leak from source A)",
          facts_of_release(Path(out["release_dir"])), norm(ISO_B))


def drill_negative_calendars(repo: Path, fixtures: Path, build: Path) -> None:
    print("\n== calendar rejections ==")
    out = publish(repo, BASE_A, "tenant-ward", 10, "UTC",
                  "2026-03-10", "2026-03-11",
                  fixtures / "calendars" / "bad-overlap.csv",
                  root=build / "bed-reports-neg", check_rc=False)
    check("cross-ward overlap rejects the whole calendar",
          out["status"], "bad_calendar")
    out = publish(repo, BASE_A, "tenant-ward", 10, "UTC",
                  "2026-03-10", "2026-03-11",
                  fixtures / "calendars" / "bad-missing-bed.csv",
                  root=build / "bed-reports-neg", check_rc=False)
    check("calendar bed missing at cut rejects the publish",
          out["status"], "calendar_bed_missing")
    check("no release dir left behind by rejected publishes",
          list((build / "bed-reports-neg").rglob("rel-*"))
          if (build / "bed-reports-neg").exists() else [], [])


def drill_large(repo: Path, fixtures: Path, build: Path) -> None:
    print("\n== large history + large calendar: independent cross-check ==")
    cals = fixtures / "calendars"
    ops = ind_fixture_facts(fixtures / "largeward")
    days = ind_day_bounds("UTC", "2026-04-01", "2026-04-06")
    window = (days[0][1], days[-1][2])

    expected = {}
    for cut, cal_version in ((100, 1), (101, 2)):
        locations, encounters = ind_at_cut(ops, cut)
        cal = ind_calendar(cals / f"large-v{cal_version}.csv")
        occ, exceptions = ind_occupancy(encounters, locations, {BASE_L},
                                        window)
        facts, unassigned = ind_facts(cal, occ, days, window)
        expected[(cut, cal_version)] = (facts, exceptions, unassigned)

    r1 = publish(repo, BASE_L, "tenant-largeward", 100, "UTC",
                 "2026-04-01", "2026-04-06", cals / "large-v1.csv",
                 root=build / "bed-reports-large")
    r1_dir = Path(r1["release_dir"])
    facts1, exc1, una1 = expected[(100, 1)]
    check("large R1 facts equal independent engine",
          facts_of_release(r1_dir), facts1)
    check("large R1 facts identity equal independent digest",
          r1["facts_sha256"], ind_facts_digest(facts1))
    check("large R1 unassigned count equals independent engine",
          r1["summary"]["unassigned_intervals"], len(una1))
    check("large R1 manifest verifies independently",
          verify_manifest_independently(r1_dir), [])

    r2 = publish(repo, BASE_L, "tenant-largeward", 101, "UTC",
                 "2026-04-01", "2026-04-06", cals / "large-v2.csv",
                 root=build / "bed-reports-large", base=r1_dir)
    r2_dir = Path(r2["release_dir"])
    facts2, exc2, una2 = expected[(101, 2)]
    check("large R2 facts equal independent engine",
          facts_of_release(r2_dir), facts2)

    # Independent exact row-level diff expectation.
    want_up, want_re = [], []
    for key in sorted(set(facts1) | set(facts2)):
        if key not in facts1:
            want_up.append(key)
        elif key not in facts2:
            want_re.append(key)
        elif facts1[key] != facts2[key]:
            want_up.append(key)
    got_up = sorted((r["local_date"], r["ward_code"]) for r in
                    read_ndjson(r2_dir / "delivery" / "facts-upsert.ndjson"))
    got_re = sorted((r["local_date"], r["ward_code"]) for r in
                    read_ndjson(r2_dir / "delivery" / "facts-retract.ndjson"))
    check("large correction upsert keys equal independent diff",
          got_up, sorted(want_up))
    check("large correction retract keys equal independent diff",
          got_re, sorted(want_re))
    up_rows = {(r["local_date"], r["ward_code"]):
               [r["open_seconds"], r["empty_seconds"],
                r["single_seconds"], r["conflict_seconds"]]
               for r in read_ndjson(r2_dir / "delivery" / "facts-upsert.ndjson")}
    check("large upsert rows carry the new measures",
          up_rows, {k: facts2[k] for k in want_up})

    # Loader chain equals a fresh full publish at the target cut+calendar.
    db = build / "bedload-large.sqlite3"
    for p in db.parent.glob(db.name + "*"):
        p.unlink()
    out = run(None, "bedload", "--db", str(db), "--release", str(r1_dir))
    check("large R1 loads", out["status"], "loaded")
    out = run(None, "bedload", "--db", str(db), "--release", str(r2_dir))
    check("large R2 loads", out["status"], "loaded")
    check("large chained db equals independent target facts",
          db_facts(db, r1["series_id"]), facts2)
    fresh = publish(repo, BASE_L, "tenant-largeward", 101, "UTC",
                    "2026-04-01", "2026-04-06", cals / "large-v2.csv",
                    root=build / "bed-reports-large-fresh")
    check("large fresh full publish equals chained corrections",
          facts_of_release(Path(fresh["release_dir"])),
          db_facts(db, r1["series_id"]))


def main() -> int:
    build = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build"
    fixtures = build / "bed-fixtures"
    repo = build / "bed-repo"
    if not (fixtures / "wardA").exists():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "make_bed_fixtures.py"),
                        str(fixtures)], check=True, capture_output=True)
    shutil.rmtree(repo, ignore_errors=True)
    for d in build.glob("bed-reports*"):
        shutil.rmtree(d, ignore_errors=True)
    sources = {"sources": [
        {"source_server": BASE_A, "aliases": [ALIAS_A]},
        {"source_server": BASE_B, "aliases": []},
        {"source_server": BASE_L, "aliases": []},
    ]}
    sf = build / "bed-sources.json"
    sf.write_text(json.dumps(sources, indent=2), encoding="utf-8")
    run(repo, "init", "--sources-file", str(sf))
    run(repo, "stage", "--manifest", str(fixtures / "wardA" / "manifest.json"))
    run(repo, "advance", "--source-server", BASE_A, "--tenant", "tenant-ward")
    run(repo, "stage", "--manifest", str(fixtures / "wardB" / "manifest.json"))
    run(repo, "advance", "--source-server", BASE_B, "--tenant", "tenant-b")
    run(repo, "stage", "--manifest",
        str(fixtures / "largeward" / "manifest.json"))
    run(repo, "advance", "--source-server", BASE_L, "--tenant",
        "tenant-largeward")

    drill_headline(repo, fixtures, build)
    drill_extended(repo, fixtures, build)
    drill_dst(repo, fixtures, build)
    drill_isolation(repo, fixtures, build)
    drill_negative_calendars(repo, fixtures, build)
    drill_large(repo, fixtures, build)

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
