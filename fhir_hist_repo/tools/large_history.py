#!/usr/bin/env python3 -I
"""Generate a large synthetic history package without holding it in memory.

Rows are serialised and flushed one at a time. Each PUT resource can itself
be large (a generated ``text.div`` payload of the requested byte size), which
exercises the streaming path in both the generator and the ingest pipeline.

Usage:
  large_history.py OUT_DIR --resources 5000 --txns 20 --big-every 500 \
      --big-bytes 200000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

SERVER = "https://fhir.example.org/large"
TENANT = "tenant-large"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_resource(seq: int, ordinal: int, big: bool, big_bytes: int) -> dict:
    rid = f"r{ordinal:06d}"
    vid = f"v{seq}"
    res = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"versionId": vid,
                 "lastUpdated": "2026-09-01T00:00:00Z"},
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "29463-7"}]},
        "subject": {"reference": f"Patient/p{ordinal % 25:03d}"},
    }
    if big:
        # Deterministic, obviously-synthetic padding; written as part of the
        # single JSON line, so one line is big but never the whole package.
        pad = ("SYNTHETIC-PADDING-" + str(ordinal) + "-") * (
            big_bytes // 24 + 1)
        res["text"] = {"status": "generated",
                       "div": f"<div>{pad[:big_bytes]}</div>"}
    return res


def write_package(out: Path, resources: int, txns: int, big_every: int,
                  big_bytes: int, baseline_seq: int = 100) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    shard_dir = out / "shards"
    shard_dir.mkdir(exist_ok=True)

    def write_rows(path: Path, seq: int, start_ord: int,
                   count: int) -> tuple[str, str, int]:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ordinal in range(start_ord, start_ord + count):
                big = big_every and ordinal % big_every == 0
                res = make_resource(seq, ordinal, big, big_bytes)
                row = {"seq": seq, "ordinal": ordinal, "method": "PUT",
                       "resourceType": res["resourceType"], "id": res["id"],
                       "versionId": res["meta"]["versionId"], "resource": res}
                fh.write(json.dumps(row, ensure_ascii=False))
                fh.write("\n")
                n += 1
        rel = path.relative_to(out).as_posix()
        return rel, sha256_file(path), n

    # Baseline: all resources at version v100.
    b_path = out / "baseline" / "baseline.ndjson"
    b_path.parent.mkdir(parents=True, exist_ok=True)
    b_rel, b_digest, b_count = write_rows(b_path, baseline_seq, 0, resources)

    txn_entries = []
    for k in range(1, txns + 1):
        seq = baseline_seq + k
        path = shard_dir / f"seq-{seq}.ndjson"
        # Each txn re-puts 10% of the resources with a new version.
        step = max(1, resources // 10)
        ordinals = [(k + i * step) % resources for i in range(
            min(step, resources))]
        ordinals = sorted(set(ordinals))
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ordinal in ordinals:
                big = big_every and ordinal % big_every == 0
                res = make_resource(seq, ordinal, big, big_bytes)
                row = {"seq": seq, "ordinal": n, "method": "PUT",
                       "resourceType": res["resourceType"], "id": res["id"],
                       "versionId": res["meta"]["versionId"], "resource": res}
                fh.write(json.dumps(row, ensure_ascii=False))
                fh.write("\n")
                n += 1
        rel = path.relative_to(out).as_posix()
        txn_entries.append({
            "seq": seq, "operation_count": n,
            "shards": [{"ordinal": 0, "path": rel,
                        "sha256": sha256_file(path), "operation_count": n}],
        })

    manifest = {
        "format_version": 1, "source_server": SERVER, "tenant": TENANT,
        "aliases": [],
        "baseline": {"seq": baseline_seq, "path": b_rel,
                     "sha256": b_digest, "resource_count": b_count},
        "transactions": txn_entries,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return {"status": "ok", "dir": str(out), "baseline_resources": resources,
            "transactions": txns, "bytes": size}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--resources", type=int, default=3000)
    ap.add_argument("--txns", type=int, default=12)
    ap.add_argument("--big-every", type=int, default=500)
    ap.add_argument("--big-bytes", type=int, default=150_000)
    args = ap.parse_args()
    t0 = time.monotonic()
    info = write_package(Path(args.out), args.resources, args.txns,
                         args.big_every, args.big_bytes)
    info["elapsed_seconds"] = round(time.monotonic() - t0, 2)
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
