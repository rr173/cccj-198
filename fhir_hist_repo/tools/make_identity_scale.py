#!/usr/bin/env python3 -I
"""Generate a larger synthetic campus for the identity-dimension scale drill.

Creates one source (``echo``) with a configurable number of Patients arranged
in groups. The groups are deliberately simple so the drill can compare the
CLI with an independent engine without enumerating every key.

All data is invented; no patient matching is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fhist.jsonl import write_jsonl  # noqa: E402

BASE = "https://fhir.example.org/echo"
TENANT = "tenant-care"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def patient(pid: str, vid: str) -> dict:
    return {"resourceType": "Patient", "id": pid,
            "meta": {"versionId": vid, "lastUpdated": "2026-01-01T00:00:00Z"},
            "active": True,
            "name": [{"family": f"SYNTH-{pid}", "given": ["Synthetic"]}]}


def op(seq: int, ordinal: int, res: dict) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "PUT",
            "resourceType": "Patient", "id": res["id"],
            "versionId": res["meta"]["versionId"], "resource": res}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--groups", type=int, default=40)
    ap.add_argument("--per-group", type=int, default=5)
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        import shutil
        shutil.rmtree(out)
    rows: list[dict] = []
    ordinal = 0
    for g in range(args.groups):
        for m in range(args.per_group):
            pid = f"g{g:03d}p{m:02d}"
            rows.append(op(50, ordinal, patient(pid, "v1")))
            ordinal += 1
    rel, digest, count = None, None, None
    shard = out / "baseline" / "baseline.ndjson"
    shard.parent.mkdir(parents=True, exist_ok=True)
    count = write_jsonl(shard, iter(rows))
    rel = shard.relative_to(out).as_posix()
    digest = sha256_file(shard)
    doc = {"format_version": 1, "source_server": BASE, "tenant": TENANT,
           "aliases": [],
           "baseline": {"seq": 50, "path": rel, "sha256": digest,
                        "resource_count": count},
           "transactions": []}
    (out / "manifest.json").write_text(json.dumps(doc, indent=2),
                                      encoding="utf-8")
    print(json.dumps({"status": "ok", "source": BASE,
                      "patients": ordinal, "groups": args.groups}))


if __name__ == "__main__":
    main()
