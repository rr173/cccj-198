"""Package intake: copy a delivered handoff package into the repository.

The repository must remain queryable after the delivery directory is moved
or deleted, so ``stage`` first copies the manifest and every shard it names
under the source's own ``packages`` tree. Only repository-relative paths are
persisted afterwards. The copy is resumable: existing verified files are not
re-copied.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import ManifestError
from .jsonl import stream_file_digest
from .manifest import Manifest, load_manifest
from .paths import RepoPaths


def _safe_rel(package_root: Path, relpath: str) -> Path:
    target = (package_root / relpath).resolve()
    if package_root.resolve() not in target.parents:
        raise ManifestError(f"path escapes package root: {relpath}")
    return target


def intake_package(paths: RepoPaths, manifest_path: str | Path) -> tuple[Manifest, Path]:
    """Copy manifest + named shards; return (manifest, internal package root)."""

    manifest_path = Path(manifest_path).resolve()
    package_root = manifest_path.parent
    manifest = load_manifest(manifest_path)

    # The full manifest digest makes every distinct delivery its own
    # directory: two deliveries for the same baseline seq never share files,
    # even when they reuse identical relative shard paths.
    dest_root = (
        paths.source_root(manifest.source_server, manifest.tenant)
        / "packages"
        / f"seq{manifest.baseline.seq}-{manifest.digest}"
    )
    dest_root.mkdir(parents=True, exist_ok=True)

    def copy_one(src_rel: str, claimed_digest: str | None = None) -> None:
        src = _safe_rel(package_root, src_rel)
        if not src.is_file():
            return  # missing files stay missing; staging records the state
        dst = dest_root / src_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            # Same delivery re-intake: identical bytes make this a no-op.
            if stream_file_digest(dst) == stream_file_digest(src):
                return
            raise ManifestError(
                f"internal collision in {dest_root}: {src_rel} already exists "
                "with different bytes"
            )
        # Stream copy keeps a huge resource file out of memory.
        with open(src, "rb") as fin, open(dst.with_suffix(".copy"), "wb") as fout:
            for chunk in iter(lambda: fin.read(1024 * 1024), b""):
                fout.write(chunk)
        dst.with_suffix(".copy").replace(dst)

    copy_one(manifest.baseline.relpath)
    for txn in manifest.transactions:
        for shard in txn.shards:
            copy_one(shard.relpath)

    manifest_dst = dest_root / "manifest.json"
    if not manifest_dst.exists() or \
            stream_file_digest(manifest_dst) != manifest.digest:
        shutil.copyfile(manifest_path, manifest_dst)
    return manifest, dest_root
