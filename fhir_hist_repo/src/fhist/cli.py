"""Command-line entry point for the FHIR R4 history object store.

Subcommands: init, stage, advance, status, resource, references, trace-path,
verify. All output is JSON on stdout; failures carry a stable ``status``
string and a non-zero exit code. Run with ``python -I`` from any directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow execution via `python -I bin/fhist ...` without installation: the
# src tree sits next to bin/ in the relocatable repository delivery.
_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fhist import ingest, query, status as status_mod  # noqa: E402
from fhist.config import RepoConfig  # noqa: E402
from fhist.db import connect, initialise  # noqa: E402
from fhist.errors import FHError  # noqa: E402
from fhist.intake import intake_package  # noqa: E402
from fhist.paths import RepoPaths  # noqa: E402


def _emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _open_repo(args: argparse.Namespace) -> tuple[RepoPaths, RepoConfig, Any]:
    paths = RepoPaths(args.repo)
    if not paths.exists():
        raise FHError(f"not an initialised repository: {args.repo}")
    config = RepoConfig.load(paths.root)
    conn = connect(paths.db)
    return paths, config, conn


def cmd_init(args: argparse.Namespace) -> None:
    paths = RepoPaths(args.repo)
    paths.ensure_layout()
    sources = []
    if args.sources_file:
        raw = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))
        sources = raw.get("sources", [])
    config = RepoConfig.initial(sources)
    config.save(paths.root)
    conn = connect(paths.db)
    initialise(conn)
    conn.close()
    _emit({"status": "initialised", "repo": str(paths.root),
           "registered_sources": sorted(config.sources.keys())})


def cmd_stage(args: argparse.Namespace) -> None:
    paths, config, conn = _open_repo(args)
    try:
        manifest, internal_root = intake_package(paths, args.manifest)
        result = ingest.stage_manifest(
            paths, conn, config,
            internal_root / "manifest.json",
            package_root=internal_root,
        )
        result["status"] = "staged"
        result["manifest_digest"] = manifest.digest
        _emit(result)
    finally:
        conn.close()


def cmd_advance(args: argparse.Namespace) -> None:
    paths, config, conn = _open_repo(args)
    try:
        result = ingest.advance(paths, conn, config, args.source_server,
                                args.tenant, to_seq=args.to_seq)
        result["status"] = "advanced"
        _emit(result)
    finally:
        conn.close()


def cmd_status(args: argparse.Namespace) -> None:
    paths, _config, conn = _open_repo(args)
    try:
        payload = status_mod.repo_status(conn)
        if args.source_server:
            payload["sources"] = [
                s for s in payload["sources"]
                if s["source_server"] == args.source_server
                and (args.tenant is None or s["tenant"] == args.tenant)
            ]
        payload["status"] = "ok"
        _emit(payload)
    finally:
        conn.close()


def _source(conn, args):
    return query.load_source(conn, args.source_server, args.tenant)


def cmd_resource(args: argparse.Namespace) -> None:
    paths, _config, conn = _open_repo(args)
    try:
        source = _source(conn, args)
        if args.version_id:
            out = query.read_version(paths, conn, source, args.cut,
                                     args.resource_type, args.id,
                                     args.version_id)
        elif args.history:
            out = query.read_history(paths, conn, source, args.cut,
                                     args.resource_type, args.id)
        else:
            out = query.read_current(paths, conn, source, args.cut,
                                     args.resource_type, args.id)
        if args.hold_open:  # test hook: keep the snapshot while a writer runs
            _hold(args.hold_open)
        _emit(out)
    finally:
        conn.close()


def cmd_references(args: argparse.Namespace) -> None:
    paths, _config, conn = _open_repo(args)
    try:
        source = _source(conn, args)
        out = query.references_for(paths, conn, source, args.cut,
                                   args.resource_type, args.id,
                                   args.direction)
        if args.hold_open:
            _hold(args.hold_open)
        _emit(out)
    finally:
        conn.close()


def cmd_trace_path(args: argparse.Namespace) -> None:
    paths, _config, conn = _open_repo(args)
    try:
        source = _source(conn, args)
        out = query.trace_path(paths, conn, source, args.cut,
                               args.resource_type, args.id,
                               max_hops=args.max_hops)
        _emit(out)
    finally:
        conn.close()


def cmd_verify(args: argparse.Namespace) -> None:
    """Recompute every stored body digest; used by rebuild/copy drills."""

    from fhist.jsonl import stream_file_digest
    paths, _config, conn = _open_repo(args)
    try:
        rows = conn.execute(
            "SELECT body_digest, body_relpath FROM resource_version "
            "WHERE body_relpath IS NOT NULL"
        ).fetchall()
        bad = []
        checked = 0
        for row in rows:
            actual = stream_file_digest(paths.root / row["body_relpath"])
            checked += 1
            if actual != row["body_digest"]:
                bad.append({"relpath": row["body_relpath"],
                            "expected": row["body_digest"], "actual": actual})
        _emit({"status": "ok" if not bad else "corrupt",
               "checked_bodies": checked, "bad": bad})
    finally:
        conn.close()


def _hold(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(0.05, end - time.monotonic()))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fhist",
                                description="FHIR R4 offline history object store")
    p.add_argument("--repo", required=True, help="repository root directory")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="create a new repository")
    sp.add_argument("--sources-file",
                    help="JSON file declaring canonical base URLs and aliases")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("stage", help="stage a manifest delivery")
    sp.add_argument("--manifest", required=True)
    sp.set_defaults(func=cmd_stage)

    sp = sub.add_parser("advance", help="publish consecutive staged txns")
    sp.add_argument("--source-server", required=True)
    sp.add_argument("--tenant", required=True)
    sp.add_argument("--to-seq", type=int, default=None)
    sp.set_defaults(func=cmd_advance)

    sp = sub.add_parser("status", help="show frontier, staging and conflicts")
    sp.add_argument("--source-server")
    sp.add_argument("--tenant")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("resource", help="read a resource at a cut")
    sp.add_argument("--source-server", required=True)
    sp.add_argument("--tenant", required=True)
    sp.add_argument("--cut", type=int, required=True)
    sp.add_argument("--resource-type", required=True)
    sp.add_argument("--id", required=True)
    sp.add_argument("--version-id")
    sp.add_argument("--history", action="store_true")
    sp.add_argument("--hold-open", type=float, default=0,
                    help="test hook: hold the read snapshot open for N seconds")
    sp.set_defaults(func=cmd_resource)

    sp = sub.add_parser("references", help="reference up/downstream at a cut")
    sp.add_argument("--source-server", required=True)
    sp.add_argument("--tenant", required=True)
    sp.add_argument("--cut", type=int, required=True)
    sp.add_argument("--resource-type")
    sp.add_argument("--id")
    sp.add_argument("--direction", choices=("out", "in", "both"), default="out")
    sp.add_argument("--hold-open", type=float, default=0)
    sp.set_defaults(func=cmd_references)

    sp = sub.add_parser("trace-path", help="walk outbound edges with cycle guard")
    sp.add_argument("--source-server", required=True)
    sp.add_argument("--tenant", required=True)
    sp.add_argument("--cut", type=int, required=True)
    sp.add_argument("--resource-type", required=True)
    sp.add_argument("--id", required=True)
    sp.add_argument("--max-hops", type=int, default=50)
    sp.set_defaults(func=cmd_trace_path)

    sp = sub.add_parser("verify", help="verify stored body digests")
    sp.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except FHError as exc:
        _emit({"status": exc.status, "error": str(exc)})
        return 2
    except FileNotFoundError as exc:
        _emit({"status": "file_not_found", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
