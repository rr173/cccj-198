"""Registration of local FHIR implementation-guide profile packages.

A profile package is a movable directory containing ``package.json`` plus
StructureDefinition / ValueSet and CodeSystem JSON files. Registration

* validates the package identity (name/version), every file's sha256, the
  uniqueness of each ``canonical|version`` across the whole repository, and
  the completeness and acyclicity of the declared dependencies,
* copies the exact registered bytes into the repository's content-addressed
  blob store, so the audit evidence survives the delivery directory being
  moved or deleted, and
* identifies the package by an immutable digest over its file list; the same
  name+version with different bytes is an explicit conflict, never a silent
  overwrite.

Only local files are ever read. Dependencies are resolved against already
registered packages by ``name@version``; nothing is fetched from a network.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import blobs
from .db import ensure_audit_schema, write_tx
from .errors import (
    ProfileConflict,
    ProfileDependencyError,
    ProfilePackageError,
    UnknownPackage,
)
from .jsonl import stream_file_digest
from .paths import RepoPaths

CONFORMANCE_KINDS = {"StructureDefinition", "ValueSet", "CodeSystem"}
PACKAGE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9.\-]*\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def _fail(msg: str) -> None:
    raise ProfilePackageError(msg)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_package(paths: RepoPaths, conn: sqlite3.Connection,
                     package_dir: str | Path) -> dict[str, Any]:
    """Validate and register one package directory; return a summary dict."""

    ensure_audit_schema(conn)
    root = Path(package_dir).resolve()
    if not root.is_dir():
        _fail(f"not a directory: {package_dir}")
    meta_path = root / "package.json"
    if not meta_path.is_file():
        _fail(f"package.json missing in {root}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"package.json is not valid JSON: {exc}")
    if not isinstance(meta, dict):
        _fail("package.json root must be a JSON object")
    name = meta.get("name")
    version = meta.get("version")
    if not isinstance(name, str) or not PACKAGE_NAME_RE.match(name):
        _fail(f"package.json name must match {PACKAGE_NAME_RE.pattern}: {name!r}")
    if not isinstance(version, str) or not version.strip():
        _fail("package.json version must be a non-empty string")
    dependencies = meta.get("dependencies", {})
    if not isinstance(dependencies, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in dependencies.items()):
        _fail("package.json dependencies must be an object of name: version")
    fhir_versions = meta.get("fhirVersions", [])
    if not isinstance(fhir_versions, list):
        _fail("package.json fhirVersions must be a list when present")

    files = sorted(p for p in root.rglob("*.json") if p.is_file())
    if not files:
        _fail(f"no JSON files found under {root}")
    entries: list[tuple[str, str, Path]] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        entries.append((rel, stream_file_digest(f), f))
    package_digest = hashlib.sha256(
        "".join(f"{rel}  {digest}\n" for rel, digest, _ in entries)
        .encode("utf-8")
    ).hexdigest()

    # Parse and validate every conformance resource before touching the DB.
    canonicals: list[tuple[str, str, str, str, str]] = []
    seen: dict[tuple[str, str], str] = {}
    for rel, digest, f in entries:
        if rel == "package.json":
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _fail(f"{rel}: invalid JSON: {exc}")
        if not isinstance(doc, dict):
            _fail(f"{rel}: not a JSON object")
        kind = doc.get("resourceType")
        if kind not in CONFORMANCE_KINDS:
            _fail(f"{rel}: resourceType {kind!r} is not accepted; packages may "
                  "only carry StructureDefinition, ValueSet and CodeSystem")
        url = doc.get("url")
        ver = doc.get("version")
        if not isinstance(url, str) or not url:
            _fail(f"{rel}: {kind} requires a canonical url")
        if not isinstance(ver, str) or not ver:
            _fail(f"{rel}: {kind} {url} requires a version")
        key = (url, ver)
        if key in seen:
            _fail(f"duplicate canonical {url}|{ver} in package "
                  f"({seen[key]} and {rel})")
        seen[key] = rel
        canonicals.append((url, ver, kind, rel, digest))

    with write_tx(conn):
        existing = conn.execute(
            "SELECT * FROM profile_packages WHERE package_digest=?",
            (package_digest,),
        ).fetchone()
        if existing is not None:
            out = _package_summary(conn, existing)
            out["status"] = "already_registered"
            return out

        clash = conn.execute(
            "SELECT package_digest FROM profile_packages WHERE name=? AND version=?",
            (name, version),
        ).fetchone()
        if clash is not None:
            raise ProfileConflict(
                f"package {name}@{version} is already registered with "
                f"different content (digest {clash['package_digest']}); "
                "refusing to overwrite"
            )

        dep_digests: dict[str, str] = {}
        missing: list[str] = []
        for dname, dver in sorted(dependencies.items()):
            row = conn.execute(
                "SELECT package_digest FROM profile_packages "
                "WHERE name=? AND version=?",
                (dname, dver),
            ).fetchone()
            if row is None:
                missing.append(f"{dname}@{dver}")
            else:
                dep_digests[f"{dname}@{dver}"] = row["package_digest"]
        if missing:
            raise ProfileDependencyError(
                "unregistered dependencies: " + ", ".join(missing)
                + "; register them first (offline resolution only)"
            )
        _check_dependency_cycle(conn, name, version, dependencies)

        for url, ver, kind, rel, digest in canonicals:
            row = conn.execute(
                "SELECT content_digest, package_digest FROM profile_canonicals "
                "WHERE canonical=? AND version=?",
                (url, ver),
            ).fetchone()
            if row is not None and row["content_digest"] != digest:
                raise ProfileConflict(
                    f"canonical {url}|{ver} is already registered by package "
                    f"{row['package_digest']} with different content; "
                    "refusing to overwrite"
                )

        file_rows = []
        for rel, digest, f in entries:
            blob_digest, blob_rel = blobs.add_raw_blob(paths, f.read_bytes())
            if blob_digest != digest:  # defensive: digest computed twice
                raise ProfilePackageError(f"{rel}: digest changed while reading")
            file_rows.append((package_digest, rel, digest, blob_rel))

        conn.execute(
            "INSERT INTO profile_packages(package_digest, name, version, "
            "fhir_versions, dependencies, file_count, canonical_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (package_digest, name, version,
             json.dumps(fhir_versions, sort_keys=True),
             json.dumps(dependencies, sort_keys=True),
             len(entries), len(canonicals)),
        )
        conn.executemany(
            "INSERT INTO profile_package_files(package_digest, relpath, sha256, "
            "blob_relpath) VALUES (?,?,?,?)",
            file_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO profile_canonicals(canonical, version, kind, "
            "package_digest, file_relpath, content_digest) VALUES (?,?,?,?,?,?)",
            [(url, ver, kind, package_digest, rel, digest)
             for url, ver, kind, rel, digest in canonicals],
        )

    row = conn.execute(
        "SELECT * FROM profile_packages WHERE package_digest=?",
        (package_digest,),
    ).fetchone()
    out = _package_summary(conn, row)
    out["status"] = "registered"
    out["dependency_digests"] = dep_digests
    return out


def _check_dependency_cycle(conn: sqlite3.Connection, name: str, version: str,
                            dependencies: dict[str, str]) -> None:
    """DFS the registered dependency graph plus the new package node."""

    graph: dict[tuple[str, str], dict[str, str]] = {}
    for row in conn.execute(
            "SELECT name, version, dependencies FROM profile_packages"):
        graph[(row["name"], row["version"])] = json.loads(row["dependencies"])
    graph[(name, version)] = dict(dependencies)

    visiting: list[tuple[str, str]] = []
    done: set[tuple[str, str]] = set()

    def visit(node: tuple[str, str]) -> None:
        if node in visiting:
            cycle = visiting[visiting.index(node):] + [node]
            raise ProfileDependencyError(
                "dependency cycle: "
                + " -> ".join(f"{n}@{v}" for n, v in cycle)
            )
        if node in done:
            return
        visiting.append(node)
        for dname, dver in sorted(graph.get(node, {}).items()):
            if (dname, dver) in graph:
                visit((dname, dver))
        visiting.pop()
        done.add(node)

    visit((name, version))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _package_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    canonicals = conn.execute(
        "SELECT canonical, version, kind FROM profile_canonicals "
        "WHERE package_digest=? ORDER BY kind, canonical",
        (row["package_digest"],),
    ).fetchall()
    return {
        "package_digest": row["package_digest"],
        "name": row["name"],
        "version": row["version"],
        "fhir_versions": json.loads(row["fhir_versions"]),
        "dependencies": json.loads(row["dependencies"]),
        "file_count": row["file_count"],
        "canonical_count": row["canonical_count"],
        "registered_at": row["registered_at"],
        "canonicals": [dict(c) for c in canonicals],
    }


def list_packages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_audit_schema(conn)
    rows = conn.execute(
        "SELECT * FROM profile_packages ORDER BY name, version"
    ).fetchall()
    return [_package_summary(conn, row) for row in rows]


def resolve_package_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Resolve a package reference: full digest or ``name:version``."""

    ensure_audit_schema(conn)
    if DIGEST_RE.match(ref):
        row = conn.execute(
            "SELECT * FROM profile_packages WHERE package_digest=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise UnknownPackage(f"no registered package with digest {ref}")
        return row
    if ":" in ref:
        name, version = ref.rsplit(":", 1)
        row = conn.execute(
            "SELECT * FROM profile_packages WHERE name=? AND version=?",
            (name, version),
        ).fetchone()
        if row is None:
            raise UnknownPackage(f"no registered package {name}@{version}")
        return row
    raise UnknownPackage(
        f"package reference {ref!r} is neither a 64-hex digest nor name:version"
    )


def show_package(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    row = resolve_package_ref(conn, ref)
    out = _package_summary(conn, row)
    out["files"] = [
        {"relpath": f["relpath"], "sha256": f["sha256"],
         "blob_relpath": f["blob_relpath"]}
        for f in conn.execute(
            "SELECT relpath, sha256, blob_relpath FROM profile_package_files "
            "WHERE package_digest=? ORDER BY relpath",
            (row["package_digest"],),
        )
    ]
    return out


def dependency_closure_digests(conn: sqlite3.Connection,
                               package_digest: str) -> list[str]:
    """The package itself plus every transitively registered dependency."""

    seen = {package_digest}
    queue = [package_digest]
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        row = conn.execute(
            "SELECT dependencies FROM profile_packages WHERE package_digest=?",
            (current,),
        ).fetchone()
        if row is None:
            raise UnknownPackage(f"registered package vanished: {current}")
        for dname, dver in sorted(json.loads(row["dependencies"]).items()):
            dep = conn.execute(
                "SELECT package_digest FROM profile_packages "
                "WHERE name=? AND version=?",
                (dname, dver),
            ).fetchone()
            if dep is None:
                raise ProfileDependencyError(
                    f"registered dependency {dname}@{dver} of {current} is "
                    "no longer present"
                )
            digest = dep["package_digest"]
            if digest not in seen:
                seen.add(digest)
                queue.append(digest)
    return sorted(order)


def load_closure(conn: sqlite3.Connection, paths: RepoPaths,
                 package_digest: str):
    """Build the in-memory guideline model for one pinned package digest."""

    from .igmodel import PackageClosure

    digests = dependency_closure_digests(conn, package_digest)
    marks = ",".join("?" for _ in digests)
    rows = conn.execute(
        "SELECT c.canonical, c.version, c.kind, f.blob_relpath "
        "FROM profile_canonicals c "
        "JOIN profile_package_files f "
        "  ON f.package_digest = c.package_digest "
        " AND f.relpath = c.file_relpath "
        f"WHERE c.package_digest IN ({marks}) "
        "ORDER BY c.canonical, c.version",
        digests,
    ).fetchall()
    sds: dict[tuple[str, str], dict] = {}
    vss: dict[tuple[str, str], dict] = {}
    css: dict[tuple[str, str], dict] = {}
    for row in rows:
        doc = json.loads((paths.root / row["blob_relpath"]).read_text(
            encoding="utf-8"))
        key = (row["canonical"], row["version"])
        if row["kind"] == "StructureDefinition":
            sds[key] = doc
        elif row["kind"] == "ValueSet":
            vss[key] = doc
        else:
            css[key] = doc
    return PackageClosure(package_digest, sds, vss, css)
