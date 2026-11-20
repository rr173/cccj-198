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


class BedCalendarError(FHError):
    """The bed-open calendar CSV is malformed or internally inconsistent."""

    status = "bad_calendar"


class CalendarBedMissing(FHError):
    """A bed used by the calendar is not a current Location at the cut."""

    status = "calendar_bed_missing"


class UnknownRelease(FHError):
    """A bedreport base-release reference matched no published release."""

    status = "unknown_release"


class ReleaseError(FHError):
    """A bedreport release directory is missing, corrupt or was tampered with."""

    status = "bad_release"


class SeriesMismatch(FHError):
    """A base release belongs to a different bedreport series."""

    status = "series_mismatch"


class DeliveryError(FHError):
    """A delivery file is missing, corrupt or fails manifest verification."""

    status = "bad_delivery"


class BaseMismatch(FHError):
    """The local partition is not the release manifest's previous batch."""

    status = "base_mismatch"


class LocalStateMismatch(FHError):
    """Recorded loader state disagrees with the actual local partition."""

    status = "local_state_mismatch"


class TargetMismatch(FHError):
    """The post-load partition does not match the manifest's target identity."""

    status = "target_mismatch"


class PartitionNotEmpty(FHError):
    """An initial (full) release was offered to a non-empty partition."""

    status = "partition_not_empty"


# ---------------------------------------------------------------------------
# Cross-source patient identity dimension (see docs/IDENTITY_DIMENSION.md)
# ---------------------------------------------------------------------------

class UnknownIdentityRelease(FHError):
    """The requested identity-dimension release number does not exist."""

    status = "unknown_release"


class UnknownDecision(FHError):
    """A decision id/revision is not the effective revision in the base."""

    status = "unknown_decision"


class IdentityReleaseRejected(FHError):
    """A release request is invalid; nothing of it becomes visible."""

    status = "release_rejected"


class IdentityDateOutOfRange(FHError):
    """A queried business date lies outside the release's fixed range."""

    status = "date_out_of_range"


class KeyNotParticipating(FHError):
    """The source key's source does not take part in this release."""

    status = "key_not_participating"


