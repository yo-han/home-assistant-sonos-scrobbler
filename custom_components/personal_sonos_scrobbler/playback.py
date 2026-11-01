"""Immutable values the pure engine hands to later delivery stages (stdlib only)."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """Original provider metadata, kept verbatim and out of every repr."""

    artist: str = field(repr=False)
    title: str = field(repr=False)
    album: str | None = field(repr=False, default=None)


@dataclass(frozen=True, slots=True)
class CurrentPlayback:
    """What one group plays right now, proven by its latest state; RAM only.

    An unsettled entry keeps the occurrence known while its group regroups or its
    metadata is missing, so the same play is not taken for a new one afterwards. It
    may not be announced.
    """

    occurrence_id: str
    metadata: TrackMetadata = field(repr=False)
    started_at: datetime = field(repr=False)
    duration_seconds: float | None = field(repr=False, default=None)
    settled: bool = True
    # The entity whose state proved this: the group's coordinator, selected or not.
    speaker: str | None = field(repr=False, default=None)


@dataclass(frozen=True, slots=True)
class QualifiedPlay:
    """One occurrence that reached its listening threshold, emitted exactly once."""

    occurrence_id: str
    track_key: bytes = field(repr=False)
    metadata: TrackMetadata = field(repr=False)
    started_at: datetime = field(repr=False)
    qualified_at: datetime = field(repr=False)
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ResumeHint:
    """One marker slot of a selected speaker, by its stable registry id.

    A speaker holds its active marker and at most one prior marker: the identity
    it left behind when it joined a stream on a different occurrence.
    """

    occurrence_id: str
    track_key: bytes = field(repr=False)
    started_at: datetime = field(repr=False)
    lineage: str = field(repr=False)
    prior: bool = False


@dataclass(frozen=True, slots=True)
class MarkerDelta:
    """What changed in the durable marker state since the last report.

    lineages_cleared wipes all slots of a lineage; priors_cleared only its prior.
    """

    markers_set: tuple[ResumeHint, ...] = ()
    lineages_cleared: tuple[str, ...] = field(repr=False, default=())
    priors_cleared: tuple[str, ...] = field(repr=False, default=())
    guards_set: tuple[str, ...] = field(repr=False, default=())
    guards_cleared: tuple[str, ...] = field(repr=False, default=())

    def __bool__(self) -> bool:
        """Tell whether anything changed."""
        return any(
            (
                self.markers_set,
                self.lineages_cleared,
                self.priors_cleared,
                self.guards_set,
                self.guards_cleared,
            )
        )


@dataclass(frozen=True, slots=True)
class ActiveOccurrence:
    """The occurrence the engine is following right now, without metadata."""

    occurrence_id: str
    track_key: bytes = field(repr=False)
    started_at: datetime | None = field(repr=False)
    qualified: bool
