"""Local implementation-guide model: profiles, value sets and code systems.

Everything here is built from *registered* profile package bytes held in the
repository's content-addressed storage. Nothing is fetched from the network;
a canonical that is not present in the registered package closure is unknown
and surfaces as an explicit ``unsupported`` outcome, never as a pass.

Supported constraint subset (anything else is reported as unsupported):

* ``baseDefinition`` inheritance merged over locally registered ancestors
  (external bases such as the FHIR core definitions are terminal: only the
  constraints the package itself states are evaluated)
* element paths, choice (``value[x]``) type restrictions, min/max cardinality
* ``fixed[x]`` and ``pattern[x]``
* closed/open slicing with a single ``value`` or ``type`` discriminator
* required ValueSet bindings expanded from ``compose`` include/exclude
  against locally registered CodeSystems

The module is deliberately free of repository/SQLite knowledge so the model
can be diffed against an independent implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ModelError(Exception):
    """The guideline model cannot be interpreted with the supported subset."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UnknownTypeCode(Exception):
    """An element references a FHIR type code outside the known R4 set."""


def split_canonical(text: str) -> tuple[str, str | None]:
    """Split ``url|version``; a bare url yields ``(url, None)``."""

    if "|" in text:
        url, ver = text.rsplit("|", 1)
        return url, ver or None
    return text, None


# ---------------------------------------------------------------------------
# Element model
# ---------------------------------------------------------------------------

@dataclass
class ElementModel:
    eid: str
    path: str
    slice_name: str | None
    min: int
    max: str
    types: list[str]
    type_profiles: list[str]
    fixed: tuple[str, Any] | None
    pattern: tuple[str, Any] | None
    binding_valueset: str | None          # only required bindings are kept
    slicing: dict | None
    fhirpath_constraints: list[str]
    is_choice: bool


@dataclass
class ProfileModel:
    canonical: str
    version: str
    sd_type: str
    elements: list[ElementModel]
    raw_elements: list[dict] = field(repr=False, default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.canonical}|{self.version}"


def parse_element(raw: dict) -> ElementModel:
    eid = raw.get("id") or raw["path"]
    path = raw["path"]
    fixed = None
    pattern = None
    for key, value in raw.items():
        if key.startswith("fixed") and fixed is None:
            fixed = (key, value)
        elif key.startswith("pattern") and pattern is None:
            pattern = (key, value)
    binding_vs = None
    binding = raw.get("binding")
    if isinstance(binding, dict) and binding.get("strength") == "required" \
            and isinstance(binding.get("valueSet"), str):
        binding_vs = binding["valueSet"]
    types: list[str] = []
    type_profiles: list[str] = []
    for t in raw.get("type", []) or []:
        if not isinstance(t, dict):
            continue
        if isinstance(t.get("code"), str):
            types.append(t["code"])
        for key in ("profile", "targetProfile"):
            for p in t.get(key, []) or []:
                if isinstance(p, str):
                    type_profiles.append(p)
    constraints = [
        c.get("expression", "")
        for c in raw.get("constraint", []) or []
        if isinstance(c, dict)
    ]
    return ElementModel(
        eid=eid,
        path=path,
        slice_name=raw.get("sliceName"),
        min=int(raw.get("min", 0) or 0),
        max=str(raw.get("max", "*")),
        types=types,
        type_profiles=type_profiles,
        fixed=fixed,
        pattern=pattern,
        binding_valueset=binding_vs,
        slicing=raw.get("slicing") if isinstance(raw.get("slicing"), dict) else None,
        fhirpath_constraints=constraints,
        is_choice=path.endswith("[x]"),
    )


def merge_elements(parent: list[dict], child: list[dict]) -> list[dict]:
    """Merge child element statements over the parent's, keyed by element id.

    A key the child states replaces the parent's; anything the child leaves
    unstated is inherited. This single rule covers both snapshot children
    (which restate everything) and differential children (which state only
    changes), because a profile can only tighten its base.
    """

    merged: dict[str, dict] = {}
    order: list[str] = []
    for raw in parent:
        eid = raw.get("id") or raw.get("path")
        if eid is None:
            continue
        merged[eid] = dict(raw)
        order.append(eid)
    for raw in child:
        eid = raw.get("id") or raw.get("path")
        if eid is None:
            continue
        if eid in merged:
            merged[eid].update(raw)
        else:
            merged[eid] = dict(raw)
            order.append(eid)
    return [merged[eid] for eid in order]


# ---------------------------------------------------------------------------
# Package closure
# ---------------------------------------------------------------------------

class PackageClosure:
    """Immutable view of one registered package plus its dependency closure."""

    def __init__(self, package_digest: str,
                 sds: dict[tuple[str, str], dict],
                 vss: dict[tuple[str, str], dict],
                 css: dict[tuple[str, str], dict]):
        self.package_digest = package_digest
        self.sds = dict(sds)
        self.vss = dict(vss)
        self.css = dict(css)
        self._sd_index = _index(self.sds)
        self._vs_index = _index(self.vss)
        self._cs_index = _index(self.css)
        self._model_cache: dict[tuple[str, str], ProfileModel] = {}
        self._expansion_cache: dict[tuple[str, str], set[tuple[str | None, str]]] = {}

    # -- StructureDefinitions ---------------------------------------------

    def sd_versions(self, url: str) -> list[str]:
        return list(self._sd_index.get(url, ()))

    def resolve_model(self, url: str, version: str) -> ProfileModel:
        key = (url, version)
        if key not in self._model_cache:
            self._model_cache[key] = self._resolve_model(url, version, ())
        return self._model_cache[key]

    def _resolve_model(self, url: str, version: str,
                       visiting: tuple[tuple[str, str], ...]) -> ProfileModel:
        if (url, version) in visiting:
            raise ModelError(
                "unsupported_profile_unresolvable",
                f"baseDefinition cycle reaches {url}|{version}")
        raw = self.sds.get((url, version))
        if raw is None:
            raise ModelError(
                "unsupported_profile_unresolvable",
                f"StructureDefinition {url}|{version} is not in the "
                "registered package closure")
        own = raw.get("snapshot", {}).get("element") \
            or raw.get("differential", {}).get("element") or []
        own = [dict(e) for e in own
               if isinstance(e, dict) and isinstance(e.get("path"), str)]
        base_ref = raw.get("baseDefinition")
        merged_raw = own
        if isinstance(base_ref, str) and base_ref:
            burl, bver = split_canonical(base_ref)
            local_versions = self.sd_versions(burl)
            if bver is None:
                if len(local_versions) == 1:
                    bver = local_versions[0]
                elif len(local_versions) > 1:
                    raise ModelError(
                        "unsupported_profile_unresolvable",
                        f"baseDefinition {burl!r} is ambiguous across "
                        f"registered versions {local_versions}")
            if bver is not None and (burl, bver) in self.sds:
                parent = self._resolve_model(burl, bver,
                                             visiting + ((url, version),))
                merged_raw = merge_elements(parent.raw_elements, own)
            elif local_versions:
                raise ModelError(
                    "unsupported_profile_unresolvable",
                    f"baseDefinition {burl}|{bver} is not registered; only "
                    f"versions {local_versions} are available offline")
            # else: external base (e.g. FHIR core) — evaluate the constraints
            # this package states itself; nothing else is knowable offline.
        elements = [parse_element(e) for e in merged_raw]
        sd_type = raw.get("type")
        if not isinstance(sd_type, str) or not sd_type:
            raise ModelError("unsupported_profile_unresolvable",
                             f"StructureDefinition {url}|{version} has no type")
        return ProfileModel(canonical=url, version=version, sd_type=sd_type,
                            elements=elements, raw_elements=merged_raw)

    # -- ValueSets ----------------------------------------------------------

    def pin_vs_version(self, url: str, version: str | None) -> str:
        return _pin(self._vs_index, url, version,
                    ambiguous_code="unsupported_valueset_ambiguous",
                    unknown_code="unsupported_valueset_unresolved",
                    kind="ValueSet")

    def expand_valueset(self, url: str, version: str) -> set[tuple[str | None, str]]:
        key = (url, version)
        if key not in self._expansion_cache:
            self._expansion_cache[key] = self._expand(url, version, ())
        return self._expansion_cache[key]

    def _expand(self, url: str, version: str,
                visiting: tuple[tuple[str, str], ...]) -> set[tuple[str | None, str]]:
        if (url, version) in visiting:
            raise ModelError("unsupported_valueset_cycle",
                             f"ValueSet compose cycle reaches {url}|{version}")
        vs = self.vss.get((url, version))
        if vs is None:
            raise ModelError("unsupported_valueset_unresolved",
                             f"ValueSet {url}|{version} is not registered")
        compose = vs.get("compose")
        if not isinstance(compose, dict):
            expansion = vs.get("expansion")
            if isinstance(expansion, dict) \
                    and isinstance(expansion.get("contains"), list):
                return {
                    (c.get("system"), c.get("code"))
                    for c in expansion["contains"]
                    if isinstance(c, dict) and isinstance(c.get("code"), str)
                }
            raise ModelError("unsupported_valueset_compose",
                             f"ValueSet {url}|{version} has neither compose "
                             "nor a stored expansion")
        acc: set[tuple[str | None, str]] = set()
        for inc in compose.get("include", []) or []:
            acc |= self._expand_include(inc, visiting + ((url, version),))
        for exc in compose.get("exclude", []) or []:
            acc -= self._expand_include(exc, visiting + ((url, version),))
        return acc

    def _expand_include(self, inc: Any,
                        visiting: tuple[tuple[str, str], ...]
                        ) -> set[tuple[str | None, str]]:
        if not isinstance(inc, dict):
            raise ModelError("unsupported_valueset_compose",
                             "compose include/exclude entry is not an object")
        acc: set[tuple[str | None, str]] = set()
        refs = inc.get("valueSet") or []
        for ref in refs:
            if not isinstance(ref, str):
                raise ModelError("unsupported_valueset_compose",
                                 "include.valueSet entries must be canonicals")
            rurl, rver = split_canonical(ref)
            rver = self.pin_vs_version(rurl, rver)
            acc |= self._expand(rurl, rver, visiting)
        system = inc.get("system")
        if system is not None:
            if inc.get("filter"):
                raise ModelError(
                    "unsupported_valueset_filter",
                    "ValueSet compose filters need terminology reasoning that "
                    "is not available offline")
            concepts = inc.get("concept")
            if concepts is not None:
                for c in concepts:
                    if isinstance(c, dict) and isinstance(c.get("code"), str):
                        acc.add((system, c["code"]))
            else:
                cs_url, cs_ver = split_canonical(system)
                ver = inc.get("version") or cs_ver
                cs = self._lookup_codesystem(cs_url, ver)
                if cs is None:
                    raise ModelError(
                        "unsupported_codesystem_unresolved",
                        f"CodeSystem {system} is not registered; cannot "
                        "enumerate its concepts offline")
                for code in walk_codesystem(cs):
                    acc.add((cs_url, code))
        if system is None and not refs:
            raise ModelError("unsupported_valueset_compose",
                             "compose include names neither system nor valueSet")
        return acc

    def _lookup_codesystem(self, url: str, version: str | None) -> dict | None:
        if version is not None:
            return self.css.get((url, version))
        versions = self._cs_index.get(url, [])
        if len(versions) == 1:
            return self.css[(url, versions[0])]
        if len(versions) > 1:
            raise ModelError(
                "unsupported_codesystem_ambiguous",
                f"CodeSystem {url} is registered in versions {versions}; "
                "pin a version")
        return None


def _index(table: dict[tuple[str, str], dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for (url, version) in table:
        out.setdefault(url, []).append(version)
    for versions in out.values():
        versions.sort()
    return out


def _pin(index: dict[str, list[str]], url: str, version: str | None,
         ambiguous_code: str, unknown_code: str, kind: str) -> str:
    if version is not None:
        if version not in index.get(url, []):
            raise ModelError(unknown_code,
                             f"{kind} {url}|{version} is not registered")
        return version
    versions = index.get(url, [])
    if not versions:
        raise ModelError(unknown_code, f"{kind} {url} is not registered")
    if len(versions) > 1:
        raise ModelError(ambiguous_code,
                         f"{kind} {url} is registered in versions {versions}; "
                         "pin a version")
    return versions[0]


def walk_codesystem(cs: dict) -> list[str]:
    """All concept codes of a local CodeSystem, depth-first, duplicates kept out."""

    codes: list[str] = []
    seen: set[str] = set()

    def walk(concepts: Any) -> None:
        for concept in concepts or []:
            if not isinstance(concept, dict):
                continue
            code = concept.get("code")
            if isinstance(code, str) and code not in seen:
                seen.add(code)
                codes.append(code)
            walk(concept.get("concept"))

    walk(cs.get("concept"))
    return codes


# ---------------------------------------------------------------------------
# Instance-side helpers (shared by the evaluator and the independent checker
# specification in docs/PROFILE_AUDIT.md)
# ---------------------------------------------------------------------------

PRIMITIVE_STRING = {
    "base64Binary", "canonical", "code", "date", "dateTime", "id", "instant",
    "markdown", "oid", "string", "time", "uri", "url", "uuid", "xhtml",
}
PRIMITIVE_INT = {"integer", "integer64", "positiveInt", "unsignedInt"}
COMPLEX_KNOWN = {
    "Address", "Age", "Annotation", "Attachment", "BackboneElement",
    "CodeableConcept", "Coding", "ContactDetail", "ContactPoint",
    "Contributor", "Count", "DataRequirement", "Distance", "Dosage",
    "Duration", "Element", "Expression", "Extension", "HumanName",
    "Identifier", "Meta", "Money", "Narrative", "ParameterDefinition",
    "Period", "Quantity", "Range", "Ratio", "Reference", "RelatedArtifact",
    "Resource", "SampledData", "Signature", "Timing", "TriggerDefinition",
    "UsageContext",
}


def json_typename(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "null"


def single_type_matches(value: Any, code: str) -> bool:
    if code in PRIMITIVE_STRING:
        return isinstance(value, str)
    if code == "boolean":
        return isinstance(value, bool)
    if code in PRIMITIVE_INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if code == "decimal":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if code in COMPLEX_KNOWN:
        return isinstance(value, dict)
    raise UnknownTypeCode(code)


def deep_equal(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def pattern_match(pattern: Any, value: Any) -> bool:
    """FHIR pattern semantics: the instance must contain the pattern.

    Objects: every pattern key must match recursively. Arrays: every pattern
    item must have at least one matching instance item. Scalars: equality.
    """

    if isinstance(pattern, dict):
        if not isinstance(value, dict):
            return False
        return all(k in value and pattern_match(v, value[k])
                   for k, v in pattern.items())
    if isinstance(pattern, list):
        if not isinstance(value, list):
            return False
        return all(any(pattern_match(p, item) for item in value)
                   for p in pattern)
    return pattern == value


def resolve_path(resource: dict, path: str) -> list[tuple[str, Any, str | None]]:
    """Instance items at an element path; each item is (loc, value, choice_key)."""

    segments = path.split(".")[1:]  # drop the root resource type
    return resolve_segments(resource, segments, "$")


def resolve_segments(node: Any, segments: list[str],
                     loc: str) -> list[tuple[str, Any, str | None]]:
    current: list[tuple[str, Any, str | None]] = [(loc, node, None)]
    for seg in segments:
        nxt: list[tuple[str, Any, str | None]] = []
        for loc0, node0, _ in current:
            if not isinstance(node0, dict):
                continue
            if seg.endswith("[x]"):
                base = seg[:-3]
                for key, value in node0.items():
                    if key.startswith(base) and len(key) > len(base) \
                            and key[len(base)].isupper():
                        nxt.append((f"{loc0}.{key}", value, key))
            elif seg in node0:
                value = node0[seg]
                if isinstance(value, list):
                    for idx, item in enumerate(value):
                        nxt.append((f"{loc0}.{seg}[{idx}]", item, None))
                else:
                    nxt.append((f"{loc0}.{seg}", value, None))
        current = nxt
    return current


def choice_concrete_type(choice_key: str, base: str) -> str:
    """``valueQuantity`` -> ``Quantity`` (the FHIR type name of the choice)."""

    return choice_key[len(base):]


def coded_pairs(value: Any) -> set[tuple[str | None, str]]:
    """(system, code) pairs carried by a CodeableConcept/Coding/code value."""

    if isinstance(value, str):
        return {(None, value)}
    if isinstance(value, dict):
        coding = value.get("coding")
        if isinstance(coding, list):
            return {
                (c.get("system") if isinstance(c.get("system"), str) else None,
                 c["code"])
                for c in coding
                if isinstance(c, dict) and isinstance(c.get("code"), str)
            }
        if isinstance(value.get("code"), str):
            system = value.get("system")
            return {(system if isinstance(system, str) else None, value["code"])}
    return set()


def pair_in_expansion(pair: tuple[str | None, str],
                      expansion: set[tuple[str | None, str]]) -> bool:
    system, code = pair
    for esys, ecode in expansion:
        if ecode != code:
            continue
        if system is None or esys is None or esys == system:
            return True
    return False
