#!/usr/bin/env python3 -I
"""Generate synthetic handoff packages for the identity-dimension drill.

All data is invented. Three ward campuses (``alpha``, ``bravo``, ``charlie``)
plus an unrelated fourth (``delta``) each publish their own Patient ids; a
same-looking id in two campuses stays unrelated unless a human adjudication
says otherwise.

Headline scenario::

    a < b < c     same(a,b)       [2026-01-01, 2026-02-01)
                  same(b,c)       [2026-01-10, 2026-01-20)
                  different(a,c)  [2026-01-15, 2026-01-18)

    14th -> all three keys belong to ``a``
    16th -> the whole group is in conflict, evidence runs a-c via b
    19th -> back to ``a`` (different ended on the 18th)
    20th -> c stands alone

Extra synthetic material in ``delta`` (and a same-named id in each source):

* ``alpha|a1`` and ``delta|a1`` share a logical id but never merge by name;
* ``delta`` m/n/o/p form two equal-length ``same`` paths m-n-p and m-o-p;
* ``delta`` also has parallel ``same`` adjudications between two keys.

Layout::

    build/identity-fixtures/
        alpha/  bravo/  charlie/  delta/  bravo-advance/
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fhist.jsonl import write_jsonl  # noqa: E402

BASE = "https://fhir.example.org"
TENANT = "tenant-care"

SOURCES = {
    "alpha": f"{BASE}/alpha",
    "bravo": f"{BASE}/bravo",
    "charlie": f"{BASE}/charlie",
    "delta": f"{BASE}/delta",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def patient(pid: str, vid: str, last_updated: str) -> dict:
    return {
        "resourceType": "Patient", "id": pid,
        "meta": {"versionId": vid, "lastUpdated": last_updated},
        "active": True,
        "name": [{"use": "official", "family": f"SYNTH-{pid}",
                  "given": ["Synthetic"]}],
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
    return dest.relative_to(dest.parents[1]).as_posix(), sha256_file(dest), n


def manifest_doc(source_server: str, baseline: dict,
                 txns: list[dict]) -> dict:
    return {"format_version": 1, "source_server": source_server,
            "tenant": TENANT, "aliases": [], "baseline": baseline,
            "transactions": txns}


def build_alpha(root: Path) -> None:
    d = root / "alpha"
    rows = [
        op_put(10, 0, patient("a1", "v1", "2026-01-01T00:00:00Z")),
        op_put(10, 1, patient("x1", "v1", "2026-01-01T00:00:00Z")),
    ]
    rel, digest, count = write_shard(d / "baseline" / "baseline.ndjson", rows)
    doc = manifest_doc(SOURCES["alpha"],
                       {"seq": 10, "path": rel, "sha256": digest,
                        "resource_count": count}, [])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_bravo(root: Path) -> None:
    d = root / "bravo"
    rows = [
        op_put(20, 0, patient("b1", "v1", "2026-01-01T00:00:00Z")),
        op_put(20, 1, patient("x2", "v1", "2026-01-01T00:00:00Z")),
    ]
    rel, digest, count = write_shard(d / "baseline" / "baseline.ndjson", rows)
    doc = manifest_doc(SOURCES["bravo"],
                       {"seq": 20, "path": rel, "sha256": digest,
                        "resource_count": count}, [])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_charlie(root: Path) -> None:
    d = root / "charlie"
    rows = [
        op_put(30, 0, patient("c1", "v1", "2026-01-01T00:00:00Z")),
        op_put(30, 1, patient("y1", "v1", "2026-01-01T00:00:00Z")),
    ]
    rel, digest, count = write_shard(d / "baseline" / "baseline.ndjson", rows)
    doc = manifest_doc(SOURCES["charlie"],
                       {"seq": 30, "path": rel, "sha256": digest,
                        "resource_count": count}, [])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_delta(root: Path) -> None:
    d = root / "delta"
    rows = [
        # Same logical id as alpha|a1, but a different source: never merged.
        op_put(40, 0, patient("a1", "v1", "2026-01-01T00:00:00Z")),
        # Equal-length path fixture: m-n-p and m-o-p.
        op_put(40, 1, patient("m", "v1", "2026-01-01T00:00:00Z")),
        op_put(40, 2, patient("n", "v1", "2026-01-01T00:00:00Z")),
        op_put(40, 3, patient("o", "v1", "2026-01-01T00:00:00Z")),
        op_put(40, 4, patient("p", "v1", "2026-01-01T00:00:00Z")),
        # Parallel adjudication pair.
        op_put(40, 5, patient("x3", "v1", "2026-01-01T00:00:00Z")),
        op_put(40, 6, patient("x4", "v1", "2026-01-01T00:00:00Z")),
        op_put(40, 7, patient("z1", "v1", "2026-01-01T00:00:00Z")),
    ]
    rel, digest, count = write_shard(d / "baseline" / "baseline.ndjson", rows)
    doc = manifest_doc(SOURCES["delta"],
                       {"seq": 40, "path": rel, "sha256": digest,
                        "resource_count": count}, [])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_bravo_advance(root: Path) -> None:
    """b1 tombstoned (seq21), b2 new (seq22), b1 restored (seq23)."""

    d = root / "bravo-advance"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(root / "bravo", d)
    base = json.loads((d / "manifest.json").read_text(encoding="utf-8"))["baseline"]
    s21 = write_shard(d / "shards" / "seq-21.ndjson",
                      [op_delete(21, 0, "Patient", "b1", "v2")])
    s22 = write_shard(d / "shards" / "seq-22.ndjson",
                      [op_put(22, 0, patient("b2", "v1",
                                             "2026-01-05T00:00:00Z"))])
    s23 = write_shard(d / "shards" / "seq-23.ndjson",
                      [op_put(23, 0, patient("b1", "v3",
                                             "2026-01-01T00:00:00Z"))])
    doc = manifest_doc(SOURCES["bravo"], base, [
        {"seq": seq, "operation_count": 1,
         "shards": [{"ordinal": 0, "path": shard[0], "sha256": shard[1],
                     "operation_count": shard[2]}]}
        for seq, shard in ((21, s21), (22, s22), (23, s23))])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/identity-fixtures")
    root.mkdir(parents=True, exist_ok=True)
    for fn in (build_alpha, build_bravo, build_charlie, build_delta):
        fn(root)
    build_bravo_advance(root)
    print(json.dumps({"status": "ok", "fixtures": sorted(
        p.name for p in root.iterdir() if p.is_dir())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
