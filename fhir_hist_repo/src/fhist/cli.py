"""Command-line entry point for the FHIR R4 history object store.

Subcommands: init, stage, advance, status, resource, references, trace-path,
verify, profile (register/list/show), audit (run/list/status/findings/report),
bedreport (publish/list/show/verify/evidence) and bedload.
All output is JSON on stdout; failures carry a stable ``status`` string and a
non-zero exit code. Run with ``python -I`` from any directory.

Exit codes: 0 = success (a completed audit with findings is still a
success), 2 = operational/usage error, 3 = audit run interrupted and
resumable.
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

from fhist import audit as audit_mod  # noqa: E402
from fhist import bedload as bedload_mod  # noqa: E402
from fhist import bedreport as bedreport_mod  # noqa: E402
from fhist import identity as identity_mod  # noqa: E402
from fhist import ingest, profiles, query, status as status_mod  # noqa: E402
from fhist.config import RepoConfig  # noqa: E402
from fhist.db import connect, ensure_identity_schema, initialise  # noqa: E402
from fhist.errors import FHError, UsageError  # noqa: E402
from fhist.intake import intake_package  # noqa: E402
from fhist.paths import RepoPaths  # noqa: E402


def _emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _open_repo(args: argparse.Namespace) -> tuple[RepoPaths, RepoConfig, Any]:
    if not getattr(args, "repo", None):
        raise UsageError("--repo is required for this command")
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


# ---------------------------------------------------------------------------
# Profile packages and audits
# ---------------------------------------------------------------------------

def cmd_profile_register(args: argparse.Namespace) -> None:
    paths, _config, conn = _open_repo(args)
    try:
        _emit(profiles.register_package(paths, conn, args.package_dir))
    finally:
        conn.close()


def cmd_profile_list(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        _emit({"status": "ok", "packages": profiles.list_packages(conn)})
    finally:
        conn.close()


def cmd_profile_show(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        out = profiles.show_package(conn, args.package)
        out["status"] = "ok"
        _emit(out)
    finally:
        conn.close()


def cmd_audit_run(args: argparse.Namespace) -> int:
    paths, _config, conn = _open_repo(args)
    try:
        source = query.load_source(conn, args.source_server, args.tenant)
        payload, rc = audit_mod.run_audit(
            paths, conn, source, args.cut, args.package,
            args.root_profile, max_evaluations=args.max_evaluations)
        _emit(payload)
        return rc
    finally:
        conn.close()


def cmd_audit_list(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        _emit({"status": "ok", "audits": audit_mod.list_audits(conn)})
    finally:
        conn.close()


def cmd_audit_status(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        _emit(audit_mod.audit_status(conn, args.audit_id))
    finally:
        conn.close()


def cmd_audit_findings(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        report = audit_mod.build_report(conn, args.audit_id, cut=args.cut,
                                        result=args.result)
        _emit({
            "status": "ok",
            "audit_id": report["audit_id"],
            "state": report["status"],
            "resources": report["resources"],
        })
    finally:
        conn.close()


def cmd_audit_report(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        report = audit_mod.build_report(conn, args.audit_id)
        if args.out:
            Path(args.out).write_text(
                json.dumps(report, ensure_ascii=False, indent=2,
                           sort_keys=True) + "\n",
                encoding="utf-8")
            _emit({"status": "exported", "audit_id": report["audit_id"],
                   "out": args.out})
        else:
            _emit(report)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bed daily-report publishing and downstream loading
# ---------------------------------------------------------------------------

def cmd_bedreport_publish(args: argparse.Namespace) -> None:
    paths, config, conn = _open_repo(args)
    try:
        _emit(bedreport_mod.publish(
            paths, conn, config,
            source_server=args.source_server, tenant=args.tenant,
            cut_seq=args.cut, tz_name=args.timezone,
            date_from=args.from_date, date_to=args.to_date,
            calendar_path=args.calendar,
            base_release_ref=args.base_release,
            reports_root=args.reports_root))
    finally:
        conn.close()


def cmd_bedreport_list(args: argparse.Namespace) -> None:
    if args.reports_root:
        root = args.reports_root
    else:
        paths, _config, conn = _open_repo(args)
        conn.close()
        root = paths.root / "bedreports"
    _emit(bedreport_mod.list_reports(root))


def cmd_bedreport_show(args: argparse.Namespace) -> None:
    manifest = bedreport_mod.load_release_manifest(args.release)
    out = dict(manifest)
    out["status"] = "ok"
    _emit(out)


def cmd_bedreport_verify(args: argparse.Namespace) -> None:
    manifest = bedreport_mod.load_release_manifest(args.release)
    problems = bedreport_mod.verify_release_files(args.release, manifest)
    problems += bedreport_mod.verify_facts_file(args.release, manifest)
    _emit({"status": "ok" if not problems else "corrupt",
           "release": args.release,
           "release_id": manifest["release"]["release_id"],
           "problems": problems})


def cmd_bedreport_evidence(args: argparse.Namespace) -> None:
    _emit(bedreport_mod.read_evidence(
        args.release, args.section, ward=args.ward, date=args.date,
        bed=args.bed, kind=args.kind, limit=args.limit))


def cmd_bedload(args: argparse.Namespace) -> None:
    if args.status:
        _emit(bedload_mod.load_status(args.db))
        return
    if not args.release:
        raise UsageError("bedload requires --release DIR (or --status)")
    _emit(bedload_mod.load_release(args.db, args.release))


# ---------------------------------------------------------------------------
# Cross-source patient identity dimension
# ---------------------------------------------------------------------------

def _parse_source_key(text: str, flag: str) -> tuple[str, str, str]:
    """Parse ``source_server|tenant|id`` into a complete source key."""

    parts = text.split("|")
    if len(parts) != 3 or not all(parts):
        raise UsageError(
            f"{flag} must be 'source_server|tenant|id', got {text!r}")
    return (parts[0], parts[1], parts[2])


def _parse_cut_vector(texts: list[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for text in texts:
        parts = text.split("|")
        if len(parts) != 3 or not all(parts):
            raise UsageError(
                f"--cut must be 'source_server|tenant|cut', got {text!r}")
        try:
            cut = int(parts[2])
        except ValueError as exc:
            raise UsageError(f"--cut needs an integer cut: {text!r}") from exc
        out.append({"source_server": parts[0], "tenant": parts[1], "cut": cut})
    return out


def _read_ndjson_rows(path: str | None) -> list[dict]:
    if not path:
        return []
    from fhist.jsonl import iter_jsonl

    rows: list[dict] = []
    for row in iter_jsonl(path):
        rows.append(row)
    return rows


def cmd_identity_publish(args: argparse.Namespace) -> None:
    paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        sources = _parse_cut_vector(args.cut_vector or [])
        corrections = _read_ndjson_rows(args.corrections)
        out = identity_mod.publish(
            conn, release_no=args.release, parent=args.parent,
            sources=sources, date_start=args.from_date, date_end=args.to_date,
            corrections=corrections, interrupt=args.interrupt)
        _emit(out)
    finally:
        conn.close()


def _read_snapshot(conn, produce, hold_open: float = 0):
    """Run one read against a single pinned SQLite snapshot."""

    from fhist.db import snapshot_tx
    with snapshot_tx(conn):
        out = produce(conn)
        if hold_open:
            _hold(hold_open)
    return out


def cmd_identity_list(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        _emit(_read_snapshot(conn, identity_mod.list_releases))
    finally:
        conn.close()


def cmd_identity_show(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)

        def produce(ro):
            out = identity_mod.release_info(ro, args.release)
            out["status"] = "ok"
            return out

        _emit(_read_snapshot(conn, produce))
    finally:
        conn.close()


def cmd_identity_assign(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        server, tenant, pid = _parse_source_key(args.key, "--key")
        _emit(_read_snapshot(
            conn,
            lambda ro: identity_mod.assign(ro, args.release, server, tenant,
                                           pid, args.date),
            args.hold_open))
    finally:
        conn.close()


def cmd_identity_members(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        _emit(_read_snapshot(
            conn,
            lambda ro: identity_mod.members_by_unified(
                ro, args.release, args.unified_key, args.date)))
    finally:
        conn.close()


def cmd_identity_status(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        server, tenant, pid = _parse_source_key(args.key, "--key")
        _emit(_read_snapshot(
            conn,
            lambda ro: identity_mod.status_range(
                ro, args.release, server, tenant, pid, args.from_date,
                args.to_date)))
    finally:
        conn.close()


def cmd_identity_decision(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        _emit(_read_snapshot(
            conn,
            lambda ro: identity_mod.decision_info(
                ro, args.release, args.decision_id, args.date)))
    finally:
        conn.close()


def cmd_identity_verify(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        left = _parse_source_key(args.left, "--left")
        right = _parse_source_key(args.right, "--right")
        _emit(_read_snapshot(
            conn,
            lambda ro: identity_mod.verify_pair(ro, args.release, args.date,
                                                left, right),
            args.hold_open))
    finally:
        conn.close()


def cmd_identity_stats(args: argparse.Namespace) -> None:
    _paths, _config, conn = _open_repo(args)
    try:
        ensure_identity_schema(conn)
        _emit(_read_snapshot(
            conn, lambda ro: identity_mod.stats(ro, args.release)))
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fhist",
                                description="FHIR R4 offline history object store")
    p.add_argument("--repo", required=False,
                   help="repository root directory (not needed for "
                        "bedreport show/verify/evidence or bedload)")
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

    sp = sub.add_parser("profile", help="profile package registry")
    psub = sp.add_subparsers(dest="profile_command", required=True)
    pp = psub.add_parser("register", help="register a local profile package")
    pp.add_argument("--package-dir", required=True,
                    help="directory with package.json + conformance JSON")
    pp.set_defaults(func=cmd_profile_register)
    pp = psub.add_parser("list", help="list registered profile packages")
    pp.set_defaults(func=cmd_profile_list)
    pp = psub.add_parser("show", help="show one registered package")
    pp.add_argument("--package", required=True,
                    help="package digest or name:version")
    pp.set_defaults(func=cmd_profile_show)

    sp = sub.add_parser("audit", help="profile-compliance audits over cuts")
    asub = sp.add_subparsers(dest="audit_command", required=True)
    ap = asub.add_parser("run", help="run or resume an audit")
    ap.add_argument("--source-server", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--cut", type=int, required=True, action="append",
                    help="published cut to audit; repeatable")
    ap.add_argument("--package", required=True,
                    help="profile package digest or name:version")
    ap.add_argument("--root-profile", action="append", default=None,
                    help="explicit audit root profile canonical[|version]; "
                         "repeatable. Default: select via meta.profile")
    ap.add_argument("--max-evaluations", type=int, default=None,
                    help="test hook: stop after N new evaluations (exit 3)")
    ap.set_defaults(func=cmd_audit_run)
    ap = asub.add_parser("list", help="list audit runs")
    ap.set_defaults(func=cmd_audit_list)
    ap = asub.add_parser("status", help="show audit run state/progress")
    ap.add_argument("--audit-id", required=True)
    ap.set_defaults(func=cmd_audit_status)
    ap = asub.add_parser("findings", help="per-resource findings of an audit")
    ap.add_argument("--audit-id", required=True)
    ap.add_argument("--cut", type=int, default=None)
    ap.add_argument("--result", default=None,
                    help="filter by result (pass/fail/unsupported/...)")
    ap.set_defaults(func=cmd_audit_findings)
    ap = asub.add_parser("report", help="export the deterministic JSON report")
    ap.add_argument("--audit-id", required=True)
    ap.add_argument("--out", help="write the report to this file instead")
    ap.set_defaults(func=cmd_audit_report)

    sp = sub.add_parser("bedreport", help="bed daily-report publishing")
    bsub = sp.add_subparsers(dest="bedreport_command", required=True)
    bp = bsub.add_parser("publish",
                         help="publish a day-fact partition release")
    bp.add_argument("--source-server", required=True)
    bp.add_argument("--tenant", required=True)
    bp.add_argument("--cut", type=int, required=True,
                    help="published cut to read encounters/locations at")
    bp.add_argument("--timezone", required=True,
                    choices=("UTC", "America/New_York"))
    bp.add_argument("--from-date", required=True,
                    help="first local date, YYYY-MM-DD (inclusive)")
    bp.add_argument("--to-date", required=True,
                    help="end local date, YYYY-MM-DD (exclusive)")
    bp.add_argument("--calendar", required=True,
                    help="bed-open calendar CSV: bed_id,ward_code,start,end")
    bp.add_argument("--base-release",
                    help="correction base: release dir, seq or release id "
                         "of a previous release of this series")
    bp.add_argument("--reports-root",
                    help="delivery root (default: <repo>/bedreports)")
    bp.set_defaults(func=cmd_bedreport_publish)
    bp = bsub.add_parser("list", help="list series and releases")
    bp.add_argument("--reports-root",
                    help="delivery root (default: <repo>/bedreports)")
    bp.set_defaults(func=cmd_bedreport_list)
    bp = bsub.add_parser("show", help="print a release manifest")
    bp.add_argument("--release", required=True, help="release directory")
    bp.set_defaults(func=cmd_bedreport_show)
    bp = bsub.add_parser("verify",
                         help="re-verify a (possibly moved) release directory")
    bp.add_argument("--release", required=True, help="release directory")
    bp.set_defaults(func=cmd_bedreport_verify)
    bp = bsub.add_parser("evidence",
                         help="read audit evidence of a release")
    bp.add_argument("--release", required=True, help="release directory")
    bp.add_argument("--section", default="slices",
                    choices=("slices", "exceptions", "unassigned", "facts"))
    bp.add_argument("--ward", help="filter by ward_code")
    bp.add_argument("--date", help="filter by local date YYYY-MM-DD")
    bp.add_argument("--bed", help="filter by bed_id")
    bp.add_argument("--kind", help="filter by kind (empty/single/conflict "
                                   "or exception kind)")
    bp.add_argument("--limit", type=int, default=1000)
    bp.set_defaults(func=cmd_bedreport_evidence)

    sp = sub.add_parser("bedload",
                        help="load bed-report releases into SQLite "
                             "(repository-independent)")
    sp.add_argument("--db", required=True, help="target SQLite database")
    sp.add_argument("--release", help="release directory to load")
    sp.add_argument("--status", action="store_true",
                    help="show loader state instead of loading")
    sp.set_defaults(func=cmd_bedload)

    sp = sub.add_parser(
        "identity", help="cross-source patient identity dimension")
    isub = sp.add_subparsers(dest="identity_command", required=True)

    ip = isub.add_parser("publish", help="publish an identity-dimension release")
    ip.add_argument("--release", type=int, required=True,
                    help="caller-given unique publication number")
    ip.add_argument("--parent", type=int, default=None,
                    help="parent release number (omit for the first release)")
    ip.add_argument("--cut", dest="cut_vector", action="append", default=[],
                    metavar="SOURCE|TENANT|CUT",
                    help="one participating source's complete cut; repeatable")
    ip.add_argument("--from-date", required=True,
                    help="first business date, YYYY-MM-DD (inclusive)")
    ip.add_argument("--to-date", required=True,
                    help="last business date, YYYY-MM-DD (exclusive)")
    ip.add_argument("--corrections",
                    help="NDJSON file: one adjudication per line")
    ip.add_argument("--interrupt", choices=("exit",), default=None,
                    help="test hook: die after writing, before COMMIT")
    ip.set_defaults(func=cmd_identity_publish)

    ip = isub.add_parser("list", help="list published identity releases")
    ip.set_defaults(func=cmd_identity_list)

    ip = isub.add_parser("show", help="show one identity release")
    ip.add_argument("--release", type=int, required=True)
    ip.set_defaults(func=cmd_identity_show)

    ip = isub.add_parser("assign", help="attribution of a source key on a date")
    ip.add_argument("--release", type=int, required=True)
    ip.add_argument("--key", required=True, metavar="SOURCE|TENANT|ID")
    ip.add_argument("--date", required=True)
    ip.add_argument("--hold-open", type=float, default=0,
                    help="test hook: hold the read snapshot open for N seconds")
    ip.set_defaults(func=cmd_identity_assign)

    ip = isub.add_parser("members",
                         help="enumerate members of a unified key on a date")
    ip.add_argument("--release", type=int, required=True)
    ip.add_argument("--unified-key", required=True)
    ip.add_argument("--date", required=True)
    ip.set_defaults(func=cmd_identity_members)

    ip = isub.add_parser("status",
                         help="status/attribution runs of a source key")
    ip.add_argument("--release", type=int, required=True)
    ip.add_argument("--key", required=True, metavar="SOURCE|TENANT|ID")
    ip.add_argument("--from-date", required=True)
    ip.add_argument("--to-date", required=True,
                    help="exclusive end date")
    ip.set_defaults(func=cmd_identity_status)

    ip = isub.add_parser("decision",
                         help="explain one adjudication and its participation")
    ip.add_argument("--release", type=int, required=True)
    ip.add_argument("--decision-id", required=True)
    ip.add_argument("--date")
    ip.set_defaults(func=cmd_identity_decision)

    ip = isub.add_parser("verify",
                         help="same/different check of two keys on a date")
    ip.add_argument("--release", type=int, required=True)
    ip.add_argument("--date", required=True)
    ip.add_argument("--left", required=True, metavar="SOURCE|TENANT|ID")
    ip.add_argument("--right", required=True, metavar="SOURCE|TENANT|ID")
    ip.add_argument("--hold-open", type=float, default=0,
                    help="test hook: hold the read snapshot open for N seconds")
    ip.set_defaults(func=cmd_identity_verify)

    ip = isub.add_parser("stats", help="per-release processing statistics")
    ip.add_argument("--release", type=int, required=True)
    ip.set_defaults(func=cmd_identity_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        return result if isinstance(result, int) else 0
    except FHError as exc:
        _emit({"status": exc.status, "error": str(exc)})
        return 2
    except FileNotFoundError as exc:
        _emit({"status": "file_not_found", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
