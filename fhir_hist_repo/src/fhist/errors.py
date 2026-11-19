"""Error taxonomy for the FHIR history store.

All user-facing failure conditions map to one of these exception types so the
CLI can render a stable ``status`` string instead of a Python traceback.
"""


class FHError(Exception):
    """Base class for all fhist errors."""

    status = "error"


class UsageError(FHError):
    """Invalid command-line arguments or configuration."""

    status = "usage_error"


class RepoNotFound(FHError):
    """The given path does not look like an initialised repository."""

    status = "repo_not_found"


class UnknownSource(FHError):
    """A repository command referenced a source that never registered."""

    status = "unknown_source"


class ManifestError(FHError):
    """The manifest document itself is malformed or internally inconsistent."""

    status = "bad_manifest"


class ShardError(FHError):
    """A shard file is missing, corrupt, has the wrong digest or wrong count."""

    status = "bad_shard"


class TransactionConflict(FHError):
    """An unpublished transaction conflicts with another staged transaction.

    The conflicting payload is quarantined and the source frontier is held.
    """

    status = "conflict"


class PayloadError(FHError):
    """Operation metadata does not match the resource body, or body invalid."""

    status = "bad_payload"


class CutNotQueryable(FHError):
    """The requested cut predates baseline or is beyond the published frontier."""

    status = "cut_not_queryable"


class RepoLocked(FHError):
    """Another writer holds the repository; the operation may be retried."""

    status = "locked"


class ProfilePackageError(FHError):
    """A profile package directory is malformed or fails validation."""

    status = "profile_package_error"


class ProfileConflict(FHError):
    """A package name+version or canonical+version clashes with other content.

    Never silently overwritten: the conflicting registration is rejected.
    """

    status = "profile_conflict"


class ProfileDependencyError(FHError):
    """Package dependencies are missing or form a cycle."""

    status = "profile_dependency_error"


class UnknownPackage(FHError):
    """An audit referenced a profile package that is not registered."""

    status = "unknown_package"


class UnknownAudit(FHError):
    """The requested audit id does not exist in this repository."""

    status = "unknown_audit"
