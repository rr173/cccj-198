#!/usr/bin/env python3 -I
"""Generate synthetic profile packages and history fixtures for audit drills.

Everything is invented data, created at check time under ``build/ig-fixtures``
so the repository itself stays small. Two kinds of fixtures are produced:

* ``packages/<name>-<version>/`` — local implementation-guide profile
  packages (package.json + StructureDefinition/ValueSet/CodeSystem JSON),
  including deliberately broken packages for the negative registration
  drills;
* ``history/`` — a handoff package (manifest + baseline + two transactions)
  whose resources exercise every supported rule and every visible outcome.

Layout::

    build/ig-fixtures/
        packages/<pkg dir>/package.json, *.json
        history/manifest.json, baseline/*.ndjson, shards/*.ndjson
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fhist.jsonl import write_jsonl  # noqa: E402

SERVER_IG = "https://fhir.example.org/ig"
TENANT_IG = "tenant-ig"

CANON = "http://synth.example.org"
SD_PATIENT_BASE = f"{CANON}/StructureDefinition/synth-patient-base"
SD_PATIENT = f"{CANON}/StructureDefinition/synth-patient"
SD_OBS = f"{CANON}/StructureDefinition/synth-observation"
SD_OBS_STRICT = f"{CANON}/StructureDefinition/synth-observation-strict"
SD_OBS_TYPED = f"{CANON}/StructureDefinition/synth-observation-typed"
SD_DUP = f"{CANON}/StructureDefinition/dup-sd"
VS_GENDER = f"{CANON}/ValueSet/gender-vs"
VS_GENDER_BASE = f"{CANON}/ValueSet/gender-base-vs"
VS_CATEGORY = f"{CANON}/ValueSet/category-vs"
CS_GENDER = f"{CANON}/CodeSystem/gender-cs"
CS_COMP = f"{CANON}/CodeSystem/obs-comp-cs"
MRN_SYSTEM = f"{CANON}/mrn"


def write_json(dest: Path, doc: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n",
                    encoding="utf-8")


def package_json(name: str, version: str,
                 dependencies: dict | None = None) -> dict:
    return {
        "name": name,
        "version": version,
        "fhirVersions": ["4.0.1"],
        "dependencies": dependencies or {},
    }


def code_system(url: str, version: str, codes: list[str]) -> dict:
    return {
        "resourceType": "CodeSystem",
        "url": url,
        "version": version,
        "name": url.rsplit("/", 1)[-1].replace("-", "_"),
        "status": "active",
        "content": "complete",
        "concept": [{"code": c} for c in codes],
    }


def sd(url: str, version: str, name: str, sd_type: str, base: str,
       elements: list[dict], via: str = "differential") -> dict:
    return {
        "resourceType": "StructureDefinition",
        "url": url,
        "version": version,
        "name": name,
        "status": "active",
        "kind": "resource",
        "abstract": False,
        "type": sd_type,
        "baseDefinition": base,
        "derivation": "constraint",
        via: {"element": elements},
    }


# ---------------------------------------------------------------------------
# Profile packages
# ---------------------------------------------------------------------------

def build_core(root: Path) -> None:
    d = root / "synth-core-1.0.0"
    write_json(d / "package.json", package_json("synth-core", "1.0.0"))
    write_json(d / "CodeSystem-gender-cs.json",
               code_system(CS_GENDER, "1.0.0", ["male", "female", "unknown"]))
    write_json(d / "CodeSystem-obs-comp-cs.json",
               code_system(CS_COMP, "1.0.0", ["sys", "dia"]))
    # Base profile: external (FHIR core) baseDefinition is terminal offline;
    # the constraints stated here are what the child inherits.
    write_json(d / "StructureDefinition-synth-patient-base.json", sd(
        SD_PATIENT_BASE, "1.0.0", "SynthPatientBase", "Patient",
        "http://hl7.org/fhir/StructureDefinition/Patient",
        [
            {"id": "Patient", "path": "Patient"},
            {"id": "Patient.name", "path": "Patient.name", "min": 1,
             "max": "*"},
            {"id": "Patient.active", "path": "Patient.active",
             "fixedBoolean": True},
        ]))


def _patient_elements(version: str) -> list[dict]:
    return [
        {"id": "Patient", "path": "Patient"},
        # Child tightens the inherited name (adds max); min stays inherited.
        {"id": "Patient.name", "path": "Patient.name", "max": "3"},
        {"id": "Patient.gender", "path": "Patient.gender", "min": 1,
         "max": "1",
         "binding": {"strength": "required", "valueSet": VS_GENDER}},
        {"id": "Patient.identifier", "path": "Patient.identifier",
         "slicing": {"discriminator": [{"type": "value", "path": "system"}],
                     "rules": "open"}},
        {"id": "Patient.identifier:mrn", "path": "Patient.identifier",
         "sliceName": "mrn", "min": 1, "max": "1"},
        {"id": "Patient.identifier:mrn.system",
         "path": "Patient.identifier.system", "min": 1,
         "fixedUri": MRN_SYSTEM},
    ]


def _observation_elements() -> list[dict]:
    return [
        {"id": "Observation", "path": "Observation"},
        {"id": "Observation.status", "path": "Observation.status",
         "min": 1, "max": "1", "fixedCode": "final"},
        {"id": "Observation.code", "path": "Observation.code", "min": 1,
         "patternCodeableConcept": {
             "coding": [{"system": "http://loinc.org"}]}},
        {"id": "Observation.value[x]", "path": "Observation.value[x]",
         "min": 0, "max": "1", "type": [{"code": "Quantity"}]},
        {"id": "Observation.component", "path": "Observation.component",
         "slicing": {"discriminator": [{"type": "value", "path": "code"}],
                     "rules": "closed"}},
        {"id": "Observation.component:sys", "path": "Observation.component",
         "sliceName": "sys", "min": 1, "max": "1"},
        {"id": "Observation.component:sys.code",
         "path": "Observation.component.code",
         "patternCodeableConcept": {"coding": [
             {"system": CS_COMP, "code": "sys"}]}},
        {"id": "Observation.component:sys.value[x]",
         "path": "Observation.component.value[x]", "min": 1, "max": "1",
         "type": [{"code": "Quantity"}]},
        {"id": "Observation.component:dia", "path": "Observation.component",
         "sliceName": "dia", "min": 0, "max": "1"},
        {"id": "Observation.component:dia.code",
         "path": "Observation.component.code",
         "patternCodeableConcept": {"coding": [
             {"system": CS_COMP, "code": "dia"}]}},
    ]


def _typed_elements() -> list[dict]:
    """Closed ``type`` discriminator slicing on a choice element."""

    return [
        {"id": "Observation", "path": "Observation"},
        {"id": "Observation.status", "path": "Observation.status",
         "fixedCode": "final"},
        {"id": "Observation.value[x]", "path": "Observation.value[x]",
         "slicing": {"discriminator": [{"type": "type", "path": "$this"}],
                     "rules": "closed"}},
        {"id": "Observation.value[x]:valueQuantity",
         "path": "Observation.value[x]", "sliceName": "valueQuantity",
         "min": 0, "max": "1", "type": [{"code": "Quantity"}]},
        {"id": "Observation.value[x]:valueString",
         "path": "Observation.value[x]", "sliceName": "valueString",
         "min": 0, "max": "1", "type": [{"code": "string"}]},
    ]


def _strict_elements() -> list[dict]:
    return [
        {"id": "Observation", "path": "Observation"},
        {"id": "Observation.status", "path": "Observation.status",
         "fixedCode": "final"},
        {"id": "Observation.subject", "path": "Observation.subject",
         "type": [{"code": "Reference",
                   "targetProfile": [SD_PATIENT]}]},
        {"id": "Observation.effective[x]", "path": "Observation.effective[x]",
         "constraint": [{"key": "synth-1", "severity": "error",
                         "human": "must be in the past",
                         "expression": "effectiveDateTime <= now()"}]},
        {"id": "Observation.category", "path": "Observation.category",
         "min": 1,
         "binding": {"strength": "required", "valueSet": VS_CATEGORY}},
    ]


def _gender_vs(version: str, with_exclude: bool,
               nested: bool = False) -> dict:
    if nested:
        # Compose through another local ValueSet (nested include resolution).
        compose: dict = {"include": [{"valueSet": [VS_GENDER_BASE]}]}
    else:
        compose = {"include": [{"system": CS_GENDER}]}
        if with_exclude:
            compose["exclude"] = [{"system": CS_GENDER,
                                   "concept": [{"code": "unknown"}]}]
    return {
        "resourceType": "ValueSet",
        "url": VS_GENDER,
        "version": version,
        "name": "SynthGenderVS",
        "status": "active",
        "compose": compose,
    }


def _category_vs(version: str) -> dict:
    return {
        "resourceType": "ValueSet",
        "url": VS_CATEGORY,
        "version": version,
        "name": "SynthCategoryVS",
        "status": "active",
        "compose": {"include": [{
            "system": "http://terminology.hl7.org/CodeSystem/"
                      "observation-category",
            "filter": [{"property": "concept", "op": "is-a",
                        "value": "vital-signs"}],
        }]},
    }


def build_ig(root: Path, version: str, with_exclude: bool) -> None:
    d = root / f"synth-ig-{version}"
    write_json(d / "package.json",
               package_json("synth-ig", version, {"synth-core": "1.0.0"}))
    nested = not with_exclude  # the 2.0.0 package composes via a nested VS
    write_json(d / "ValueSet-gender-vs.json",
               _gender_vs(version, with_exclude, nested=nested))
    if nested:
        write_json(d / "ValueSet-gender-base-vs.json", {
            "resourceType": "ValueSet",
            "url": VS_GENDER_BASE,
            "version": version,
            "name": "SynthGenderBaseVS",
            "status": "active",
            "compose": {"include": [{"system": CS_GENDER}]},
        })
    write_json(d / "ValueSet-category-vs.json", _category_vs(version))
    write_json(d / "StructureDefinition-synth-patient.json", sd(
        SD_PATIENT, version, "SynthPatient", "Patient",
        f"{SD_PATIENT_BASE}|1.0.0", _patient_elements(version)))
    write_json(d / "StructureDefinition-synth-observation.json", sd(
        SD_OBS, version, "SynthObservation", "Observation",
        "http://hl7.org/fhir/StructureDefinition/Observation",
        _observation_elements()))
    write_json(d / "StructureDefinition-synth-observation-strict.json", sd(
        SD_OBS_STRICT, version, "SynthObservationStrict", "Observation",
        "http://hl7.org/fhir/StructureDefinition/Observation",
        _strict_elements()))
    write_json(d / "StructureDefinition-synth-observation-typed.json", sd(
        SD_OBS_TYPED, version, "SynthObservationTyped", "Observation",
        "http://hl7.org/fhir/StructureDefinition/Observation",
        _typed_elements(), via="snapshot"))


def build_dup_packages(root: Path) -> None:
    for dirname, pkg_name, sd_version in (
            ("synth-dup-a-1.0.0", "synth-dup-a", "1.0.0"),
            ("synth-dup-b-1.0.0", "synth-dup-b", "2.0.0")):
        d = root / dirname
        write_json(d / "package.json", package_json(pkg_name, "1.0.0"))
        write_json(d / "StructureDefinition-dup-sd.json", sd(
            SD_DUP, sd_version, "DupSd", "Patient",
            "http://hl7.org/fhir/StructureDefinition/Patient",
            [{"id": "Patient", "path": "Patient"},
             {"id": "Patient.name", "path": "Patient.name", "min": 1}]))
    d = root / "synth-ambig-1.0.0"
    write_json(d / "package.json", package_json(
        "synth-ambig", "1.0.0",
        {"synth-dup-a": "1.0.0", "synth-dup-b": "1.0.0"}))


def build_bad_packages(root: Path) -> None:
    d = root / "bad-missing-dep"
    write_json(d / "package.json", package_json(
        "synth-orphan", "1.0.0", {"no-such-pkg": "1.0.0"}))
    write_json(d / "CodeSystem-x.json",
               code_system(f"{CANON}/CodeSystem/x-cs", "1.0.0", ["a"]))

    d = root / "bad-self-dep"
    write_json(d / "package.json", package_json(
        "synth-loop", "1.0.0", {"synth-loop": "1.0.0"}))
    write_json(d / "CodeSystem-y.json",
               code_system(f"{CANON}/CodeSystem/y-cs", "1.0.0", ["b"]))

    # Same canonical|version as synth-core's gender-cs but different bytes.
    d = root / "bad-canonical-clash"
    write_json(d / "package.json", package_json("synth-clash", "1.0.0"))
    write_json(d / "CodeSystem-gender-cs.json",
               code_system(CS_GENDER, "1.0.0", ["male"]))

    # Same package name+version as synth-ig 1.0.0, different content.
    d = root / "synth-ig-1.0.0-alt"
    write_json(d / "package.json",
               package_json("synth-ig", "1.0.0", {"synth-core": "1.0.0"}))
    write_json(d / "ValueSet-gender-vs.json",
               _gender_vs("1.0.0", with_exclude=False))


# ---------------------------------------------------------------------------
# History fixtures
# ---------------------------------------------------------------------------

def patient(rid: str, vid: str, profiles: list[str] | None,
            gender: str | None = "male", active: bool = True,
            with_name: bool = True, mrn: bool = True) -> dict:
    meta: dict = {"versionId": vid, "lastUpdated": "2026-01-10T00:00:00Z"}
    if profiles:
        meta["profile"] = profiles
    res: dict = {
        "resourceType": "Patient",
        "id": rid,
        "meta": meta,
        "active": active,
        "identifier": [],
    }
    if with_name:
        res["name"] = [{"family": f"Synth-{rid}", "given": ["Test"]}]
    if gender is not None:
        res["gender"] = gender
    if mrn:
        res["identifier"].append({"system": MRN_SYSTEM,
                                  "value": f"MRN-{rid}"})
    else:
        res["identifier"].append(
            {"system": "http://other.example.org/x", "value": "X"})
    return res


def observation(rid: str, vid: str, profile: str, status: str = "final",
                code_system_url: str = "http://loinc.org",
                value_kind: str = "quantity",
                components: str = "both") -> dict:
    res: dict = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"versionId": vid, "lastUpdated": "2026-01-10T00:00:00Z",
                 "profile": [profile]},
        "status": status,
        "code": {"coding": [{"system": code_system_url, "code": "8480-6"}]},
    }
    if value_kind == "quantity":
        res["valueQuantity"] = {"value": 120, "unit": "mmHg"}
    else:
        res["valueString"] = "heavy"
    comp = []
    if components in ("both", "sys"):
        comp.append({"code": {"coding": [{"system": CS_COMP, "code": "sys"}]},
                     "valueQuantity": {"value": 120, "unit": "mmHg"}})
    if components in ("both", "dia"):
        comp.append({"code": {"coding": [{"system": CS_COMP, "code": "dia"}]},
                     "valueQuantity": {"value": 80, "unit": "mmHg"}})
    if components == "extra":
        comp.append({"code": {"coding": [{"system": CS_COMP, "code": "dia"}]},
                     "valueQuantity": {"value": 80, "unit": "mmHg"}})
        comp.append({"code": {"coding": [{"system": "http://x.example.org/c",
                                          "code": "zzz"}]},
                     "valueString": "?"})
    if comp:
        res["component"] = comp
    return res


def op_put(seq: int, ordinal: int, resource: dict) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "PUT",
            "resourceType": resource["resourceType"], "id": resource["id"],
            "versionId": resource["meta"]["versionId"], "resource": resource}


def op_delete(seq: int, ordinal: int, rtype: str, rid: str, vid: str) -> dict:
    return {"seq": seq, "ordinal": ordinal, "method": "DELETE",
            "resourceType": rtype, "id": rid, "versionId": vid}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_shard(history_dir: Path, rel: str, rows: list[dict]) -> dict:
    dest = history_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = write_jsonl(dest, iter(rows))
    return {"path": rel, "sha256": sha256_file(dest), "count": count}


def build_history(root: Path) -> None:
    d = root / "history"
    p_ok = patient("p-ok", "v1", [SD_PATIENT])
    p_ok["identifier"].append(
        {"system": "http://other.example.org/x", "value": "X1"})
    baseline = [
        op_put(30, 0, p_ok),
        op_put(30, 1, patient("p-bad", "v1", [SD_PATIENT], gender="other",
                              active=False, with_name=False, mrn=False)),
        op_put(30, 2, patient("p-excl", "v1", [SD_PATIENT],
                              gender="unknown")),
        op_put(30, 3, patient("p-noprof", "v1", None)),
        op_put(30, 4, patient("p-ambig", "v1", [SD_DUP])),
        op_put(30, 5, patient("p-unknown", "v1",
                              [f"{CANON}/StructureDefinition/ghost|9.9.9"])),
        op_put(30, 6, patient("p-del", "v1", [SD_PATIENT])),
        op_put(30, 7, observation("o-ok", "v1", SD_OBS)),
        op_put(30, 8, observation("o-bad", "v1", SD_OBS,
                                  status="preliminary",
                                  code_system_url="http://other.example.org/x",
                                  value_kind="string", components="extra")),
        op_put(30, 9, {
            "resourceType": "Observation", "id": "o-strict",
            "meta": {"versionId": "v1",
                     "lastUpdated": "2026-01-10T00:00:00Z",
                     "profile": [SD_OBS_STRICT]},
            "status": "final",
            "subject": {"reference": "Patient/p-ok"},
            "effectiveDateTime": "2026-01-10T00:00:00Z",
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/"
                          "observation-category",
                "code": "vital-signs"}]}],
        }),
        # Child profile caps name at 3 (overriding the inherited max):
        # four names violate the merged constraint.
        op_put(30, 10, patient("p-names", "v1", [SD_PATIENT],
                               with_name=False) | {
            "name": [{"family": "One"}, {"family": "Two"},
                     {"family": "Three"}, {"family": "Four"}],
        }),
        # Closed type-discriminator slicing on value[x].
        op_put(30, 11, {
            "resourceType": "Observation", "id": "o-typed-ok",
            "meta": {"versionId": "v1",
                     "lastUpdated": "2026-01-10T00:00:00Z",
                     "profile": [SD_OBS_TYPED]},
            "status": "final",
            "valueQuantity": {"value": 5.5, "unit": "cm"},
        }),
        op_put(30, 12, {
            "resourceType": "Observation", "id": "o-typed-bad",
            "meta": {"versionId": "v1",
                     "lastUpdated": "2026-01-10T00:00:00Z",
                     "profile": [SD_OBS_TYPED]},
            "status": "final",
            "valuePeriod": {"start": "2026-01-01"},
        }),
        # meta.profile names a Patient profile on an Observation.
        op_put(30, 13, {
            "resourceType": "Observation", "id": "o-wrongtype",
            "meta": {"versionId": "v1",
                     "lastUpdated": "2026-01-10T00:00:00Z",
                     "profile": [SD_PATIENT]},
            "status": "final",
        }),
    ]
    b = write_shard(d, "baseline/baseline.ndjson", baseline)

    p_ok_v2 = patient("p-ok", "v2", [SD_PATIENT], gender="other")
    t31 = write_shard(d, "shards/seq-31.ndjson", [op_put(31, 0, p_ok_v2)])
    t32 = write_shard(d, "shards/seq-32.ndjson",
                      [op_delete(32, 0, "Patient", "p-del", "v2")])

    manifest = {
        "format_version": 1,
        "source_server": SERVER_IG,
        "tenant": TENANT_IG,
        "aliases": [],
        "baseline": {"seq": 30, "path": b["path"], "sha256": b["sha256"],
                     "resource_count": b["count"]},
        "transactions": [
            {"seq": 31, "operation_count": t31["count"], "shards": [
                {"ordinal": 0, "path": t31["path"], "sha256": t31["sha256"],
                 "operation_count": t31["count"]}]},
            {"seq": 32, "operation_count": t32["count"], "shards": [
                {"ordinal": 0, "path": t32["path"], "sha256": t32["sha256"],
                 "operation_count": t32["count"]}]},
        ],
    }
    write_json(d / "manifest.json", manifest)


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/ig-fixtures")
    packages = root / "packages"
    build_core(packages)
    build_ig(packages, "1.0.0", with_exclude=True)
    build_ig(packages, "2.0.0", with_exclude=False)
    build_dup_packages(packages)
    build_bad_packages(packages)
    build_history(root)
    print(json.dumps({"status": "ok", "root": str(root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
