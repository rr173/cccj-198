"""Reference extraction and structural classification.

This module knows *where* references live in a FHIR resource and *what shape*
they have. It deliberately knows nothing about whether a target exists at a
cut: resolution semantics live in :mod:`fhist.resolver`.

Reference shapes recognised for the same source:

* ``Type/id``                                  relative, unversioned
* ``Type/id/_history/versionId``               relative, versioned
* an absolute URL under the canonical base or
  an explicitly configured equivalent alias    absolute, same source
* ``#fragment``                                contained resource, scoped to
                                                the containing resource version

Everything else (``urn:``, ``identifier:``, malformed strings, absolute URLs
on a foreign host) is classified as ``external`` or ``invalid``; a foreign
host is never joined by guessing from a trailing id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urlsplit

FHIR_ID_RE = re.compile(r"[A-Za-z0-9\-.]{1,64}\Z")
RELATIVE_RE = re.compile(
    r"(?P<type>[A-Za-z][A-Za-z0-9]{1,63})/"
    r"(?P<id>[A-Za-z0-9\-.]{1,64})"
    r"(?:/_history/(?P<vid>[A-Za-z0-9\-.]{1,64}))?\Z"
)


@dataclass(frozen=True)
class RefShape:
    """Structural classification of one raw reference string."""

    kind: str  # relative|absolute|contained|external|invalid
    versioned: bool
    target_type: str | None = None
    target_id: str | None = None
    target_version: str | None = None
    reason: str | None = None


def classify_ref(raw: str, alias_bases: set[str]) -> RefShape:
    """Classify a reference string without consulting the database."""

    if not isinstance(raw, str) or not raw:
        return RefShape(kind="invalid", versioned=False, reason="empty reference")
    text = raw.strip()
    if not text:
        return RefShape(kind="invalid", versioned=False, reason="blank reference")

    if text.startswith("#"):
        frag = text[1:]
        if frag and FHIR_ID_RE.match(frag):
            return RefShape(
                kind="contained", versioned=False, target_id=frag
            )
        return RefShape(kind="invalid", versioned=False, reason="bad fragment id")

    if "://" in text:
        no_frag, _ = urldefrag(text)
        parts = urlsplit(no_frag)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return RefShape(
                kind="invalid", versioned=False, reason="unsupported URL scheme"
            )
        base = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        matched_base = next(
            (b for b in sorted(alias_bases, key=len, reverse=True)
             if base == b or base.startswith(b + "/")),
            None,
        )
        if matched_base is None:
            return RefShape(
                kind="external",
                versioned=False,
                reason="absolute URL host/base is not a configured alias",
            )
        tail = base[len(matched_base):].lstrip("/")
        return _classify_relative_tail(tail, absolute=True)

    if text.startswith(("urn:", "identifier:", "http:", "https:")):
        return RefShape(kind="external", versioned=False, reason="non-resolvable scheme")
    if text.startswith("/"):
        return RefShape(
            kind="invalid",
            versioned=False,
            reason="host-relative URL without a base is not accepted",
        )
    return _classify_relative_tail(text, absolute=False)


def _classify_relative_tail(tail: str, absolute: bool) -> RefShape:
    m = RELATIVE_RE.match(tail)
    if not m:
        return RefShape(
            kind="invalid",
            versioned=False,
            reason="reference is not Type/id or Type/id/_history/versionId",
        )
    return RefShape(
        kind="absolute" if absolute else "relative",
        versioned=bool(m.group("vid")),
        target_type=m.group("type"),
        target_id=m.group("id"),
        target_version=m.group("vid"),
    )


@dataclass
class Edge:
    """One reference extracted from a resource version."""

    container: str          # logical id of the owner, or "<owner>#<containedId>"
    loc: str                # JSON-path-like source location inside the version
    ref_text: str
    shape: RefShape


def extract_edges(resource: dict[str, Any],
                  alias_bases: set[str] | None = None) -> list[Edge]:
    """Walk one full PUT resource body and collect every Reference.

    Resources listed under ``contained`` are walked separately with their own
    fragment scope: a ``#x`` inside one owner never merges with a ``#x``
    inside another resource version.
    """

    alias_bases = alias_bases or set()
    edges: list[Edge] = []
    logical_id = str(resource.get("id", ""))
    contained = resource.get("contained")
    contained_ids: set[str] = set()
    if isinstance(contained, list):
        for idx, item in enumerate(contained):
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if isinstance(cid, str) and cid:
                contained_ids.add(cid)
                container = f"{logical_id}#{cid}"
                _walk(item, f"contained[{idx}]", container, edges, alias_bases)
    _walk_without_contained(resource, "$", logical_id, edges, alias_bases)
    return edges


def _make_edge(container: str, loc: str, ref_text: str,
               alias_bases: set[str]) -> Edge:
    return Edge(container, loc, ref_text, classify_ref(ref_text, alias_bases))


def _walk_without_contained(node: Any, path: str, container: str,
                            edges: list[Edge], alias_bases: set[str]) -> None:
    if isinstance(node, dict):
        if "reference" in node and isinstance(node["reference"], str):
            edges.append(_make_edge(container, f"{path}.reference",
                                    node["reference"], alias_bases))
        for key, value in node.items():
            if key == "contained":
                continue  # contained subtrees get their own scoped walk
            _walk_without_contained(value, f"{path}.{key}", container,
                                    edges, alias_bases)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _walk_without_contained(item, f"{path}[{idx}]", container,
                                    edges, alias_bases)


def _walk(node: Any, path: str, container: str, edges: list[Edge],
          alias_bases: set[str]) -> None:
    if isinstance(node, dict):
        if "reference" in node and isinstance(node["reference"], str):
            edges.append(_make_edge(container, f"{path}.reference",
                                    node["reference"], alias_bases))
        for key, value in node.items():
            _walk(value, f"{path}.{key}", container, edges, alias_bases)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _walk(item, f"{path}[{idx}]", container, edges, alias_bases)
