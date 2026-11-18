#!/usr/bin/env python3 -I
"""Generate all synthetic handoff packages for the offline drill.

Everything here is invented data: no hospital systems are contacted and no
patient-entity matching is performed. The generator only knows how to write
manifest.json + NDJSON shard packages with matching sha256 digests, plus the
deliberately broken packages used by the negative tests.

Layout per source::

    build/fixtures/<source_name>/
        manifest.json
        baseline/...ndjson
        shards/seq-<n>-part-<m>.ndjson
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fhist.jsonl import write_jsonl  # noqa: E402

BASE_A = "https://fhir.example.org/a"
BASE_B = "https://fhir.example.org/b"
ALIAS_A = "https://fhir-alias.example.net/a"
FOREIGN = "https://elsewhere.example.com/fhir"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def patient(pid: str, vid: str, refs: list[dict] | None = None,
            contained: list[dict] | None = None,
            extra: dict | None = None) -> dict:
    res: dict = {
        "resourceType": "Patient",
        "id": pid,
        "meta": {"versionId": vid,
                 "lastUpdated": f"2026-01-0{vid[-1] if vid[-1].isdigit() else 1}T00:00:00Z"},
        "active": True,
        "name": [{"use": "official", "family": f"SYNTHETIC-{pid}",
                  "given": ["Test"]}],
    }
    if refs:
        res["link"] = refs
    if contained:
        res["contained"] = contained
    if extra:
        res.update(extra)
    return res


def observation(oid: str, vid: str, refs: list[dict],
                status: str = "final") -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "meta": {"versionId": vid, "lastUpdated": "2026-01-10T00:00:00Z"},
        "status": status,
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "29463-7", "display": "Body weight"}]},
        "subject": refs[0] if refs else None,
        "focus": refs[1:] if len(refs) > 1 else [],
    }


def op_put(seq: int, ordinal: int, resource: dict) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "PUT",
            "resourceType": resource["resourceType"], "id": resource["id"],
            "versionId": resource["meta"]["versionId"], "resource": resource}


def op_delete(seq: int, ordinal: int, rtype: str, rid: str,
              vid: str) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "DELETE",
            "resourceType": rtype, "id": rid, "versionId": vid}


def write_shard(dest: Path, rows: list[dict]) -> tuple[str, str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = write_jsonl(dest, iter(rows))
    rel = dest.relative_to(dest.parents[1]).as_posix()
    return rel, sha256_file(dest), n


def manifest_doc(source_server: str, tenant: str, aliases: list[str],
                 baseline: dict, txns: list[dict]) -> dict:
    return {
        "format_version": 1,
        "source_server": source_server,
        "tenant": tenant,
        "aliases": aliases,
        "baseline": baseline,
        "transactions": txns,
    }


# ---------------------------------------------------------------------------
# Source A: the headline walkthrough
# ---------------------------------------------------------------------------

def build_source_a(root: Path) -> None:
    d = root / "sourceA"
    # Baseline seq 10: Patient/p at vA, Observation/o with two refs.
    p_vA = patient("p", "vA")
    o_v1 = observation("o", "oV1", [
        {"reference": "Patient/p"},
        {"reference": f"Patient/p/_history/vA"},
    ])
    # A probe whose versioned edge points at a version that only appears later.
    probe = observation("probe", "probeV1", [
        {"reference": "Patient/p/_history/vFUTURE"},
    ])
    # Contained demo: owner1 and owner2 each define a contained "#x".
    owner1 = patient("owner1", "owner1V1",
                     refs=[{"reference": "#x"},
                           {"reference": "Patient/p"}],
                     contained=[{"resourceType": "RelatedPerson", "id": "x",
                                 "name": [{"family": "contained-in-owner1"}]}])
    owner2 = patient("owner2", "owner2V1",
                     refs=[{"reference": "#x"}],
                     contained=[{"resourceType": "RelatedPerson", "id": "x",
                                 "name": [{"family": "contained-in-owner2"}]}])
    # Absolute same-source via alias, and an external foreign-host reference.
    org = {
        "resourceType": "Organization", "id": "org1",
        "meta": {"versionId": "orgV1", "lastUpdated": "2026-01-10T00:00:00Z"},
        "name": "Synthetic Org",
        "partOf": {"reference": f"{ALIAS_A}/Organization/shared"},
    }
    ext = {
        "resourceType": "Basic", "id": "ext1",
        "meta": {"versionId": "extV1", "lastUpdated": "2026-01-10T00:00:00Z"},
        "code": {"text": "external holder"},
        "subject": {"reference": f"{FOREIGN}/Patient/zzz"},
    }
    baseline_rows = [
        op_put(10, 0, p_vA), op_put(10, 1, o_v1), op_put(10, 2, probe),
        op_put(10, 3, owner1), op_put(10, 4, owner2),
        op_put(10, 5, org), op_put(10, 6, ext),
    ]
    b_rel, b_digest, b_count = write_shard(
        d / "baseline" / "baseline.ndjson", baseline_rows)

    # Transaction 11 split into two shards: p vA -> vB.
    p_vB = patient("p", "vB")
    t11_s1 = [op_put(11, 0, p_vB)]
    t11_s2 = [op_put(11, 1, observation("noise11", "n11V1",
                                        [{"reference": "Patient/p"}]))]
    s11_1 = write_shard(d / "shards" / "seq-11-part-0.ndjson", t11_s1)
    s11_2 = write_shard(d / "shards" / "seq-11-part-1.ndjson", t11_s2)

    # Transaction 12: delete p with tombstone version vC.
    t12 = [op_delete(12, 0, "Patient", "p", "vC")]
    s12 = write_shard(d / "shards" / "seq-12-part-0.ndjson", t12)

    doc = manifest_doc(BASE_A, "tenant-main", [ALIAS_A],
                       {"seq": 10, "path": b_rel, "sha256": b_digest,
                        "resource_count": b_count},
                       [
                        {"seq": 11, "operation_count": 2, "shards": [
                            {"ordinal": 0, "path": s11_1[0],
                             "sha256": s11_1[1], "operation_count": s11_1[2]},
                            {"ordinal": 1, "path": s11_2[0],
                             "sha256": s11_2[1], "operation_count": s11_2[2]}]},
                        {"seq": 12, "operation_count": 1, "shards": [
                            {"ordinal": 0, "path": s12[0],
                             "sha256": s12[1], "operation_count": s12[2]}]},
                       ])
    (d / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def build_source_a_partial(root: Path) -> None:
    """Same manifest content but delivered without the seq-11 second shard."""

    src = root / "sourceA"
    d = root / "sourceA-partial"
    import shutil
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(src, d)
    # Drop txn 12 entirely and one shard of txn 11 to stage an incomplete 11
    # while a complete 12 sits in the same delivery (out-of-order drill).
    doc = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    doc["transactions"] = [
        {"seq": 11, "operation_count": 2, "shards": [
            {"ordinal": 0,
             "path": "shards/seq-11-part-0.ndjson",
             "sha256": sha256_file(d / "shards" / "seq-11-part-0.ndjson"),
             "operation_count": 1},
            {"ordinal": 1,
             "path": "shards/seq-11-part-1-MISSING.ndjson",
             "sha256": "0" * 64, "operation_count": 1}]},
        {"seq": 12, "operation_count": 1, "shards": [
            {"ordinal": 0,
             "path": "shards/seq-12-part-0.ndjson",
             "sha256": sha256_file(d / "shards" / "seq-12-part-0.ndjson"),
             "operation_count": 1}]},
    ]
    (d / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Source B: second tenant with the same logical id "p"
# ---------------------------------------------------------------------------

def build_source_b(root: Path) -> None:
    d = root / "sourceB"
    p = patient("p", "vX")  # same id, different identity namespace
    other = patient("other", "vO",
                    refs=[{"reference": "Patient/p"},
                          {"reference": f"{BASE_A}/Patient/p"}])
    rows = [op_put(20, 0, p), op_put(20, 1, other)]
    b_rel, b_digest, b_count = write_shard(
        d / "baseline" / "baseline.ndjson", rows)
    doc = manifest_doc(BASE_B, "tenant-other", [],
                       {"seq": 20, "path": b_rel, "sha256": b_digest,
                        "resource_count": b_count}, [])
    (d / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Negative / edge-case packages, one isolated source each
# ---------------------------------------------------------------------------

def _mini_baseline(d: Path, seq: int = 5) -> tuple[dict, None]:
    p = patient("p", "v1")
    rows = [op_put(seq, 0, p)]
    b_rel, b_digest, b_count = write_shard(
        d / "baseline" / "baseline.ndjson", rows)
    return ({"seq": seq, "path": b_rel, "sha256": b_digest,
             "resource_count": b_count}), None


def build_dup_ordinal(root: Path) -> None:
    d = root / "case-dup-ordinal"
    base, _ = _mini_baseline(d)
    rows = [op_put(6, 0, patient("q", "q1")),
            op_put(6, 0, patient("r", "r1"))]  # duplicate ordinal
    rel, digest, count = write_shard(d / "shards" / "seq-6.ndjson", rows)
    doc = manifest_doc(BASE_A, "tenant-dup", [], base, [
        {"seq": 6, "operation_count": count, "shards": [
            {"ordinal": 0, "path": rel, "sha256": digest,
             "operation_count": count}]}])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_body_mismatch(root: Path) -> None:
    d = root / "case-body-mismatch"
    base, _ = _mini_baseline(d)
    bad = patient("p", "v9")  # envelope will claim a different versionId
    row = op_put(6, 0, bad)
    row["versionId"] = "vLIAR"
    rel, digest, count = write_shard(d / "shards" / "seq-6.ndjson", [row])
    doc = manifest_doc(BASE_A, "tenant-mismatch", [], base, [
        {"seq": 6, "operation_count": count, "shards": [
            {"ordinal": 0, "path": rel, "sha256": digest,
             "operation_count": count}]}])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_corrupt_shard(root: Path) -> None:
    d = root / "case-corrupt"
    base, _ = _mini_baseline(d)
    rel, digest, count = write_shard(
        d / "shards" / "seq-6.ndjson", [op_put(6, 0, patient("q", "q1"))])
    # Tamper bytes while keeping the manifest digest stale.
    target = d / rel
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    doc = manifest_doc(BASE_A, "tenant-corrupt", [], base, [
        {"seq": 6, "operation_count": count, "shards": [
            {"ordinal": 0, "path": rel, "sha256": digest,
             "operation_count": count}]}])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_cycle(root: Path) -> None:
    d = root / "case-cycle"
    base, _ = _mini_baseline(d)
    a = patient("cycA", "a1", refs=[{"reference": "Patient/cycB"}])
    b = patient("cycB", "b1", refs=[{"reference": "Patient/cycC"}])
    c = patient("cycC", "c1", refs=[{"reference": "Patient/cycA"}])
    rows = [op_put(6, 0, a), op_put(6, 1, b), op_put(6, 2, c)]
    rel, digest, count = write_shard(d / "shards" / "seq-6.ndjson", rows)
    doc = manifest_doc(BASE_A, "tenant-cycle", [], base, [
        {"seq": 6, "operation_count": count, "shards": [
            {"ordinal": 0, "path": rel, "sha256": digest,
             "operation_count": count}]}])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def build_divergent_retransmit(root: Path) -> None:
    """First delivery publishes seq 6; second delivery re-sends a changed body."""

    d1 = root / "case-retransmit-v1"
    base, _ = _mini_baseline(d1)
    good = patient("p", "v2")
    rel, digest, count = write_shard(
        d1 / "shards" / "seq-6.ndjson", [op_put(6, 0, good)])
    doc = manifest_doc(BASE_A, "tenant-retrans", [], base, [
        {"seq": 6, "operation_count": count, "shards": [
            {"ordinal": 0, "path": rel, "sha256": digest,
             "operation_count": count}]}])
    (d1 / "manifest.json").write_text(json.dumps(doc, indent=2),
                                      encoding="utf-8")

    d2 = root / "case-retransmit-v2"
    import shutil
    if d2.exists():
        shutil.rmtree(d2)
    shutil.copytree(d1, d2)
    changed = dict(good)
    changed = json.loads(json.dumps(good))
    changed["active"] = False  # same identity/version, different payload
    rel2, digest2, count2 = write_shard(
        d2 / "shards" / "seq-6.ndjson", [op_put(6, 0, changed)])
    doc2 = json.loads((d2 / "manifest.json").read_text(encoding="utf-8"))
    doc2["baseline"] = doc["baseline"]
    doc2["transactions"] = [
        {"seq": 6, "operation_count": count2, "shards": [
            {"ordinal": 0, "path": rel2, "sha256": digest2,
             "operation_count": count2}]}]
    (d2 / "manifest.json").write_text(json.dumps(doc2, indent=2),
                                      encoding="utf-8")


def build_missing_history(root: Path) -> None:
    """A versioned ref to a version that never exists at any cut."""

    d = root / "case-missing-history"
    base, _ = _mini_baseline(d)
    holder = observation("h", "h1",
                         [{"reference": "Patient/p/_history/vGHOST"}])
    rel, digest, count = write_shard(
        d / "shards" / "seq-6.ndjson", [op_put(6, 0, holder)])
    doc = manifest_doc(BASE_A, "tenant-missing-history", [], base, [
        {"seq": 6, "operation_count": count, "shards": [
            {"ordinal": 0, "path": rel, "sha256": digest,
             "operation_count": count}]}])
    (d / "manifest.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/fixtures")
    root.mkdir(parents=True, exist_ok=True)
    builders = [
        build_source_a, build_source_a_partial, build_source_b,
        build_dup_ordinal, build_body_mismatch, build_corrupt_shard,
        build_cycle, build_divergent_retransmit, build_missing_history,
    ]
    for fn in builders:
        fn(root)
    print(json.dumps({"status": "ok", "fixtures": sorted(
        p.name for p in root.iterdir() if p.is_dir())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
