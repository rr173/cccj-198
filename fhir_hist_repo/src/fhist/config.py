"""Repository configuration: canonical base URLs and their explicit aliases.

Absolute ``Reference.reference`` values are only treated as same-source when
they match the configured canonical base URL of the source named in the
manifest or one of its explicitly declared equivalent aliases. Matching a
bare trailing id across hosts is never allowed.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urldefrag

from .errors import UsageError
from .paths import REPO_CONFIG


class RepoConfig:
    def __init__(self) -> None:
        self.version = 1
        # source_server -> {"aliases": set[str]}
        self.sources: dict[str, dict[str, set[str]]] = {}

    @classmethod
    def load(cls, root: Path) -> "RepoConfig":
        path = root / REPO_CONFIG
        if not path.is_file():
            raise UsageError(f"missing repository config {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = cls()
        cfg.version = raw.get("format_version", 1)
        for entry in raw.get("sources", []):
            bases = _normalise_base(entry["source_server"])
            aliases = {bases}
            for alias in entry.get("aliases", []):
                aliases.add(_normalise_base(alias))
            cfg.sources[bases] = {"aliases": aliases}
        return cfg

    @classmethod
    def initial(cls, extra_sources: list[dict[str, str]] | None = None) -> "RepoConfig":
        cfg = cls()
        for entry in extra_sources or []:
            cfg.register(entry["source_server"], entry.get("aliases", []))
        return cfg

    def register(self, source_server: str, aliases: list[str]) -> str:
        base = _normalise_base(source_server)
        existing = self.sources.setdefault(base, {"aliases": {base}})
        for alias in aliases:
            existing["aliases"].add(_normalise_base(alias))
        return base

    def save(self, root: Path) -> None:
        payload = {
            "format_version": self.version,
            "sources": [
                {"source_server": base, "aliases": sorted(v["aliases"] - {base})}
                for base, v in sorted(self.sources.items())
            ],
        }
        (root / REPO_CONFIG).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def known_bases(self) -> set[str]:
        out: set[str] = set()
        for v in self.sources.values():
            out |= v["aliases"]
        return out

    def match_source(self, base_url: str) -> str | None:
        """Return the canonical source_server whose alias set covers base_url."""

        normalised = _normalise_base(base_url)
        for canonical, v in self.sources.items():
            if normalised in v["aliases"]:
                return canonical
        return None


def _normalise_base(url: str) -> str:
    """Normalise an absolute base URL for comparison.

    The fragment is always dropped, a trailing slash is removed, and the
    scheme/host are lower-cased. Path casing is preserved.
    """

    if "://" not in url:
        raise UsageError(f"expected absolute http(s) URL, got: {url!r}")
    no_frag, _ = urldefrag(url)
    scheme, rest = no_frag.split("://", 1)
    if "/" in rest:
        host, path = rest.split("/", 1)
    else:
        host, path = rest, ""
    host = host.lower()
    path = path.rstrip("/")
    return f"{scheme.lower()}://{host}/{path}" if path else f"{scheme.lower()}://{host}"
