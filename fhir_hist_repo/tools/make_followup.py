#!/usr/bin/env python3 -I
"""Build a follow-up delivery for source A: seq 13 adds Patient/new1.

Used by the concurrency drill: a reader pins cut 12 while this transaction
is staged and advanced, proving the in-flight query never observes seq 13.
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
ALIAS_A = "https://fhir-alias.example.net/a"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def patient(pid: str, vid: str, refs=None) -> dict:
    res = {
        "resourceType": "Patient",
        "id": pid,
        "meta": {"versionId": vid, "lastUpdated": "2026-02-01T00:00:00Z"},
        "active": True,
        "name": [{"family": f"SYNTHETIC-{pid}"}],
    }
    if refs:
        res["link"] = refs
    return res


def op_put(seq: int, ordinal: int, resource: dict) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "PUT",
            "resourceType": resource["resourceType"], "id": resource["id"],
            "versionId": resource["meta"]["versionId"], "resource": resource}


def main() -> None:
    fixtures = Path(sys.argv[1])
    src = fixtures / "sourceA"
    out = fixtures / "sourceA-seq13"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    rows = [op_put(13, 0, patient("new1", "new1V1",
                                  refs=[{"reference": "Patient/p"}]))]
    rel = "shards/seq-13-part-0.ndjson"
    write_jsonl(out / rel, iter(rows))
    digest = sha256_file(out / rel)
    base_doc = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    doc = {
        "format_version": 1,
        "source_server": SERVER_A,
        "tenant": "tenant-main",
        "aliases": [ALIAS_A],
        "baseline": base_doc["baseline"],
        "transactions": [{
            "seq": 13,
            "operation_count": 1,
            "shards": [{"ordinal": 0, "path": rel, "sha256": digest,
                        "operation_count": 1}],
        }],
    }
    (out / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "dir": str(out)}))


if __name__ == "__main__":
    main()
