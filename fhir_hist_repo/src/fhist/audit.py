"""Profile-compliance audits over published cuts.

An audit pins one profile package digest, one source, and an explicit set of
published cuts. For every cut it evaluates the *last non-deleted* version of
each resource identity — tombstones are counted as ``deleted`` and never
evaluated, and versions younger than the cut are invisible to it.

Findings are pure functions of ``(package digest, root profiles, resource
version body)`` and are cached under exactly that key, so a resource version
appearing in several cuts is evaluated once while every cut keeps its own
membership and summary. Run state lives in the database: an interrupted run
resumes from persisted members, a repeated identical request folds onto the
stored result, and every report is rebuilt from persisted state on demand.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import blobs, profiles
from .db import ensure_audit_schema, snapshot_tx, write_tx
from .errors import UnknownAudit, UsageError
from .igmodel import (
    ModelError,
    PackageClosure,
    ProfileModel,
    UnknownTypeCode,
    choice_concrete_type,
    coded_pairs,
    deep_equal,
    json_typename,
    pair_in_expansion,
    pattern_match,
    resolve_path,
    resolve_segments,
    single_type_matches,
    split_canonical,
)
from .paths import RepoPaths
from .query import check_cut

RESULT_KINDS = (
    "pass", "fail", "unsupported", "no_profile",
    "profile_unknown", "profile_ambiguous", "profile_type_mismatch",
)

REQUEST_FORMAT = 1


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def _finding(code: str, severity: str, profile: str | None,
             element_id: str | None, path: str, message: str,
             expected: Any = None, actual: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "profile": profile,
        "element_id": element_id,
        "path": path,
        "message": message,
    }
    if expected is not None:
        out["expected"] = expected
    if actual is not None:
        out["actual"] = actual
    return out


class _Eval:
    """Collects findings for one (resource version, profile) evaluation."""

    def __init__(self, closure: PackageClosure, profile_label: str,
                 findings: list[dict[str, Any]]):
        self.closure = closure
        self.profile = profile_label
        self.findings = findings

    def err(self, code: str, element_id: str | None, path: str, message: str,
            expected: Any = None, actual: Any = None) -> None:
        self.findings.append(_finding(code, "error", self.profile, element_id,
                                      path, message, expected, actual))

    def uns(self, code: str, element_id: str | None, path: str, message: str,
            expected: Any = None, actual: Any = None) -> None:
        self.findings.append(_finding(code, "unsupported", self.profile,
                                      element_id, path, message,
                                      expected, actual))


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

def evaluate_body(closure: PackageClosure,
                  root_models: list[tuple[str, ProfileModel]] | None,
                  rtype: str, body: dict[str, Any],
                  ) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Evaluate one resource version body; return (result, selected, findings).

    ``root_models`` is ``None`` in meta.profile selection mode, otherwise the
    pre-resolved caller-specified root profiles.
    """

    findings: list[dict[str, Any]] = []
    problems: list[str] = []
    targets: list[tuple[str, ProfileModel]] = []

    if root_models is not None:
        targets = [(label, model) for label, model in root_models
                   if model.sd_type == rtype]
        if not targets:
            # Explicit roots exist but none constrains this resource type.
            return "no_profile", [], []
    else:
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        entries = [p for p in (meta.get("profile") or [])
                   if isinstance(p, str)]
        if not entries:
            return "no_profile", [], []
        for entry in entries:
            url, ver = split_canonical(entry)
            model = None
            problem = None
            if ver is not None:
                if (url, ver) in closure.sds:
                    try:
                        model = closure.resolve_model(url, ver)
                    except ModelError as exc:
                        problem = ("unsupported_profile_unresolvable",
                                       exc.message)
                else:
                    problem = ("profile_unknown",
                               f"meta.profile entry {entry!r} is not in the "
                               "registered package closure")
            else:
                versions = closure.sd_versions(url)
                if not versions:
                    problem = ("profile_unknown",
                               f"meta.profile entry {entry!r} is not in the "
                               "registered package closure")
                elif len(versions) > 1:
                    problem = ("profile_ambiguous",
                               f"meta.profile entry {entry!r} matches "
                               f"registered versions {versions}; pin a version")
                else:
                    try:
                        model = closure.resolve_model(url, versions[0])
                    except ModelError as exc:
                        problem = ("unsupported_profile_unresolvable",
                                   exc.message)
            if model is not None and model.sd_type != rtype:
                problem = ("profile_type_mismatch",
                           f"meta.profile entry {entry!r} constrains "
                           f"{model.sd_type}, not {rtype}")
                model = None
            if model is not None:
                targets.append((model.label, model))
            elif problem is not None:
                code, message = problem
                problems.append(code)
                findings.append(_finding(code, "unsupported", None, None,
                                         "$.meta.profile", message,
                                         actual=entry))
        if not targets:
            order = {"profile_ambiguous": 0, "profile_unknown": 1,
                     "profile_type_mismatch": 2,
                     "unsupported_profile_unresolvable": 3}
            result = sorted(problems, key=lambda c: order.get(c, 9))[0]
            return result, [], findings

    selected = [label for label, _ in targets]
    for label, model in targets:
        try:
            _eval_model(closure, model, body, findings)
        except ModelError as exc:
            findings.append(_finding(
                exc.code, "unsupported", label, None, "$", exc.message))

    if any(f["severity"] == "error" for f in findings):
        result = "fail"
    elif any(f["severity"] == "unsupported" for f in findings) or problems:
        result = "unsupported"
    else:
        result = "pass"
    return result, selected, findings


# ---------------------------------------------------------------------------
# Element evaluation
# ---------------------------------------------------------------------------

def _eval_model(closure: PackageClosure, model: ProfileModel,
                body: dict[str, Any],
                findings: list[dict[str, Any]]) -> None:
    ev = _Eval(closure, model.label, findings)

    # Partition elements: base elements (no ":" in id) are evaluated at their
    # path; slice roots and slice sub-elements are handled by the slicer.
    base_by_path: dict[str, list] = {}
    slice_roots: dict[str, list] = {}
    sub_by_slice: dict[str, list] = {}
    for elem in model.elements:
        eid = elem.eid
        if ":" not in eid:
            base_by_path.setdefault(elem.path, []).append(elem)
            continue
        head, _, rest = eid.partition(":")
        if "." not in rest:
            slice_roots.setdefault(head, []).append(elem)
        else:
            slice_id = f"{head}:{rest.split('.')[0]}"
            sub_by_slice.setdefault(slice_id, []).append(elem)

    for path in sorted(set(base_by_path) | set(slice_roots)):
        if path == model.sd_type:
            continue  # the root element itself carries no instance checks
        items = resolve_path(body, path)
        elems = base_by_path.get(path, [])
        main = next((e for e in elems if e.slice_name is None),
                    elems[0] if elems else None)
        if main is not None:
            _check_element(ev, main, items)
        slices = slice_roots.get(path)
        if slices:
            _do_slicing(ev, main, slices, sub_by_slice, items, path)


def _check_element(ev: _Eval, elem,
                   items: list[tuple[str, Any, str | None]]) -> None:
    count = len(items)
    if count < elem.min:
        ev.err("min_cardinality", elem.eid, elem.path,
               f"{elem.path}: {count} item(s) present, minimum is {elem.min}",
               expected={"min": elem.min}, actual={"count": count})
    if elem.max != "*":
        try:
            max_int = int(elem.max)
        except ValueError:
            max_int = None
        if max_int is not None and count > max_int:
            ev.err("max_cardinality", elem.eid, elem.path,
                   f"{elem.path}: {count} item(s) present, maximum is {max_int}",
                   expected={"max": max_int}, actual={"count": count})

    in_scope = count > 0 or elem.min > 0
    if in_scope and elem.fhirpath_constraints:
        ev.uns("unsupported_fhirpath_constraint", elem.eid, elem.path,
               "FHIRPath constraint(s) are not evaluated by this audit",
               actual={"expressions": elem.fhirpath_constraints})
    if in_scope and elem.type_profiles:
        ev.uns("unsupported_target_profile", elem.eid, elem.path,
               "type profile / Reference targetProfile constraints are not "
               "evaluated by this audit",
               actual={"profiles": sorted(elem.type_profiles)})

    expansion = None
    if elem.binding_valueset and count > 0:
        url, ver = split_canonical(elem.binding_valueset)
        try:
            pinned = ev.closure.pin_vs_version(url, ver)
            expansion = ev.closure.expand_valueset(url, pinned)
        except ModelError as exc:
            ev.uns(exc.code, elem.eid, elem.path, exc.message)

    allowed = {t.lower() for t in elem.types}
    for loc, value, choice_key in items:
        if elem.is_choice:
            if elem.types and choice_key is not None:
                base = elem.path.split(".")[-1][:-3]
                concrete = choice_concrete_type(choice_key, base)
                if concrete.lower() not in allowed:
                    ev.err("choice_type_not_allowed", elem.eid, loc,
                           f"choice {choice_key!r} is not one of the allowed "
                           f"types {elem.types}",
                           expected={"types": elem.types},
                           actual={"choice": choice_key, "type": concrete})
        elif elem.types:
            try:
                if not any(single_type_matches(value, t) for t in elem.types):
                    ev.err("type_mismatch", elem.eid, loc,
                           f"value does not match allowed types {elem.types}",
                           expected={"types": elem.types},
                           actual={"json_type": json_typename(value)})
            except UnknownTypeCode as exc:
                ev.uns("unsupported_type_code", elem.eid, loc,
                       f"unknown FHIR type code {exc}",
                       actual={"types": elem.types})
        if elem.fixed is not None and not deep_equal(value, elem.fixed[1]):
            ev.err("fixed_mismatch", elem.eid, loc,
                   f"value must equal {elem.fixed[0]}",
                   expected=elem.fixed[1], actual=value)
        if elem.pattern is not None and not pattern_match(elem.pattern[1], value):
            ev.err("pattern_mismatch", elem.eid, loc,
                   f"value must contain pattern {elem.pattern[0]}",
                   expected=elem.pattern[1], actual=value)
        if expansion is not None:
            pairs = coded_pairs(value)
            if not any(pair_in_expansion(p, expansion) for p in pairs):
                codings = sorted(
                    ({"system": s or "", "code": c} for s, c in pairs),
                    key=lambda d: (d["system"], d["code"]),
                )
                ev.err("code_not_in_valueset", elem.eid, loc,
                       "coded value is not in the required value set",
                       expected={"valueSet": elem.binding_valueset,
                                 "expansion_size": len(expansion)},
                       actual={"codings": codings})


def _do_slicing(ev: _Eval, main, slices: list,
                sub_by_slice: dict[str, list],
                items: list[tuple[str, Any, str | None]], path: str) -> None:
    if main is None or main.slicing is None:
        ev.uns("unsupported_slicing_missing_base", None, path,
               f"slices declared at {path} but the sliced element carries no "
               "slicing definition")
        return
    discriminators = main.slicing.get("discriminator") or []
    rules = main.slicing.get("rules", "open")
    if len(discriminators) != 1:
        ev.uns("unsupported_slicing_discriminator", main.eid, path,
               f"slicing with {len(discriminators)} discriminators is not "
               "supported (exactly one is required)")
        return
    disc = discriminators[0] if isinstance(discriminators[0], dict) else {}
    dtype = disc.get("type")
    dpath = disc.get("path") or "$this"
    if dtype not in ("value", "type"):
        ev.uns("unsupported_slicing_discriminator", main.eid, path,
               f"discriminator type {dtype!r} is not supported "
               "(value and type are)")
        return

    # Pre-compute one matcher per slice; any slice the audit cannot interpret
    # makes the whole slicing unsupported rather than silently passing.
    matchers = []
    for s in slices:
        if dtype == "value":
            disc_path = f"{path}.{dpath}"
            disc_elem = next(
                (e for e in sub_by_slice.get(s.eid, [])
                 if e.path == disc_path and e.slice_name is None),
                None,
            )
            if disc_elem is None or (
                    disc_elem.fixed is None and disc_elem.pattern is None):
                ev.uns("unsupported_slicing_discriminator", s.eid, path,
                       f"slice {s.slice_name!r} has no discriminator element "
                       f"{disc_path} with a fixed or pattern value")
                return
            fixed, pattern = disc_elem.fixed, disc_elem.pattern
            segments = dpath.split(".")

            def match(item, _fixed=fixed, _pattern=pattern,
                      _segments=segments):
                _, value, _ = item
                for _, candidate, _ in resolve_segments(value, _segments, "$"):
                    if _fixed is not None and deep_equal(candidate, _fixed[1]):
                        return True
                    if _pattern is not None \
                            and pattern_match(_pattern[1], candidate):
                        return True
                return False
            matchers.append(match)
        else:  # type discriminator
            if dpath not in ("$this", ""):
                ev.uns("unsupported_slicing_discriminator", s.eid, path,
                       "type discriminators are only supported on $this")
                return
            if not s.types:
                ev.uns("unsupported_slicing_discriminator", s.eid, path,
                       f"slice {s.slice_name!r} restricts no types to "
                       "discriminate on")
                return
            allowed = {t.lower() for t in s.types}
            base = path.split(".")[-1][:-3] if path.endswith("[x]") else None
            if base is None:
                ev.uns("unsupported_slicing_discriminator", s.eid, path,
                       "type discriminators are only supported on choice "
                       "(value[x]) elements")
                return

            def match(item, _allowed=allowed, _base=base):
                _, _, choice_key = item
                if choice_key is None:
                    return False
                return choice_concrete_type(choice_key, _base).lower() in _allowed
            matchers.append(match)

    matched: dict[str, list] = {s.eid: [] for s in slices}
    unmatched: list[tuple[str, Any, str | None]] = []
    for item in items:
        target = None
        for s, matcher in zip(slices, matchers):
            if matcher(item):
                target = s
                break
        if target is not None:
            matched[target.eid].append(item)
        else:
            unmatched.append(item)

    if rules == "closed":
        for loc, value, _ in unmatched:
            ev.err("slice_unmatched", main.eid, loc,
                   f"item matches no slice of the closed slicing at {path}",
                   expected={"slices": [s.slice_name for s in slices]},
                   actual=value)
    # open slicing: unmatched items are allowed and were already checked
    # against the sliced element itself by _check_element.

    for s in slices:
        count = len(matched[s.eid])
        if count < s.min:
            ev.err("slice_min", s.eid, path,
                   f"slice {s.slice_name!r}: {count} item(s), minimum {s.min}",
                   expected={"slice": s.slice_name, "min": s.min},
                   actual={"count": count})
        if s.max != "*":
            try:
                max_int = int(s.max)
            except ValueError:
                max_int = None
            if max_int is not None and count > max_int:
                ev.err("slice_max", s.eid, path,
                       f"slice {s.slice_name!r}: {count} item(s), "
                       f"maximum {max_int}",
                       expected={"slice": s.slice_name, "max": max_int},
                       actual={"count": count})
        for sub in sub_by_slice.get(s.eid, []):
            if sub.slice_name is not None:
                ev.uns("unsupported_slicing_nested", sub.eid, path,
                       "slices within slices are not supported")
                continue
            rel = sub.path[len(path) + 1:]
            if not rel or rel == sub.path:
                continue
            for loc, value, _ in matched[s.eid]:
                sub_items = resolve_segments(value, rel.split("."), loc)
                _check_element(ev, sub, sub_items)


# ---------------------------------------------------------------------------
# Audit runs
# ---------------------------------------------------------------------------

def _request_digest(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cache_key(package_digest: str, roots_key: str, source_id: int,
               rtype: str, logical_id: str, version_id: str,
               body_digest: str) -> str:
    material = "\x1f".join([
        package_digest, roots_key, str(source_id), rtype, logical_id,
        version_id, body_digest,
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _resolve_roots(closure: PackageClosure,
                   root_profiles: list[str]) -> list[tuple[str, ProfileModel]]:
    models: list[tuple[str, ProfileModel]] = []
    for ref in root_profiles:
        url, ver = split_canonical(ref)
        if ver is None:
            versions = closure.sd_versions(url)
            if len(versions) != 1:
                raise UsageError(
                    f"root profile {ref!r} resolves to {len(versions)} "
                    "registered versions; pin it as canonical|version")
            ver = versions[0]
        if (url, ver) not in closure.sds:
            raise UsageError(
                f"root profile {ref!r} is not in the package closure")
        try:
            models.append((f"{url}|{ver}", closure.resolve_model(url, ver)))
        except ModelError as exc:
            raise UsageError(
                f"root profile {ref!r} cannot be interpreted: {exc.message}"
            ) from exc
    return models


def run_audit(paths: RepoPaths, conn: sqlite3.Connection,
              source: sqlite3.Row, cuts: list[int], package_ref: str,
              root_profiles: list[str] | None,
              max_evaluations: int | None = None) -> tuple[dict[str, Any], int]:
    """Run or resume an audit; return (payload, exit_code).

    Exit code 0 means the audit is complete (or was already complete); 3
    means the run was interrupted and can be resumed by repeating the call.
    """

    ensure_audit_schema(conn)
    package = profiles.resolve_package_ref(conn, package_ref)
    package_digest = package["package_digest"]
    closure = profiles.load_closure(conn, paths, package_digest)
    root_models = None
    roots_key = ""
    if root_profiles:
        root_models = _resolve_roots(closure, root_profiles)
        roots_key = ",".join(sorted(label for label, _ in root_models))

    request = {
        "format": REQUEST_FORMAT,
        "source_server": source["source_server"],
        "tenant": source["tenant"],
        "cuts": sorted(set(cuts)),
        "package_digest": package_digest,
        "package": f"{package['name']}@{package['version']}",
        "root_profiles": sorted(root_profiles or []),
    }
    audit_id = _request_digest(request)

    with snapshot_tx(conn) as ro:
        for cut in request["cuts"]:
            check_cut(ro, source, cut)

    existing = conn.execute(
        "SELECT * FROM audit_runs WHERE audit_id=?", (audit_id,),
    ).fetchone()
    if existing is not None and existing["state"] == "complete":
        return {
            "status": "already_complete",
            "reused": True,
            "audit_id": audit_id,
            "request": request,
            "summary": json.loads(existing["summary_json"]),
        }, 0

    resumed = existing is not None
    if not resumed:
        try:
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO audit_runs(audit_id, request_json, source_id, "
                    "package_digest, state, frontier_at_start) "
                    "VALUES (?,?,?,?,'running',?)",
                    (audit_id, json.dumps(request, sort_keys=True),
                     source["source_id"], package_digest,
                     source["published_seq"]),
                )
        except sqlite3.IntegrityError:
            # A concurrent identical request created the run first; fold onto
            # it and resume from its persisted state.
            existing = conn.execute(
                "SELECT * FROM audit_runs WHERE audit_id=?", (audit_id,),
            ).fetchone()
            if existing is not None and existing["state"] == "complete":
                return {
                    "status": "already_complete",
                    "reused": True,
                    "audit_id": audit_id,
                    "request": request,
                    "summary": json.loads(existing["summary_json"]),
                }, 0
            resumed = True

    interrupted = False
    evaluations = 0
    for cut in request["cuts"]:
        done = conn.execute(
            "SELECT 1 FROM audit_cut_progress WHERE audit_id=? AND cut_seq=?",
            (audit_id, cut),
        ).fetchone()
        if done is not None:
            continue
        members = _enumerate_members(conn, source["source_id"], cut)
        with write_tx(conn):
            for member in members:
                if member["is_deleted"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO audit_deleted(audit_id, "
                        "cut_seq, resource_type, logical_id, version_id) "
                        "VALUES (?,?,?,?,?)",
                        (audit_id, cut, member["resource_type"],
                         member["logical_id"], member["version_id"]),
                    )
                    continue
                cache_key = _cache_key(
                    package_digest, roots_key, source["source_id"],
                    member["resource_type"], member["logical_id"],
                    member["version_id"], member["body_digest"])
                present = conn.execute(
                    "SELECT 1 FROM audit_members WHERE audit_id=? AND "
                    "cut_seq=? AND cache_key=?",
                    (audit_id, cut, cache_key),
                ).fetchone()
                if present is not None:
                    continue
                cached = conn.execute(
                    "SELECT cache_key FROM eval_cache WHERE cache_key=?",
                    (cache_key,),
                ).fetchone()
                if cached is None:
                    body = blobs.read_blob(paths, member["body_relpath"])
                    result, selected, findings = evaluate_body(
                        closure, root_models, member["resource_type"], body)
                    # Findings are a pure function of the cache key, so a
                    # concurrent identical evaluation may safely fold.
                    conn.execute(
                        "INSERT OR IGNORE INTO eval_cache(cache_key, "
                        "package_digest, roots_key, source_id, resource_type, "
                        "logical_id, version_id, body_digest, result, "
                        "selected_json, findings_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (cache_key, package_digest, roots_key,
                         source["source_id"], member["resource_type"],
                         member["logical_id"], member["version_id"],
                         member["body_digest"], result,
                         json.dumps(selected, sort_keys=True),
                         json.dumps(findings, sort_keys=True)),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO audit_members(audit_id, cut_seq, "
                    "cache_key) VALUES (?,?,?)",
                    (audit_id, cut, cache_key),
                )
                evaluations += 1
                if max_evaluations is not None \
                        and evaluations >= max_evaluations:
                    interrupted = True
                    break
            if not interrupted:
                member_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM audit_members "
                    "WHERE audit_id=? AND cut_seq=?",
                    (audit_id, cut),
                ).fetchone()["n"]
                deleted_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM audit_deleted "
                    "WHERE audit_id=? AND cut_seq=?",
                    (audit_id, cut),
                ).fetchone()["n"]
                conn.execute(
                    "INSERT INTO audit_cut_progress(audit_id, cut_seq, "
                    "member_count, deleted_count) VALUES (?,?,?,?)",
                    (audit_id, cut, member_count, deleted_count),
                )
        if interrupted:
            break

    if interrupted:
        return {
            "status": "interrupted",
            "resumed": resumed,
            "audit_id": audit_id,
            "request": request,
            "progress": _progress(conn, audit_id, request["cuts"]),
        }, 3

    summary = compute_summary(conn, audit_id, request["cuts"])
    with write_tx(conn):
        conn.execute(
            "UPDATE audit_runs SET state='complete', "
            "completed_at=datetime('now'), summary_json=? WHERE audit_id=?",
            (json.dumps(summary, sort_keys=True), audit_id),
        )
    return {
        "status": "complete",
        "resumed": resumed,
        "audit_id": audit_id,
        "request": request,
        "summary": summary,
    }, 0


def _enumerate_members(conn: sqlite3.Connection, source_id: int,
                       cut_seq: int) -> list[sqlite3.Row]:
    """The last version row per identity at the cut, tombstones included."""

    return list(conn.execute(
        """
        SELECT resource_type, logical_id, version_id, is_deleted, body_digest,
               body_relpath
        FROM (
          SELECT rv.*, ROW_NUMBER() OVER (
              PARTITION BY resource_type, logical_id
              ORDER BY seq DESC, tx_ordinal DESC
          ) AS rn
          FROM resource_version rv
          WHERE rv.source_id=? AND rv.seq<=?
        ) WHERE rn=1
        ORDER BY resource_type, logical_id
        """,
        (source_id, cut_seq),
    ))


def _progress(conn: sqlite3.Connection, audit_id: str,
              cuts: list[int]) -> dict[str, Any]:
    done = [r["cut_seq"] for r in conn.execute(
        "SELECT cut_seq FROM audit_cut_progress WHERE audit_id=? "
        "ORDER BY cut_seq", (audit_id,))]
    members = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_members WHERE audit_id=?",
        (audit_id,)).fetchone()["n"]
    return {
        "cuts_done": done,
        "cuts_pending": [c for c in cuts if c not in done],
        "members_recorded": members,
    }


# ---------------------------------------------------------------------------
# Summaries and reports (always rebuilt from persisted state)
# ---------------------------------------------------------------------------

def compute_summary(conn: sqlite3.Connection, audit_id: str,
                    cuts: list[int]) -> dict[str, Any]:
    per_cut: dict[str, dict[str, int]] = {}
    totals = {kind: 0 for kind in RESULT_KINDS}
    totals["deleted"] = 0
    memberships = 0
    by_code: dict[str, int] = {}
    by_rule: dict[tuple[str, str, str], int] = {}

    member_rows = conn.execute(
        "SELECT m.cut_seq, c.result, c.findings_json FROM audit_members m "
        "JOIN eval_cache c ON c.cache_key = m.cache_key "
        "WHERE m.audit_id=? ORDER BY m.cut_seq",
        (audit_id,),
    ).fetchall()
    deleted_rows = conn.execute(
        "SELECT cut_seq, COUNT(*) AS n FROM audit_deleted WHERE audit_id=? "
        "GROUP BY cut_seq",
        (audit_id,),
    ).fetchall()
    deleted_by_cut = {r["cut_seq"]: r["n"] for r in deleted_rows}

    for cut in cuts:
        per_cut[str(cut)] = {kind: 0 for kind in RESULT_KINDS}
        per_cut[str(cut)]["deleted"] = deleted_by_cut.get(cut, 0)

    for row in member_rows:
        cut_key = str(row["cut_seq"])
        if cut_key not in per_cut:
            continue
        result = row["result"]
        per_cut[cut_key][result] = per_cut[cut_key].get(result, 0) + 1
        totals[result] = totals.get(result, 0) + 1
        memberships += 1
        for finding in json.loads(row["findings_json"]):
            by_code[finding["code"]] = by_code.get(finding["code"], 0) + 1
            rule = (finding.get("profile") or "",
                    finding.get("element_id") or "",
                    finding["code"])
            by_rule[rule] = by_rule.get(rule, 0) + 1
    for cut in cuts:
        totals["deleted"] += deleted_by_cut.get(cut, 0)

    unique_evaluations = conn.execute(
        "SELECT COUNT(DISTINCT cache_key) AS n FROM audit_members "
        "WHERE audit_id=?",
        (audit_id,),
    ).fetchone()["n"]

    return {
        "cuts": per_cut,
        "total": totals,
        "memberships": memberships,
        "unique_evaluations": unique_evaluations,
        "by_finding_code": dict(sorted(by_code.items())),
        "by_rule": [
            {"profile": profile or None, "element_id": element or None,
             "code": code, "count": count}
            for (profile, element, code), count in sorted(by_rule.items())
        ],
    }


def _load_run(conn: sqlite3.Connection, audit_id: str) -> sqlite3.Row:
    ensure_audit_schema(conn)
    row = conn.execute(
        "SELECT * FROM audit_runs WHERE audit_id=?", (audit_id,),
    ).fetchone()
    if row is None:
        raise UnknownAudit(f"no audit run with id {audit_id}")
    return row


def list_audits(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_audit_schema(conn)
    out = []
    for row in conn.execute(
            "SELECT * FROM audit_runs ORDER BY created_at, audit_id"):
        request = json.loads(row["request_json"])
        out.append({
            "audit_id": row["audit_id"],
            "state": row["state"],
            "source_server": request["source_server"],
            "tenant": request["tenant"],
            "cuts": request["cuts"],
            "package": request["package"],
            "package_digest": request["package_digest"],
            "root_profiles": request["root_profiles"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        })
    return out


def audit_status(conn: sqlite3.Connection, audit_id: str) -> dict[str, Any]:
    row = _load_run(conn, audit_id)
    request = json.loads(row["request_json"])
    out: dict[str, Any] = {
        "status": "ok",
        "audit_id": row["audit_id"],
        "state": row["state"],
        "request": request,
        "frontier_at_start": row["frontier_at_start"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }
    if row["state"] == "complete":
        out["summary"] = json.loads(row["summary_json"])
    else:
        out["progress"] = _progress(conn, audit_id, request["cuts"])
    return out


def build_report(conn: sqlite3.Connection, audit_id: str,
                 cut: int | None = None,
                 result: str | None = None) -> dict[str, Any]:
    """Rebuild the deterministic report from persisted audit state."""

    row = _load_run(conn, audit_id)
    request = json.loads(row["request_json"])
    cuts = request["cuts"]
    params: list[Any] = [audit_id]
    sql = (
        "SELECT m.cut_seq, c.resource_type, c.logical_id, c.version_id, "
        "c.body_digest, c.result, c.selected_json, c.findings_json "
        "FROM audit_members m JOIN eval_cache c ON c.cache_key = m.cache_key "
        "WHERE m.audit_id=?"
    )
    if cut is not None:
        sql += " AND m.cut_seq=?"
        params.append(cut)
    if result is not None:
        sql += " AND c.result=?"
        params.append(result)
    sql += " ORDER BY m.cut_seq, c.resource_type, c.logical_id"
    resources = []
    for r in conn.execute(sql, params):
        findings = json.loads(r["findings_json"])
        findings.sort(key=lambda f: (f.get("profile") or "",
                                     f.get("element_id") or "",
                                     f["code"], f["path"]))
        resources.append({
            "cut_seq": r["cut_seq"],
            "identity": {
                "source_server": request["source_server"],
                "tenant": request["tenant"],
                "resourceType": r["resource_type"],
                "id": r["logical_id"],
            },
            "versionId": r["version_id"],
            "body_digest": r["body_digest"],
            "result": r["result"],
            "selected_profiles": json.loads(r["selected_json"]),
            "findings": findings,
        })
    deleted = [
        {"cut_seq": d["cut_seq"], "resourceType": d["resource_type"],
         "id": d["logical_id"], "versionId": d["version_id"]}
        for d in conn.execute(
            "SELECT cut_seq, resource_type, logical_id, version_id "
            "FROM audit_deleted WHERE audit_id=? ORDER BY cut_seq, "
            "resource_type, logical_id",
            (audit_id,),
        )
        if cut is None or d["cut_seq"] == cut
    ]
    summary = compute_summary(conn, audit_id, cuts)
    return {
        "status": row["state"],
        "audit_id": row["audit_id"],
        "request": request,
        "frontier_at_start": row["frontier_at_start"],
        "summary": summary,
        "resources": resources,
        "deleted": deleted,
    }
