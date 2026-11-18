#!/usr/bin/env python3 -I
"""Full rebuild of a repository from archived handoff packages.

The store never needs incremental repair: every published fact traces back
to a manifest + shard bytes kept under the source's ``packages/`` tree. This
tool builds a fresh repository, replays every source's archived packages in
delivery order, advances each source to its previous frontier, and then
compares the cut lists and per-cut current reads of old and new.

Usage:
  rebuild.py --from OLD_REPO --to NEW_REPO
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "fhist" / "cli.py"


def run(repo: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), "--repo", str(repo), *args],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"rebuild command failed: {' '.join(args)}\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout)


def _package_material_complete(manifest_path: Path) -> bool:
    """True when every transaction shard named by the manifest exists.

    A follow-up delivery may reference a baseline that physically lives in
    an earlier package directory, so baseline presence is not required here;
    only the shards this package actually carries must be on disk.
    """

    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    for txn in doc.get("transactions", []):
        for sh in txn["shards"]:
            if not (root / sh["path"]).is_file():
                return False
    return True


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_dirname(server: str, tenant: str) -> str:
    return hashlib.sha256(f"{server}\x1f{tenant}".encode("utf-8")).hexdigest()[:16]


def _packages_dir(repo: Path, server: str, tenant: str) -> Path:
    return repo / "sources" / _source_dirname(server, tenant) / "packages"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src_repo", required=True)
    ap.add_argument("--to", dest="dst_repo", required=True)
    args = ap.parse_args()
    old = Path(args.src_repo)
    new = Path(args.dst_repo)
    if new.exists():
        shutil.rmtree(new)

    old_status = run(old, "status")
    # Aliases live in repo.json of the old store; reuse that config verbatim.
    shutil.copyfile(old / "repo.json", new.parent / "rebuild-repo.json")
    run(new, "init", "--sources-file", str(new.parent / "rebuild-repo.json"))

    report: dict[str, Any] = {"rebuilt_sources": [], "mismatches": []}
    for src in old_status["sources"]:
        server, tenant = src["source_server"], src["tenant"]
        # Stage every archived delivery first (partial/corrupt packages may
        # precede their clean replacements), then advance until the frontier
        # stops moving. A package whose manifest names shards that were never
        # copied (because they were missing in the original delivery) is
        # skipped here: it cannot publish anything and must not block the
        # complete package for the same seq.
        manifests = sorted(_packages_dir(old, server, tenant).glob(
            "*/manifest.json"))
        seen_digests: set[str] = set()
        skipped_incomplete: list[str] = []
        for manifest in manifests:
            digest = _sha256(manifest.read_text(encoding="utf-8"))
            if digest in seen_digests:
                continue
            seen_digests.add(digest)
            if not _package_material_complete(manifest):
                skipped_incomplete.append(manifest.parent.name)
                continue
            run(new, "stage", "--manifest", str(manifest))
        # Repeated deliveries for one seq (partial then complete) update the
        # same shard rows; advance until the frontier stops moving so every
        # later verified delivery gets a chance to unblock its seq.
        previous = None
        while True:
            out = run(new, "advance", "--source-server", server,
                      "--tenant", tenant)
            frontier = out.get("frontier")
            if frontier == previous:
                break
            previous = frontier

        old_s = run(old, "status", "--source-server", server,
                    "--tenant", tenant)["sources"][0]
        new_s = run(new, "status", "--source-server", server,
                    "--tenant", tenant)["sources"][0]
        cuts_match = (old_s["queryable_cuts"] == new_s["queryable_cuts"])
        frontier_match = (old_s["frontier_queryable"]
                          == new_s["frontier_queryable"])

        # Re-verify every cut on a representative identity when present.
        cut_reads = []
        for cut in old_s["queryable_cuts"]:
            probe = run(old, "resource", "--source-server", server,
                        "--tenant", tenant, "--cut", str(cut),
                        "--resource-type", "Patient", "--id", "p")
            if probe["status"] == "missing":
                continue
            rebuilt = run(new, "resource", "--source-server", server,
                          "--tenant", tenant, "--cut", str(cut),
                          "--resource-type", "Patient", "--id", "p")
            same = (probe["status"], probe.get("versionId")) == (
                rebuilt["status"], rebuilt.get("versionId"))
            cut_reads.append({"cut": cut, "same": same})
            if not same:
                report["mismatches"].append(
                    f"{server}/{tenant} cut{cut}: {probe} != {rebuilt}")

        report["rebuilt_sources"].append({
            "source_server": server, "tenant": tenant,
            "old_frontier": old_s["frontier_queryable"],
            "new_frontier": new_s["frontier_queryable"],
            "cuts_match": cuts_match,
            "frontier_match": frontier_match,
            "cut_read_checks": cut_reads,
            "manifests_replayed": len(seen_digests),
            "skipped_incomplete_packages": skipped_incomplete,
        })
        if not (cuts_match and frontier_match):
            report["mismatches"].append(f"{server}/{tenant}: cut list differs")

    report["status"] = "ok" if not report["mismatches"] else "mismatch"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
