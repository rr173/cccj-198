#!/usr/bin/env python3 -I
"""Build a corrupt-baseline delivery followed by a clean recovery delivery.

The two packages reuse the same relative shard path with different bytes,
which is the exact layout that exposed stale expected-digest handling.
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

SERVER = "https://fhir.example.org/a"
TENANT = "tenant-bad-baseline"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    fixtures = Path(sys.argv[1])
    clean = fixtures / "case-bad-baseline-clean"
    corrupt = fixtures / "case-bad-baseline-corrupt"
    for d in (clean, corrupt):
        if d.exists():
            shutil.rmtree(d)
        (d / "baseline").mkdir(parents=True)

    good_row = {
        "seq": 7, "ordinal": 0, "method": "PUT",
        "resourceType": "Patient", "id": "z", "versionId": "z1",
        "resource": {"resourceType": "Patient", "id": "z",
                     "meta": {"versionId": "z1",
                              "lastUpdated": "2026-04-01T00:00:00Z"}},
    }
    write_jsonl(clean / "baseline" / "b.ndjson", iter([good_row]))
    good_digest = sha256_file(clean / "baseline" / "b.ndjson")

    # Corrupt bytes: same path, appended invalid line, honestly digested.
    shutil.copyfile(clean / "baseline" / "b.ndjson",
                    corrupt / "baseline" / "b.ndjson")
    with open(corrupt / "baseline" / "b.ndjson", "a",
              encoding="utf-8") as fh:
        fh.write("{this is not valid json\n")
    bad_digest = sha256_file(corrupt / "baseline" / "b.ndjson")

    def write_manifest(d: Path, digest: str, count: int) -> None:
        doc = {
            "format_version": 1, "source_server": SERVER,
            "tenant": TENANT, "aliases": [],
            "baseline": {"seq": 7, "path": "baseline/b.ndjson",
                         "sha256": digest, "resource_count": count},
            "transactions": [],
        }
        (d / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    write_manifest(clean, good_digest, 1)
    write_manifest(corrupt, bad_digest, 2)  # count claim also wrong
    print(json.dumps({"status": "ok",
                      "packages": [clean.name, corrupt.name]}))


if __name__ == "__main__":
    main()
