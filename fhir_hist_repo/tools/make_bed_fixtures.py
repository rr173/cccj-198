#!/usr/bin/env python3 -I
"""Generate synthetic fixtures for the bed daily-report drills.

Everything is invented offline data: FHIR handoff packages (baseline +
transactions) for three sources and the bed-open calendar CSVs used by the
bedreport publish drills. No hospital system is contacted; no real patient
or bed data exists.

Layout::

    build/bed-fixtures/
        wardA/          manifest.json + baseline + shards (source A tenant-ward)
        wardB/          manifest.json + baseline          (source B tenant-b)
        largeward/      manifest.json + baseline + shards (source L, generated
                                                          large history + calendars)
        calendars/      headline-v1.csv headline-v2.csv extended.csv
                        ny-spring.csv ny-fall.csv iso-b.csv
                        bad-overlap.csv bad-missing-bed.csv
                        large-v1.csv large-v2.csv
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fhist.jsonl import write_jsonl  # noqa: E402

BASE_A = "https://fhir.example.org/a"
ALIAS_A = "https://fhir-alias.example.net/a"
BASE_B = "https://fhir.example.org/b"
BASE_L = "https://fhir.example.org/largeward"
FOREIGN = "https://elsewhere.example.com/fhir"

DAY = 86400
# Window of the large scenario: 2026-04-01T00:00:00Z .. 2026-04-06T00:00:00Z
LARGE_W0 = 1775001600
LARGE_DAYS = 5
LARGE_BEDS = 1500
LARGE_ENCOUNTERS = 4000
LARGE_WARDS = 12


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _h(*parts) -> int:
    return int(hashlib.sha256(
        ":".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16], 16)


def ts(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def location(lid: str, vid: str) -> dict:
    return {
        "resourceType": "Location",
        "id": lid,
        "meta": {"versionId": vid, "lastUpdated": "2026-01-10T00:00:00Z"},
        "status": "active",
        "name": f"SYNTHETIC-BED-{lid}",
        "mode": "instance",
    }


def loc_entry(ref: str, status: str, start: str | None,
              end: str | None) -> dict:
    entry: dict = {"location": {"reference": ref}, "status": status}
    period: dict = {}
    if start is not None:
        period["start"] = start
    if end is not None:
        period["end"] = end
    if period:
        entry["period"] = period
    return entry


def encounter(eid: str, vid: str, entries: list[dict]) -> dict:
    return {
        "resourceType": "Encounter",
        "id": eid,
        "meta": {"versionId": vid, "lastUpdated": "2026-01-10T00:00:00Z"},
        "status": "completed",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                  "code": "IMP"},
        "location": entries,
    }


def op_put(seq: int, ordinal: int, resource: dict) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "PUT",
            "resourceType": resource["resourceType"], "id": resource["id"],
            "versionId": resource["meta"]["versionId"], "resource": resource}


def op_delete(seq: int, ordinal: int, rtype: str, rid: str, vid: str) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "DELETE",
            "resourceType": rtype, "id": rid, "versionId": vid}


def write_shard(dest: Path, rows: list[dict]) -> tuple[str, str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = write_jsonl(dest, iter(rows))
    rel = dest.relative_to(dest.parents[1]).as_posix()
    return rel, sha256_file(dest), n


def write_manifest(d: Path, source_server: str, tenant: str,
                   baseline: dict, txns: list[dict]) -> None:
    doc = {"format_version": 1, "source_server": source_server,
           "tenant": tenant, "aliases": [], "baseline": baseline,
           "transactions": txns}
    (d / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Source A / tenant-ward: headline + extended + DST + exception cases
# ---------------------------------------------------------------------------

def build_ward_a(root: Path) -> None:
    d = root / "wardA"
    baseline_rows: list[dict] = []
    ordinal = 0
    for bed in ("bed-1", "bed-2", "bed-3", "bed-4", "bed-5", "bed-7",
                "bed-del"):
        baseline_rows.append(op_put(10, ordinal, location(bed, "v1")))
        ordinal += 1

    e1 = encounter("E1", "v1", [
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10T01:00:00+00:00", "2026-03-10T04:00:00+00:00"),
        # duplicate registration of the same stay: must not self-conflict
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10T02:00:00+00:00", "2026-03-10T03:00:00+00:00"),
    ])
    e2 = encounter("E2", "v1", [
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10T03:00:00+00:00", "2026-03-10T05:00:00+00:00"),
    ])
    e3 = encounter("E3", "v1", [
        loc_entry("Location/bed-3", "completed",
                  "2026-03-15T06:00:00+00:00", "2026-03-15T18:00:00+00:00"),
    ])
    e4 = encounter("E4", "v1", [
        # crosses the spring-forward local midnight (2026-03-08 -> 03-09)
        loc_entry("Location/bed-4", "completed",
                  "2026-03-08T22:30:00-04:00", "2026-03-09T02:30:00-04:00"),
    ])
    e5 = encounter("E5", "v1", [
        # runs past the bed's open interval -> unassigned diagnostic tail
        loc_entry("Location/bed-5", "completed",
                  "2026-03-15T18:00:00+00:00", "2026-03-16T00:00:00+00:00"),
    ])
    e6 = encounter("E6", "v1", [
        # [0] completed without an end -> exception
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10T06:00:00+00:00", None),
        # [1] active without a start -> exception
        loc_entry("Location/bed-1", "active",
                  None, "2026-03-10T07:00:00+00:00"),
        # [2] reversed period -> exception
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10T10:00:00+00:00", "2026-03-10T09:00:00+00:00"),
        # [3] malformed timestamp -> exception
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10 08:00", "2026-03-10T09:00:00+00:00"),
        # [4] bed that never existed -> exception
        loc_entry("Location/bed-9", "completed",
                  "2026-03-10T06:00:00+00:00", "2026-03-10T07:00:00+00:00"),
        # [5] foreign absolute URL (same trailing id) -> exception
        loc_entry(f"{FOREIGN}/Location/bed-1", "completed",
                  "2026-03-10T06:00:00+00:00", "2026-03-10T07:00:00+00:00"),
        # [6] versioned reference -> exception
        loc_entry("Location/bed-1/_history/v1", "completed",
                  "2026-03-10T06:00:00+00:00", "2026-03-10T07:00:00+00:00"),
        # [7] planned status -> silently not occupancy
        loc_entry("Location/bed-1", "planned",
                  "2026-03-10T06:00:00+00:00", "2026-03-10T07:00:00+00:00"),
        # [8] bed-del: exists at cut 10, tombstoned at cut 11
        loc_entry("Location/bed-del", "completed",
                  "2026-03-10T06:00:00+00:00", "2026-03-10T07:00:00+00:00"),
    ])
    e7 = encounter("E7", "v1", [
        # active without end: open until the report window ends
        loc_entry("Location/bed-7", "active",
                  "2026-03-15T12:00:00+00:00", None),
    ])
    e8 = encounter("E8", "v1", [
        # crosses the fall-back local midnight (2026-10-31 -> 11-01)
        loc_entry("Location/bed-4", "completed",
                  "2026-10-31T23:30:00-04:00", "2026-11-01T03:30:00-05:00"),
    ])
    for res in (e1, e2, e3, e4, e5, e6, e7, e8):
        baseline_rows.append(op_put(10, ordinal, res))
        ordinal += 1
    b_rel, b_digest, b_count = write_shard(
        d / "baseline" / "baseline.ndjson", baseline_rows)

    # seq 11: delete E2, correct E1 to 01:00-02:00, tombstone bed-del.
    e1_v2 = encounter("E1", "v2", [
        loc_entry("Location/bed-1", "completed",
                  "2026-03-10T01:00:00+00:00", "2026-03-10T02:00:00+00:00"),
    ])
    t11 = [
        op_delete(11, 0, "Encounter", "E2", "v2"),
        op_put(11, 1, e1_v2),
        op_delete(11, 2, "Location", "bed-del", "v2"),
    ]
    s11 = write_shard(d / "shards" / "seq-11-part-0.ndjson", t11)

    write_manifest(d, BASE_A, "tenant-ward",
                   {"seq": 10, "path": b_rel, "sha256": b_digest,
                    "resource_count": b_count},
                   [{"seq": 11, "operation_count": 3, "shards": [
                       {"ordinal": 0, "path": s11[0], "sha256": s11[1],
                        "operation_count": s11[2]}]}])


# ---------------------------------------------------------------------------
# Source B / tenant-b: same-named bed-1 in a different identity namespace
# ---------------------------------------------------------------------------

def build_ward_b(root: Path) -> None:
    d = root / "wardB"
    rows = [
        op_put(20, 0, location("bed-1", "v1")),
        op_put(20, 1, encounter("EB1", "v1", [
            loc_entry("Location/bed-1", "completed",
                      "2026-03-10T10:00:00+00:00", "2026-03-10T12:00:00+00:00"),
        ])),
    ]
    b_rel, b_digest, b_count = write_shard(
        d / "baseline" / "baseline.ndjson", rows)
    write_manifest(d, BASE_B, "tenant-b",
                   {"seq": 20, "path": b_rel, "sha256": b_digest,
                    "resource_count": b_count}, [])


# ---------------------------------------------------------------------------
# Source L / tenant-largeward: generated large history + large calendars
# ---------------------------------------------------------------------------

def _large_calendar_rows(version: int) -> list[tuple[str, str, int, int]]:
    """(bed, ward, start, end) rows; version 2 remaps a subset of wards."""

    w0 = LARGE_W0
    w1 = w0 + LARGE_DAYS * DAY
    rows = []
    for i in range(LARGE_BEDS):
        bed = f"b-l-{i:04d}"
        h1 = _h("cal", i)
        ward_idx = h1 % LARGE_WARDS
        remap = version == 2 and _h("remap", i) % 7 == 0
        if remap:
            ward_idx = (ward_idx + 5) % LARGE_WARDS
        ward = f"WL-{ward_idx:02d}"
        mode = h1 % 10
        if mode < 7:
            rows.append((bed, ward, w0, w1))
        elif mode < 9:
            rows.append((bed, ward, w0, w0 + 2 * DAY + 12 * 3600))
            rows.append((bed, ward, w0 + 3 * DAY, w1))
        else:
            mid = w0 + 2 * DAY + 12 * 3600
            ward2 = f"WL-{(ward_idx + 1) % LARGE_WARDS:02d}"
            rows.append((bed, ward, w0, mid))
            rows.append((bed, ward2, mid, w1))
    return rows


def _large_encounter(j: int, vid: str, shift: int = 0) -> dict:
    entries = []
    for k in range(1 + _h("ent", j) % 2):
        bed = f"b-l-{_h('bed', j, k) % LARGE_BEDS:04d}"
        start = LARGE_W0 + _h("start", j, k) % (LARGE_DAYS * DAY - 7200) + shift
        start = min(start, LARGE_W0 + LARGE_DAYS * DAY - 1800)
        status = "completed" if _h("st", j, k) % 10 else "active"
        open_ended = status == "active" and _h("open", j, k) % 3 == 0
        end = None if open_ended else start + 1800 * (1 + _h("dur", j, k) % 16)
        ref = f"Location/{bed}" if _h("abs", j, k) % 20 \
            else f"{BASE_L}/Location/{bed}"
        entries.append(loc_entry(ref, status, ts(start),
                                 ts(end) if end is not None else None))
    return encounter(f"e-l-{j:05d}", vid, entries)


def build_largeward(root: Path) -> None:
    d = root / "largeward"
    baseline_rows: list[dict] = []
    ordinal = 0
    for i in range(LARGE_BEDS):
        baseline_rows.append(op_put(100, ordinal, location(f"b-l-{i:04d}", "v1")))
        ordinal += 1
    for j in range(LARGE_ENCOUNTERS):
        baseline_rows.append(op_put(100, ordinal, _large_encounter(j, "v1")))
        ordinal += 1
    b_rel, b_digest, b_count = write_shard(
        d / "baseline" / "baseline.ndjson", baseline_rows)

    txn_rows: list[dict] = []
    ordinal = 0
    for j in range(LARGE_ENCOUNTERS):
        if _h("del", j) % 13 == 0:
            txn_rows.append(op_delete(101, ordinal, "Encounter",
                                      f"e-l-{j:05d}", "v2"))
            ordinal += 1
        elif _h("mod", j) % 5 == 0:
            txn_rows.append(op_put(101, ordinal,
                                   _large_encounter(j, "v2", shift=3600)))
            ordinal += 1
    for k in range(50):
        txn_rows.append(op_put(101, ordinal,
                               _large_encounter(LARGE_ENCOUNTERS + k, "v1")))
        ordinal += 1
    s101 = write_shard(d / "shards" / "seq-101-part-0.ndjson", txn_rows)

    write_manifest(d, BASE_L, "tenant-largeward",
                   {"seq": 100, "path": b_rel, "sha256": b_digest,
                    "resource_count": b_count},
                   [{"seq": 101, "operation_count": len(txn_rows), "shards": [
                       {"ordinal": 0, "path": s101[0], "sha256": s101[1],
                        "operation_count": s101[2]}]}])


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------

def _write_calendar(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("bed_id,ward_code,start,end\n")
        for bed, ward, start, end in rows:
            fh.write(f"{bed},{ward},{start},{end}\n")


def build_calendars(root: Path) -> None:
    d = root / "calendars"
    _write_calendar(d / "headline-v1.csv", [
        ("bed-1", "W1", "2026-03-10T00:00:00+00:00",
         "2026-03-11T00:00:00+00:00"),
        ("bed-2", "W2", "2026-03-10T00:00:00+00:00",
         "2026-03-11T00:00:00+00:00"),
    ])
    # Correction: bed-1 moves to ward W3; bed-2 stays in W2 untouched.
    _write_calendar(d / "headline-v2.csv", [
        ("bed-1", "W3", "2026-03-10T00:00:00+00:00",
         "2026-03-11T00:00:00+00:00"),
        ("bed-2", "W2", "2026-03-10T00:00:00+00:00",
         "2026-03-11T00:00:00+00:00"),
    ])
    _write_calendar(d / "extended.csv", [
        ("bed-3", "W1", "2026-03-15T00:00:00+00:00",
         "2026-03-15T12:00:00+00:00"),
        ("bed-3", "W2", "2026-03-15T12:00:00+00:00",
         "2026-03-16T00:00:00+00:00"),
        ("bed-5", "W1", "2026-03-15T08:00:00+00:00",
         "2026-03-15T20:00:00+00:00"),
        ("bed-7", "W1", "2026-03-15T00:00:00+00:00",
         "2026-03-16T00:00:00+00:00"),
    ])
    _write_calendar(d / "ny-spring.csv", [
        ("bed-4", "W4", "2026-03-07T00:00:00-05:00",
         "2026-03-10T00:00:00-05:00"),
    ])
    _write_calendar(d / "ny-fall.csv", [
        ("bed-4", "W4", "2026-10-31T00:00:00-04:00",
         "2026-11-02T00:00:00-05:00"),
    ])
    _write_calendar(d / "iso-b.csv", [
        ("bed-1", "WB", "2026-03-10T00:00:00+00:00",
         "2026-03-11T00:00:00+00:00"),
    ])
    # Negative calendars.
    _write_calendar(d / "bad-overlap.csv", [
        ("bed-1", "W1", "2026-03-10T00:00:00+00:00",
         "2026-03-10T12:00:00+00:00"),
        ("bed-1", "W2", "2026-03-10T06:00:00+00:00",
         "2026-03-10T18:00:00+00:00"),
    ])
    _write_calendar(d / "bad-missing-bed.csv", [
        ("bed-9", "W1", "2026-03-10T00:00:00+00:00",
         "2026-03-11T00:00:00+00:00"),
    ])
    # Large calendars (generated, never committed to git).
    for version in (1, 2):
        rows = [(bed, ward, ts(s), ts(e))
                for bed, ward, s, e in _large_calendar_rows(version)]
        _write_calendar(d / f"large-v{version}.csv", rows)


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/bed-fixtures")
    root.mkdir(parents=True, exist_ok=True)
    build_ward_a(root)
    build_ward_b(root)
    build_largeward(root)
    build_calendars(root)
    print(json.dumps({"status": "ok", "fixtures": str(root)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
