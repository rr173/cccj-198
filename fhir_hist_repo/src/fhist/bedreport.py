"""Bed daily-report publishing: audited day-fact partitions and corrections.

A *series* pins ``(source_server, tenant, timezone, date_from, date_to)``.
The first release of a series delivers the full partition; later releases
(corrections) name a previously published release of the same series as
base and deliver only new/changed whole rows plus the keys that vanished.

Computation contract:

* All seconds are computed in absolute time (epoch seconds); local day
  boundaries come from the report timezone (``UTC`` or
  ``America/New_York``). Every interval — calendar open intervals,
  occupancy periods, hand-over instants, midnight cuts — is
  left-closed/right-open.
* Only ``Encounter.location`` entries with status ``active``/``completed``
  form occupancy. A ``completed`` entry needs both start and end; an
  ``active`` entry without end stays open until the report window ends.
* Location references must be unversioned ``Location/id`` (relative, or
  absolute under the configured canonical base/aliases) resolving to a
  non-tombstone Location of the same source+tenant at the cut. Foreign,
  versioned or deleted targets become per-item exceptions, never facts.
* Calendar beds must exist as non-tombstone Locations at the cut or the
  whole publish is rejected. Same-bed/same-ward calendar overlaps are
  unioned; same-bed overlaps across different wards reject the calendar.
* Occupancy outside any open interval is kept as per-bed diagnostics,
  never attributed to a ward and never silently dropped.
* Fact rows carry only business keys and measures
  ``(local_date, ward_code, open/empty/single/conflict seconds)`` so a
  version-coordinate change alone never rewrites a delivered row.

Memory: every publish re-scans the whole window (no incremental state),
but encounter versions, time slices and diffs stream through a staging
SQLite file and sorted NDJSON — the full window's objects, slices and
diff rows are never held in memory at once.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import blobs
from .config import RepoConfig
from .db import snapshot_tx
from .errors import (
    BedCalendarError,
    CalendarBedMissing,
    FHError,
    ReleaseError,
    SeriesMismatch,
    UnknownRelease,
    UsageError,
)
from .jsonl import iter_jsonl, stream_file_digest, stream_jsonl_digest_and_count
from .paths import RepoPaths
from .query import check_cut, load_source
from .refs import classify_ref
from .resolver import latest_version_at

FORMAT = "fhist-bedreport/1"
SUPPORTED_TIMEZONES = ("UTC", "America/New_York")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELEASE_DIR_RE = re.compile(r"^rel-(\d{4})-([0-9a-f]{12})$")
FACT_KEYS = ("local_date", "ward_code", "open_seconds", "empty_seconds",
             "single_seconds", "conflict_seconds")

ENCOUNTER_SQL = """
SELECT * FROM (
    SELECT rv.*, ROW_NUMBER() OVER (
        PARTITION BY resource_type, logical_id
        ORDER BY seq DESC, tx_ordinal DESC) AS rn
    FROM resource_version rv
    WHERE rv.source_id = ? AND rv.resource_type = 'Encounter' AND rv.seq <= ?
) WHERE rn = 1 AND is_deleted = 0
ORDER BY logical_id
"""

STAGING_SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE cal(
    bed_id TEXT NOT NULL, ward_code TEXT NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL, line INTEGER NOT NULL
);
CREATE INDEX idx_cal_bed ON cal(bed_id);
CREATE TABLE calu(
    bed_id TEXT NOT NULL, ward_code TEXT NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL, lines TEXT NOT NULL
);
CREATE INDEX idx_calu_bed ON calu(bed_id);
CREATE TABLE occ(
    bed_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    enc_id TEXT NOT NULL, version_id TEXT NOT NULL, seq INTEGER NOT NULL,
    location_index INTEGER NOT NULL, status TEXT NOT NULL,
    orig_start INTEGER NOT NULL, orig_end INTEGER
);
CREATE INDEX idx_occ_bed ON occ(bed_id);
CREATE TABLE agg(
    local_date TEXT NOT NULL, ward_code TEXT NOT NULL,
    kind TEXT NOT NULL, seconds INTEGER NOT NULL
);
"""

FACTS_SQL = """
SELECT local_date, ward_code,
       SUM(seconds) AS open_s,
       SUM(CASE WHEN kind='empty' THEN seconds ELSE 0 END) AS empty_s,
       SUM(CASE WHEN kind='single' THEN seconds ELSE 0 END) AS single_s,
       SUM(CASE WHEN kind='conflict' THEN seconds ELSE 0 END) AS conflict_s
FROM agg GROUP BY local_date, ward_code
ORDER BY local_date, ward_code
"""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(text: str) -> int:
    """Epoch seconds for a second-precision timestamp with a UTC offset."""

    if not isinstance(text, str) or not TS_RE.match(text):
        raise ValueError(
            f"expected a second-precision timestamp with timezone offset, "
            f"got {text!r}")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return int(datetime.fromisoformat(text).timestamp())


def iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def report_tz(tz_name: str):
    if tz_name == "UTC":
        return timezone.utc
    if tz_name == "America/New_York":
        return ZoneInfo("America/New_York")
    raise UsageError(
        f"unsupported report timezone {tz_name!r}; "
        f"supported: {', '.join(SUPPORTED_TIMEZONES)}")


def day_bounds(tz_name: str, date_from: str, date_to: str) -> list[tuple[str, int, int]]:
    """Local dates of the window as (iso_date, start_epoch, end_epoch)."""

    if not DATE_RE.match(date_from or "") or not DATE_RE.match(date_to or ""):
        raise UsageError("dates must be YYYY-MM-DD")
    d0 = date.fromisoformat(date_from)
    d1 = date.fromisoformat(date_to)
    if not d0 < d1:
        raise UsageError(
            f"date range must be a non-empty [from, to) interval, got "
            f"{date_from}..{date_to}")
    tz = report_tz(tz_name)
    days: list[tuple[str, int, int]] = []
    d = d0
    while d < d1:
        start = datetime.combine(d, time.min, tzinfo=tz)
        end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz)
        days.append((d.isoformat(), int(start.timestamp()), int(end.timestamp())))
        d += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Identity digests (shared with the loader; part of the file contract)
# ---------------------------------------------------------------------------

def series_id(source_server: str, tenant: str, tz_name: str,
              date_from: str, date_to: str) -> str:
    payload = json.dumps(
        {"format": FORMAT, "source_server": source_server, "tenant": tenant,
         "timezone": tz_name, "date_from": date_from, "date_to": date_to},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def facts_line(local_date: str, ward_code: str, open_s: int, empty_s: int,
               single_s: int, conflict_s: int) -> str:
    """Canonical identity line for one fact row (pipe-separated)."""

    return (f"{local_date}|{ward_code}|{open_s}|{empty_s}|"
            f"{single_s}|{conflict_s}\n")


def facts_digest_of(rows: Iterator[dict[str, Any]]) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    for r in rows:
        h.update(facts_line(r["local_date"], r["ward_code"], r["open_seconds"],
                            r["empty_seconds"], r["single_seconds"],
                            r["conflict_seconds"]).encode("utf-8"))
        n += 1
    return h.hexdigest(), n


def release_id_of(manifest: dict[str, Any]) -> str:
    """Content identity of a release: everything except the seq label."""

    core = json.loads(json.dumps(manifest))
    core["release"].pop("seq", None)
    core["release"].pop("release_id", None)
    payload = json.dumps(core, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Release directories
# ---------------------------------------------------------------------------

def _write_row(fh, row: dict[str, Any]) -> None:
    fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
    fh.write("\n")


def _safe_join(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    root_r = root.resolve()
    if p != root_r and root_r not in p.parents:
        raise ReleaseError(f"path escapes release directory: {rel!r}")
    return p


def load_release_manifest(release_dir: str | Path) -> dict[str, Any]:
    """Read and structurally validate a release manifest, binding id included."""

    release_dir = Path(release_dir)
    path = release_dir / "manifest.json"
    if not path.is_file():
        raise ReleaseError(f"no manifest.json in {release_dir}")
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"manifest.json is not valid JSON: {exc}") from exc
    _validate_manifest_shape(m, release_dir)
    if release_id_of(m) != m["release"]["release_id"]:
        raise ReleaseError(
            "release_id mismatch: manifest content was modified after publish")
    return m


def _require_hex64(value: Any, where: str) -> None:
    if not (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)):
        raise ReleaseError(f"{where} must be 64 hex chars, got {value!r}")


def _validate_manifest_shape(m: Any, release_dir: Path) -> None:
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise ReleaseError(f"{release_dir}: not a {FORMAT} manifest")
    series = m.get("series")
    if not isinstance(series, dict):
        raise ReleaseError("manifest.series missing")
    for key in ("source_server", "tenant", "timezone", "date_from", "date_to"):
        if not isinstance(series.get(key), str) or not series[key]:
            raise ReleaseError(f"manifest.series.{key} missing")
    if series["timezone"] not in SUPPORTED_TIMEZONES:
        raise ReleaseError(f"unsupported timezone {series['timezone']!r}")
    if series_id(series["source_server"], series["tenant"], series["timezone"],
                 series["date_from"], series["date_to"]) != series.get("series_id"):
        raise ReleaseError("manifest.series.series_id does not match coordinates")
    rel = m.get("release")
    if not isinstance(rel, dict):
        raise ReleaseError("manifest.release missing")
    if not isinstance(rel.get("seq"), int) or rel["seq"] < 1:
        raise ReleaseError("manifest.release.seq must be a positive integer")
    _require_hex64(rel.get("release_id"), "release.release_id")
    if rel.get("kind") not in ("initial", "correction"):
        raise ReleaseError("release.kind must be initial|correction")
    if not isinstance(rel.get("cut_seq"), int) or rel["cut_seq"] < 0:
        raise ReleaseError("release.cut_seq must be a non-negative integer")
    cal = rel.get("calendar")
    if not isinstance(cal, dict):
        raise ReleaseError("release.calendar missing")
    _require_hex64(cal.get("sha256"), "release.calendar.sha256")
    base = rel.get("base")
    if rel["kind"] == "initial":
        if base is not None:
            raise ReleaseError("initial release must not name a base")
    else:
        if not isinstance(base, dict):
            raise ReleaseError("correction release must name a base")
        _require_hex64(base.get("release_id"), "release.base.release_id")
        _require_hex64(base.get("facts_sha256"), "release.base.facts_sha256")
    target = rel.get("target")
    if not isinstance(target, dict):
        raise ReleaseError("release.target missing")
    _require_hex64(target.get("facts_sha256"), "release.target.facts_sha256")
    if not isinstance(target.get("fact_rows"), int) or target["fact_rows"] < 0:
        raise ReleaseError("release.target.fact_rows must be a non-negative int")
    for section in ("delivery", "evidence"):
        files = m.get(section, {}).get("files")
        if not isinstance(files, list) or not files:
            raise ReleaseError(f"manifest.{section}.files missing")
        for f in files:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                raise ReleaseError(f"manifest.{section}.files: bad entry {f!r}")
            _require_hex64(f.get("sha256"), f"{section} file sha256")
            if not isinstance(f.get("role"), str) or not f["role"]:
                raise ReleaseError(f"{section} file role missing")
            if f.get("rows") is not None and not isinstance(f["rows"], int):
                raise ReleaseError(f"{section} file rows must be int or null")


def verify_release_files(release_dir: str | Path,
                         manifest: dict[str, Any]) -> list[str]:
    """Recompute every listed file digest/row count; return problems."""

    release_dir = Path(release_dir)
    problems: list[str] = []
    files = (manifest["delivery"]["files"] + manifest["evidence"]["files"])
    for entry in files:
        rel = entry["path"]
        try:
            p = _safe_join(release_dir, rel)
        except ReleaseError as exc:
            problems.append(str(exc))
            continue
        if not p.is_file():
            problems.append(f"missing file: {rel}")
            continue
        digest, count = stream_jsonl_digest_and_count(p)
        if digest != entry["sha256"]:
            problems.append(f"digest mismatch: {rel}")
        if entry.get("rows") is not None and rel.endswith(".ndjson") \
                and count != entry["rows"]:
            problems.append(
                f"row count mismatch: {rel}: manifest {entry['rows']} "
                f"!= actual {count}")
    return problems


def verify_facts_file(release_dir: str | Path,
                      manifest: dict[str, Any]) -> list[str]:
    """Recompute the target facts identity from evidence/facts-full.ndjson."""

    release_dir = Path(release_dir)
    problems: list[str] = []
    path = release_dir / "evidence" / "facts-full.ndjson"
    if not path.is_file():
        return ["missing file: evidence/facts-full.ndjson"]
    try:
        digest, count = facts_digest_of(iter_jsonl(path))
    except FHError as exc:
        return [f"facts-full unreadable: {exc}"]
    target = manifest["release"]["target"]
    if digest != target["facts_sha256"]:
        problems.append("facts-full content does not match target.facts_sha256")
    if count != target["fact_rows"]:
        problems.append(
            f"facts-full rows {count} != target.fact_rows {target['fact_rows']}")
    return problems


def scan_releases(series_dir: Path) -> tuple[list[tuple[Path, dict]], list[str]]:
    """Valid (dir, manifest) pairs by seq, plus names of unreadable dirs."""

    valid: list[tuple[Path, dict]] = []
    broken: list[str] = []
    if series_dir.is_dir():
        for d in sorted(series_dir.iterdir()):
            if not d.is_dir() or not RELEASE_DIR_RE.match(d.name):
                continue
            try:
                valid.append((d, load_release_manifest(d)))
            except ReleaseError:
                broken.append(d.name)
    valid.sort(key=lambda dm: dm[1]["release"]["seq"])
    return valid, broken


def list_reports(reports_root: str | Path) -> dict[str, Any]:
    root = Path(reports_root)
    series_out: list[dict[str, Any]] = []
    if root.is_dir():
        for sdir in sorted(root.glob("series-*")):
            if not sdir.is_dir():
                continue
            valid, broken = scan_releases(sdir)
            if not valid and not broken:
                continue
            entry: dict[str, Any] = {"series_dir": sdir.name,
                                     "broken_dirs": broken, "releases": []}
            if valid:
                s = valid[0][1]["series"]
                entry.update({k: s[k] for k in
                              ("series_id", "source_server", "tenant",
                               "timezone", "date_from", "date_to")})
            for d, m in valid:
                rel = m["release"]
                entry["releases"].append({
                    "seq": rel["seq"],
                    "release_id": rel["release_id"],
                    "kind": rel["kind"],
                    "cut_seq": rel["cut_seq"],
                    "calendar_sha256": rel["calendar"]["sha256"],
                    "base_release_id":
                        rel["base"]["release_id"] if rel["base"] else None,
                    "facts_sha256": rel["target"]["facts_sha256"],
                    "fact_rows": rel["target"]["fact_rows"],
                    "dir": d.name,
                })
            series_out.append(entry)
    return {"status": "ok", "reports_root": str(root), "series": series_out}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def parse_calendar(csv_path: str | Path, st: sqlite3.Connection) -> dict[str, Any]:
    """Stream the calendar CSV into the staging table; structural checks only."""

    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise BedCalendarError(f"calendar file not found: {csv_path}")
    digest = stream_file_digest(csv_path)
    rows = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        names = {(f or "").strip() for f in (reader.fieldnames or [])}
        if not {"bed_id", "ward_code", "start", "end"} <= names:
            raise BedCalendarError(
                "calendar CSV header must contain bed_id, ward_code, start, end")
        for rec in reader:
            norm = {(k or "").strip(): v for k, v in rec.items()}
            rows += 1
            line = reader.line_num
            bed = (norm.get("bed_id") or "").strip()
            ward = (norm.get("ward_code") or "").strip()
            start_raw = (norm.get("start") or "").strip()
            end_raw = (norm.get("end") or "").strip()
            if not bed or not ward:
                raise BedCalendarError(
                    f"calendar line {line}: bed_id and ward_code are required")
            try:
                s = parse_ts(start_raw)
                e = parse_ts(end_raw)
            except ValueError as exc:
                raise BedCalendarError(f"calendar line {line}: {exc}") from exc
            if e <= s:
                raise BedCalendarError(
                    f"calendar line {line}: empty or reversed interval "
                    f"[{start_raw}, {end_raw})")
            st.execute("INSERT INTO cal VALUES (?,?,?,?,?)",
                       (bed, ward, s, e, line))
    return {"sha256": digest, "rows": rows}


def _union_intervals(rows: list[tuple[int, int, int]]) -> list[tuple[int, int, tuple]]:
    """Merge overlapping/touching same-ward intervals; keep source lines."""

    out: list[list] = []
    for s, e, line in sorted(rows):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
            out[-1][2] = out[-1][2] + (line,)
        else:
            out.append([s, e, (line,)])
    return [(s, e, lines) for s, e, lines in out]


def _build_calendar_unions(st: sqlite3.Connection) -> None:
    for (bed,) in st.execute("SELECT DISTINCT bed_id FROM cal ORDER BY bed_id"):
        rows = st.execute(
            "SELECT ward_code, start, end, line FROM cal WHERE bed_id=? "
            "ORDER BY ward_code, start, line", (bed,)).fetchall()
        by_ward: dict[str, list[tuple[int, int, int]]] = {}
        for r in rows:
            by_ward.setdefault(r["ward_code"], []).append(
                (r["start"], r["end"], r["line"]))
        for ward, ivs in by_ward.items():
            for s, e, lines in _union_intervals(ivs):
                st.execute("INSERT INTO calu VALUES (?,?,?,?,?)",
                           (bed, ward, s, e, json.dumps(sorted(lines))))


def _validate_calendar_wards(st: sqlite3.Connection) -> None:
    """Reject the whole calendar if one bed is open under two wards at once."""

    problems: list[str] = []
    for (bed,) in st.execute("SELECT DISTINCT bed_id FROM calu ORDER BY bed_id"):
        rows = st.execute(
            "SELECT ward_code, start, end FROM calu WHERE bed_id=? "
            "ORDER BY start", (bed,)).fetchall()
        active: list[tuple[int, str]] = []
        for r in rows:
            for e, w in active:
                if e > r["start"] and w != r["ward_code"]:
                    problems.append(
                        f"bed {bed}: wards {w} and {r['ward_code']} overlap at "
                        f"{iso_utc(r['start'])}")
            active = [(e, w) for e, w in active if e > r["start"]]
            active.append((r["end"], r["ward_code"]))
    if problems:
        raise BedCalendarError(
            "calendar opens one bed under two wards at the same time: "
            + "; ".join(problems[:10]))


def _check_calendar_beds(ro: sqlite3.Connection, source_id: int, cut_seq: int,
                         beds: list[str]) -> None:
    missing = []
    for bed in beds:
        row = latest_version_at(ro, source_id, "Location", bed, cut_seq)
        if row is None or row["is_deleted"]:
            missing.append(bed)
    if missing:
        raise CalendarBedMissing(
            f"calendar beds not resolvable as current Location at cut "
            f"{cut_seq}: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Occupancy extraction at the cut
# ---------------------------------------------------------------------------

def _extract_occupancy(paths: RepoPaths, ro: sqlite3.Connection,
                       config: RepoConfig, source: sqlite3.Row, cut_seq: int,
                       window: tuple[int, int], st: sqlite3.Connection,
                       exc_fh, summary: dict[str, int]) -> None:
    alias_bases = config.sources[source["source_server"]]["aliases"]
    source_id = source["source_id"]
    loc_cache: dict[str, bool] = {}
    for row in ro.execute(ENCOUNTER_SQL, (source_id, cut_seq)):
        body = blobs.read_blob(paths, row["body_relpath"])
        summary["encounters_scanned"] += 1
        locs = body.get("location")
        if not isinstance(locs, list):
            continue
        enc = {"logical_id": row["logical_id"], "version_id": row["version_id"],
               "seq": row["seq"]}
        for idx, entry in enumerate(locs):
            summary["location_entries"] += 1
            _extract_entry(ro, source_id, alias_bases, loc_cache, enc, idx,
                           entry, cut_seq, window, st, exc_fh, summary)


def _extract_entry(ro, source_id, alias_bases, loc_cache, enc, idx, entry,
                   cut_seq, window, st, exc_fh, summary) -> None:

    def exc(kind: str, detail: str, ref_text: str | None = None) -> None:
        summary["exceptions"] += 1
        row = {"encounter_id": enc["logical_id"], "version_id": enc["version_id"],
               "seq": enc["seq"], "location_index": idx, "kind": kind,
               "detail": detail}
        if ref_text:
            row["ref_text"] = ref_text
        _write_row(exc_fh, row)

    if not isinstance(entry, dict):
        exc("location_entry_invalid", "location entry is not an object")
        return
    status = entry.get("status")
    if status not in ("active", "completed"):
        summary["location_entries_ignored_status"] += 1
        return
    ref = entry.get("location")
    ref_text = ref.get("reference") if isinstance(ref, dict) else None
    shape = classify_ref(ref_text if isinstance(ref_text, str) else "",
                         alias_bases)
    if shape.kind not in ("relative", "absolute") \
            or shape.target_type != "Location" or shape.versioned:
        if shape.kind in ("relative", "absolute") and shape.versioned:
            exc("location_ref_versioned",
                "versioned location references are not supported", ref_text)
        elif shape.kind in ("relative", "absolute"):
            exc("location_ref_not_location",
                f"reference points at {shape.target_type}, not Location",
                ref_text)
        elif shape.kind == "external":
            exc("location_ref_external",
                "absolute URL is outside the configured bases", ref_text)
        else:
            exc("location_ref_invalid", shape.reason or "unusable reference",
                ref_text)
        return
    bed = shape.target_id
    if bed not in loc_cache:
        lrow = latest_version_at(ro, source_id, "Location", bed, cut_seq)
        if lrow is None:
            loc_cache[bed] = f"Location/{bed} does not exist at the cut"
        elif lrow["is_deleted"]:
            loc_cache[bed] = f"Location/{bed} is a tombstone at the cut"
        else:
            loc_cache[bed] = True  # type: ignore[assignment]
    if loc_cache[bed] is not True:
        exc("location_unresolvable", loc_cache[bed], ref_text)
        return
    period = entry.get("period")
    period = period if isinstance(period, dict) else {}
    start_raw, end_raw = period.get("start"), period.get("end")
    if start_raw is None:
        exc("period_missing_start", "location period has no start", ref_text)
        return
    try:
        s = parse_ts(start_raw)
    except ValueError as exc_msg:
        exc("period_bad_timestamp", str(exc_msg), ref_text)
        return
    if end_raw is None:
        if status == "completed":
            exc("period_missing_end",
                "completed location period has no end", ref_text)
            return
        e = window[1]  # active without end: open until the window ends
        orig_end = None
        if e <= s:
            return  # starts at/after the window end: nothing to count
    else:
        try:
            e = parse_ts(end_raw)
        except ValueError as exc_msg:
            exc("period_bad_timestamp", str(exc_msg), ref_text)
            return
        orig_end = e
        if e <= s:
            exc("period_reversed_or_empty",
                f"period start {start_raw} is not before end {end_raw}",
                ref_text)
            return
    cs, ce = max(s, window[0]), min(e, window[1])
    if ce <= cs:
        return  # outside the report window: nothing to count, not an error
    st.execute("INSERT INTO occ VALUES (?,?,?,?,?,?,?,?,?,?)",
               (bed, cs, ce, enc["logical_id"], enc["version_id"], enc["seq"],
                idx, status, s, orig_end))
    summary["occupancy_intervals"] += 1


# ---------------------------------------------------------------------------
# Per-bed sweep: slices, unassigned diagnostics, day-fact aggregation
# ---------------------------------------------------------------------------

def _cut_days(a: int, b: int, days: list[tuple[str, int, int]]):
    for iso, ds, de in days:
        if de <= a:
            continue
        if ds >= b:
            break
        yield iso, max(a, ds), min(b, de)


def _occupants(occs: tuple) -> list[dict[str, Any]]:
    by_enc: dict[tuple, list] = {}
    for o in occs:
        by_enc.setdefault((o[2], o[3], o[4]), []).append(o)
    out = []
    for (enc_id, vid, seq), entries in sorted(by_enc.items()):
        out.append({
            "encounter_id": enc_id, "version_id": vid, "seq": seq,
            "entries": [
                {"location_index": o[5], "status": o[6],
                 "period_start_utc": iso_utc(o[7]),
                 "period_end_utc": iso_utc(o[8]) if o[8] is not None else None}
                for o in sorted(entries, key=lambda x: x[5])
            ],
        })
    return out


def _merge_segments(segs: list[tuple]) -> list[tuple]:
    """Merge adjacent same-day segments with identical attribution."""

    out: list[list] = []
    for s in segs:
        d, a, b, ward, lines, occs = s
        sig = (d, ward, lines, tuple(sorted(
            (o[2], o[3], o[4], o[5]) for o in occs)))
        if out and out[-1][0] == sig and out[-1][1][2] == a:
            out[-1][1][2] = b
        else:
            out.append([sig, [d, a, b, ward, lines, occs]])
    return [tuple(item[1]) for item in out]


def _sweep_bed(bed: str, open_iv: list[tuple], occ_iv: list[tuple],
               days: list[tuple[str, int, int]], slices_fh, unassigned_fh,
               st: sqlite3.Connection, summary: dict[str, int]) -> None:
    points = sorted({p for iv in open_iv for p in (iv[0], iv[1])}
                    | {p for iv in occ_iv for p in (iv[0], iv[1])})
    oi = sorted(open_iv, key=lambda x: (x[0], x[1]))
    oc = sorted(occ_iv, key=lambda x: (x[0], x[1]))
    act_o: list[tuple] = []
    act_c: list[tuple] = []
    i = j = 0
    raw: list[tuple] = []
    for a, b in zip(points, points[1:]):
        while i < len(oi) and oi[i][0] <= a:
            if oi[i][1] > a:
                act_o.append(oi[i])
            i += 1
        while j < len(oc) and oc[j][0] <= a:
            if oc[j][1] > a:
                act_c.append(oc[j])
            j += 1
        act_o = [x for x in act_o if x[1] > a]
        act_c = [x for x in act_c if x[1] > a]
        if not act_o and not act_c:
            continue
        ward = act_o[0][2] if act_o else None
        lines = tuple(sorted({ln for x in act_o for ln in x[3]})) if act_o else ()
        raw.append((a, b, ward, lines, tuple(act_c)))
    day_segments = []
    for a, b, ward, lines, occs in raw:
        for d_iso, lo, hi in _cut_days(a, b, days):
            day_segments.append((d_iso, lo, hi, ward, lines, occs))
    for d_iso, a, b, ward, lines, occs in _merge_segments(day_segments):
        seconds = b - a
        if ward is None:
            if not occs:
                continue
            _write_row(unassigned_fh, {
                "bed_id": bed, "local_date": d_iso,
                "start_utc": iso_utc(a), "end_utc": iso_utc(b),
                "seconds": seconds,
                "reason": "occupancy_outside_open_calendar",
                "occupants": _occupants(occs)})
            summary["unassigned_intervals"] += 1
            summary["unassigned_seconds"] += seconds
            continue
        n = len({(o[2], o[3], o[4]) for o in occs})
        kind = "empty" if n == 0 else "single" if n == 1 else "conflict"
        st.execute("INSERT INTO agg VALUES (?,?,?,?)",
                   (d_iso, ward, kind, seconds))
        summary[f"{kind}_seconds"] += seconds
        summary["open_seconds"] += seconds
        _write_row(slices_fh, {
            "local_date": d_iso, "ward_code": ward, "bed_id": bed,
            "start_utc": iso_utc(a), "end_utc": iso_utc(b),
            "seconds": seconds, "kind": kind,
            "calendar_lines": list(lines), "occupants": _occupants(occs)})
        summary["slices"] += 1


def _sweep_all(st: sqlite3.Connection, days: list[tuple[str, int, int]],
               window: tuple[int, int], slices_fh, unassigned_fh,
               summary: dict[str, int]) -> None:
    beds = st.execute(
        "SELECT bed_id FROM (SELECT bed_id FROM calu "
        "UNION SELECT bed_id FROM occ) ORDER BY bed_id")
    for (bed,) in beds:
        open_iv = []
        for r in st.execute(
                "SELECT start, end, ward_code, lines FROM calu "
                "WHERE bed_id=? AND end>? AND start<? ORDER BY start",
                (bed, window[0], window[1])):
            open_iv.append((max(r["start"], window[0]),
                            min(r["end"], window[1]),
                            r["ward_code"], tuple(json.loads(r["lines"]))))
        occ_iv = [tuple(r) for r in st.execute(
            "SELECT start, end, enc_id, version_id, seq, location_index, "
            "status, orig_start, orig_end FROM occ WHERE bed_id=? "
            "ORDER BY start, enc_id, location_index", (bed,))]
        _sweep_bed(bed, open_iv, occ_iv, days, slices_fh, unassigned_fh,
                   st, summary)


# ---------------------------------------------------------------------------
# Facts and diffs
# ---------------------------------------------------------------------------

def _write_facts(st: sqlite3.Connection, path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in st.execute(FACTS_SQL):
            row = {"local_date": r["local_date"], "ward_code": r["ward_code"],
                   "open_seconds": r["open_s"], "empty_seconds": r["empty_s"],
                   "single_seconds": r["single_s"],
                   "conflict_seconds": r["conflict_s"]}
            _write_row(fh, row)
            h.update(facts_line(
                row["local_date"], row["ward_code"], row["open_seconds"],
                row["empty_seconds"], row["single_seconds"],
                row["conflict_seconds"]).encode("utf-8"))
            n += 1
    return h.hexdigest(), n


def _fact_key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["local_date"], row["ward_code"])


def _diff_rows(base_rows: Iterator[dict], new_rows: Iterator[dict]):
    """Exact row-level diff of two key-sorted fact streams."""

    a = next(base_rows, None)
    b = next(new_rows, None)
    while a is not None or b is not None:
        if a is None:
            yield ("upsert", b)
            b = next(new_rows, None)
        elif b is None:
            yield ("retract", {k: a[k] for k in ("local_date", "ward_code")})
            a = next(base_rows, None)
        else:
            ka, kb = _fact_key(a), _fact_key(b)
            if ka == kb:
                if a != b:
                    yield ("upsert", b)
                a = next(base_rows, None)
                b = next(new_rows, None)
            elif ka < kb:
                yield ("retract", {k: a[k] for k in ("local_date", "ward_code")})
                a = next(base_rows, None)
            else:
                yield ("upsert", b)
                b = next(new_rows, None)


def _write_delivery(kind: str, tmp: Path, facts_path: Path,
                    base_facts_path: Path | None) -> tuple[list[dict], dict[str, int]]:
    ddir = tmp / "delivery"
    ddir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    if kind == "initial":
        shutil.copyfile(facts_path, ddir / "facts-full.ndjson")
        digest, rows = stream_jsonl_digest_and_count(ddir / "facts-full.ndjson")
        files.append({"path": "delivery/facts-full.ndjson", "sha256": digest,
                      "rows": rows, "role": "full"})
        counts["full_rows"] = rows
        return files, counts
    upserts = retracts = 0
    up_path, re_path = ddir / "facts-upsert.ndjson", ddir / "facts-retract.ndjson"
    with open(up_path, "w", encoding="utf-8") as up, \
            open(re_path, "w", encoding="utf-8") as re:
        for tag, row in _diff_rows(iter_jsonl(base_facts_path),
                                   iter_jsonl(facts_path)):
            if tag == "upsert":
                _write_row(up, row)
                upserts += 1
            else:
                _write_row(re, row)
                retracts += 1
    for path, role, rows in ((up_path, "upsert", upserts),
                             (re_path, "retract", retracts)):
        digest, _ = stream_jsonl_digest_and_count(path)
        files.append({"path": f"delivery/{path.name}", "sha256": digest,
                      "rows": rows, "role": role})
    counts["upsert_rows"] = upserts
    counts["retract_rows"] = retracts
    return files, counts


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

def _clean_stale_tmp(series_dir: Path) -> None:
    if series_dir.is_dir():
        for d in series_dir.iterdir():
            if d.is_dir() and d.name.startswith(".tmp-"):
                shutil.rmtree(d, ignore_errors=True)


def _resolve_base(series_dir: Path, releases: list[tuple[Path, dict]],
                  ref: str) -> tuple[Path, dict[str, Any]]:
    p = Path(ref)
    if p.is_dir():
        return p, load_release_manifest(p)
    for d, m in releases:
        rel = m["release"]
        if ref in (str(rel["seq"]), rel["release_id"], rel["release_id"][:12]):
            return d, m
    raise UnknownRelease(
        f"no release {ref!r} in series {series_dir}; pass a release "
        "directory, a seq number or a release id")


def publish(paths: RepoPaths, conn: sqlite3.Connection, config: RepoConfig,
            *, source_server: str, tenant: str, cut_seq: int, tz_name: str,
            date_from: str, date_to: str, calendar_path: str,
            base_release_ref: str | None,
            reports_root: str | None) -> dict[str, Any]:
    days = day_bounds(tz_name, date_from, date_to)
    window = (days[0][1], days[-1][2])
    source = load_source(conn, source_server, tenant)
    sid = series_id(source_server, tenant, tz_name, date_from, date_to)
    root = Path(reports_root).resolve() if reports_root \
        else paths.root / "bedreports"
    series_dir = root / f"series-{sid[:16]}"
    series_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale_tmp(series_dir)
    releases, broken = scan_releases(series_dir)

    base_dir: Path | None = None
    base_manifest: dict[str, Any] | None = None
    if base_release_ref is None:
        if releases:
            raise UsageError(
                "series already has releases; corrections must name "
                "--base-release (a release dir, seq or id)")
    else:
        base_dir, base_manifest = _resolve_base(series_dir, releases,
                                                base_release_ref)
        if base_manifest["series"]["series_id"] != sid:
            raise SeriesMismatch(
                "base release belongs to a different series "
                f"({base_manifest['series']['series_id'][:16]}… != {sid[:16]}…)")
        if cut_seq < base_manifest["release"]["cut_seq"]:
            raise UsageError(
                f"correction cut {cut_seq} predates base cut "
                f"{base_manifest['release']['cut_seq']}")
        problems = verify_release_files(base_dir, base_manifest) \
            + verify_facts_file(base_dir, base_manifest)
        if problems:
            raise ReleaseError(
                "base release failed verification: " + "; ".join(problems))
    kind = "initial" if base_manifest is None else "correction"

    tmp = series_dir / f".tmp-{os.getpid()}-{len(releases) + 1:04d}"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "evidence").mkdir(parents=True)
    st = sqlite3.connect(str(tmp / "staging.sqlite3"))
    st.row_factory = sqlite3.Row
    st.executescript(STAGING_SCHEMA)
    summary: dict[str, int] = {
        "encounters_scanned": 0, "location_entries": 0,
        "location_entries_ignored_status": 0, "exceptions": 0,
        "occupancy_intervals": 0, "slices": 0,
        "open_seconds": 0, "empty_seconds": 0, "single_seconds": 0,
        "conflict_seconds": 0, "unassigned_intervals": 0,
        "unassigned_seconds": 0,
    }
    try:
        cal_stats = parse_calendar(calendar_path, st)
        _build_calendar_unions(st)
        _validate_calendar_wards(st)
        cal_beds = [r["bed_id"] for r in
                    st.execute("SELECT DISTINCT bed_id FROM cal ORDER BY bed_id")]

        with snapshot_tx(conn) as ro:
            check_cut(ro, source, cut_seq)
            _check_calendar_beds(ro, source["source_id"], cut_seq, cal_beds)
            with open(tmp / "evidence" / "exceptions.ndjson", "w",
                      encoding="utf-8") as exc_fh:
                _extract_occupancy(paths, ro, config, source, cut_seq, window,
                                   st, exc_fh, summary)

        with open(tmp / "evidence" / "slices.ndjson", "w",
                  encoding="utf-8") as slices_fh, \
                open(tmp / "evidence" / "unassigned.ndjson", "w",
                     encoding="utf-8") as unassigned_fh:
            _sweep_all(st, days, window, slices_fh, unassigned_fh, summary)

        facts_digest, fact_rows = _write_facts(
            st, tmp / "evidence" / "facts-full.ndjson")
        summary["fact_rows"] = fact_rows

        base_facts = (base_dir / "evidence" / "facts-full.ndjson"
                      if base_dir else None)
        delivery_files, delivery_counts = _write_delivery(
            kind, tmp, tmp / "evidence" / "facts-full.ndjson", base_facts)

        shutil.copyfile(calendar_path, tmp / "evidence" / "calendar.csv")
        inputs = {
            "format": FORMAT, "source_server": source_server,
            "tenant": tenant, "cut_seq": cut_seq, "timezone": tz_name,
            "date_from": date_from, "date_to": date_to,
            "calendar_sha256": cal_stats["sha256"],
            "calendar_rows": cal_stats["rows"],
            "calendar_beds": len(cal_beds),
            "base_release_id": (base_manifest["release"]["release_id"]
                                if base_manifest else None),
        }
        (tmp / "evidence" / "inputs.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")

        evidence_files = []
        for name, role, rows in (
                ("facts-full.ndjson", "facts-full", fact_rows),
                ("slices.ndjson", "slices", summary["slices"]),
                ("exceptions.ndjson", "exceptions", summary["exceptions"]),
                ("unassigned.ndjson", "unassigned",
                 summary["unassigned_intervals"]),
                ("calendar.csv", "calendar", cal_stats["rows"]),
                ("inputs.json", "inputs", None)):
            digest = stream_file_digest(tmp / "evidence" / name)
            evidence_files.append({"path": f"evidence/{name}",
                                   "sha256": digest, "rows": rows,
                                   "role": role})

        manifest: dict[str, Any] = {
            "format": FORMAT,
            "series": {
                "source_server": source_server, "tenant": tenant,
                "timezone": tz_name, "date_from": date_from,
                "date_to": date_to, "series_id": sid,
            },
            "release": {
                "seq": None, "release_id": None, "kind": kind,
                "cut_seq": cut_seq,
                "calendar": {"sha256": cal_stats["sha256"],
                             "rows": cal_stats["rows"],
                             "beds": len(cal_beds)},
                "base": ({
                    "release_id": base_manifest["release"]["release_id"],
                    "seq": base_manifest["release"]["seq"],
                    "facts_sha256":
                        base_manifest["release"]["target"]["facts_sha256"],
                } if base_manifest else None),
                "target": {"facts_sha256": facts_digest,
                           "fact_rows": fact_rows},
                "window": {"start_utc": iso_utc(window[0]),
                           "end_utc": iso_utc(window[1]),
                           "days": len(days)},
            },
            "delivery": {"kind": kind, "files": delivery_files,
                         **delivery_counts},
            "evidence": {"files": evidence_files},
            "summary": summary,
        }
        rid = release_id_of(manifest)

        # Identical inputs re-run: fold onto the existing release.
        for d, m in releases:
            if m["release"]["release_id"] == rid:
                st.close()
                shutil.rmtree(tmp, ignore_errors=True)
                return _result("folded", sid, d, m["release"], summary,
                               delivery_counts)
        used = {m["release"]["seq"] for _, m in releases}
        for d in series_dir.iterdir():
            mm = RELEASE_DIR_RE.match(d.name)
            if mm:
                used.add(int(mm.group(1)))
        seq = max(used, default=0) + 1
        manifest["release"]["seq"] = seq
        manifest["release"]["release_id"] = rid
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")
        st.close()
        for leftover in tmp.glob("staging.sqlite3*"):
            leftover.unlink()
        final = series_dir / f"rel-{seq:04d}-{rid[:12]}"
        for _attempt in range(4):
            try:
                os.rename(tmp, final)
                break
            except OSError:
                releases, _ = scan_releases(series_dir)
                clash = [(d, m) for d, m in releases
                         if m["release"]["release_id"] == rid]
                if clash:
                    st.close()
                    shutil.rmtree(tmp, ignore_errors=True)
                    d, m = clash[0]
                    return _result("folded", sid, d, m["release"], summary,
                                   delivery_counts)
                seq = max([m["release"]["seq"] for _, m in releases],
                          default=0) + 1
                manifest["release"]["seq"] = seq
                (tmp / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")
                final = series_dir / f"rel-{seq:04d}-{rid[:12]}"
        else:
            raise FHError("could not finalise the release directory")
        return _result("published", sid, final, manifest["release"], summary,
                       delivery_counts)
    except Exception:
        st.close()
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _result(status: str, sid: str, release_dir: Path, rel: dict[str, Any],
            summary: dict[str, int],
            delivery_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "status": status,
        "series_id": sid,
        "release_seq": rel["seq"],
        "release_id": rel["release_id"],
        "kind": rel["kind"],
        "release_dir": str(release_dir),
        "cut_seq": rel["cut_seq"],
        "calendar_sha256": rel["calendar"]["sha256"],
        "base_release_id": (rel["base"]["release_id"]
                            if rel.get("base") else None),
        "facts_sha256": rel["target"]["facts_sha256"],
        "fact_rows": rel["target"]["fact_rows"],
        "delivery": delivery_counts,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Evidence access (audit localisation)
# ---------------------------------------------------------------------------

SECTION_FILES = {
    "slices": "evidence/slices.ndjson",
    "exceptions": "evidence/exceptions.ndjson",
    "unassigned": "evidence/unassigned.ndjson",
    "facts": "evidence/facts-full.ndjson",
}


def read_evidence(release_dir: str | Path, section: str, *,
                  ward: str | None = None, date: str | None = None,
                  bed: str | None = None, kind: str | None = None,
                  limit: int = 1000) -> dict[str, Any]:
    release_dir = Path(release_dir)
    manifest = load_release_manifest(release_dir)
    if section not in SECTION_FILES:
        raise UsageError(f"unknown evidence section {section!r}")
    path = release_dir / SECTION_FILES[section]
    rows: list[dict[str, Any]] = []
    matched = 0
    if path.is_file():
        for row in iter_jsonl(path):
            if ward is not None and row.get("ward_code") != ward:
                continue
            if date is not None and row.get("local_date") != date:
                continue
            if bed is not None and row.get("bed_id") != bed:
                continue
            if kind is not None and row.get("kind") != kind:
                continue
            matched += 1
            if len(rows) < limit:
                rows.append(row)
    return {
        "status": "ok",
        "release": str(release_dir),
        "release_id": manifest["release"]["release_id"],
        "section": section,
        "matched": matched,
        "truncated": matched > len(rows),
        "rows": rows,
    }
