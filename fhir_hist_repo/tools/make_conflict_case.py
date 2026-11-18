#!/usr/bin/env python3 -I
"""Build the conflict-visibility package pair.

First delivery (v1): baseline seq5 Patient/p v1 + Observation/h referencing
both unversioned Patient/p and versioned Patient/p/_history/v2; txn6 puts
Patient/p v2. Second delivery (v2): re-sends txn6 with a different body for
the same identity+versionId. After isolation, every edge resolving at v2
must report status ``conflict`` while the published cut stays readable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fhist.jsonl import write_jsonl  # noqa: E402

SERVER_A = "https://fhir.example.org/a"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def patient(pid: str, vid: str, active: bool = True) -> dict:
    return {"resourceType": "Patient", "id": pid,
            "meta": {"versionId": vid, "lastUpdated": "2026-03-01T00:00:00Z"},
            "active": active, "name": [{"family": f"SYNTHETIC-{pid}"}]}


def observation(oid: str, vid: str) -> dict:
    return {"resourceType": "Observation", "id": oid,
            "meta": {"versionId": vid, "lastUpdated": "2026-03-01T00:00:00Z"},
            "status": "final",
            "code": {"text": "synthetic"},
            "subject": {"reference": "Patient/p"},
            "focus": [{"reference": "Patient/p/_history/v2"}]}


def op_put(seq: int, ordinal: int, resource: dict) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "PUT",
            "resourceType": resource["resourceType"], "id": resource["id"],
            "versionId": resource["meta"]["versionId"], "resource": resource}


def write_manifest(d: Path, base_digest: str, base_count: int,
                   shard_digest: str, shard_count: int) -> None:
    doc = {
        "format_version": 1, "source_server": SERVER_A,
        "tenant": "tenant-conflict-visibility", "aliases": [],
        "baseline": {"seq": 5, "path": "baseline/baseline.ndjson",
                     "sha256": base_digest, "resource_count": base_count},
        "transactions": [{"seq": 6, "operation_count": shard_count, "shards": [
            {"ordinal": 0, "path": "shards/seq-6.ndjson",
             "sha256": shard_digest, "operation_count": shard_count}]}],
    }
    (d / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    fixtures = Path(sys.argv[1])
    d1 = fixtures / "case-conflict-visibility-v1"
    d2 = fixtures / "case-conflict-visibility-v2"
    for d in (d1, d2):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    base_rows = [op_put(5, 0, patient("p", "v1")),
                 op_put(5, 1, observation("h", "h1"))]
    n = write_jsonl(d1 / "baseline" / "baseline.ndjson", iter(base_rows))
    bd = sha256_file(d1 / "baseline" / "baseline.ndjson")

    good = [op_put(6, 0, patient("p", "v2", active=True))]
    n6 = write_jsonl(d1 / "shards" / "seq-6.ndjson", iter(good))
    sd = sha256_file(d1 / "shards" / "seq-6.ndjson")
    write_manifest(d1, bd, n, sd, n6)

    shutil.copytree(d1 / "baseline", d2 / "baseline")
    divergent = [op_put(6, 0, patient("p", "v2", active=False))]
    n6b = write_jsonl(d2 / "shards" / "seq-6.ndjson", iter(divergent))
    sdb = sha256_file(d2 / "shards" / "seq-6.ndjson")
    write_manifest(d2, bd, n, sdb, n6b)
    print(json.dumps({"status": "ok", "packages": [d1.name, d2.name]}))


if __name__ == "__main__":
    main()
