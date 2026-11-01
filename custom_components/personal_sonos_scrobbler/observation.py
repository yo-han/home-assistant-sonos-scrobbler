"""Estimate listened time from reported playback, on an injected monotonic clock."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .const import ESTIMATE_BASIS
from .normalize import (
    MAX_MEDIA_SECONDS,
    MetadataPresence,
    PlaybackSample,
    PlaybackStatus,
    SourceCategory,
)
from .playback import ActiveOccurrence, QualifiedPlay, TrackMetadata

HEARTBEAT_SECONDS = 15.0
MAX_ACCRUAL_GAP_SECONDS = 45.0
MIN_DURATION_SECONDS = 30.0
MAX_THRESHOLD_SECONDS = 240.0
SEEK_TOLERANCE_SECONDS = 3.0
RESTART_POSITION_MAX_SECONDS = 5.0
RESTART_MIN_BACKWARD_SECONDS = 10.0
MAX_COUNTER = 1_000_000

_SUSPENDING_STATUSES = frozenset(
    {
        PlaybackStatus.UNAVAILABLE,
        PlaybackStatus.MISSING,
        PlaybackStatus.IDLE,
        PlaybackStatus.TOPOLOGY_UNCERTAIN,
    }
)


class Stage(StrEnum):
    """Where the observation of the current play stands."""

    WAITING = "waiting"
    OBSERVING = "observing"
    SUSPENDED = "suspended"
    QUALIFIED = "qualified"
    RESTART_AMBIGUOUS = "restart_ambiguous"
    EXCLUDED = "excluded"


class Reason(StrEnum):
    """Why the observation is in its current stage."""

    NO_MEDIA = "no_media"
    COUNTING = "counting"
    NOT_PLAYING = "not_playing"
    UNAVAILABLE = "unavailable"
    TOPOLOGY_UNCERTAIN = "topology_uncertain"
    ENTITY_MISSING = "entity_missing"
    METADATA_MISSING = "metadata_missing"
    SOURCE_UNKNOWN = "source_unknown"
    SOURCE_EXCLUDED = "source_excluded"
    DURATION_UNKNOWN = "duration_unknown"
    DURATION_TOO_SHORT = "duration_too_short"
    CLOCK_GAP = "clock_gap"
    THRESHOLD_REACHED = "threshold_reached"


@dataclass(frozen=True, slots=True)
class ObservationSnapshot:
    """Everything the engine exposes: enums, flags and bounded numbers only."""

    stage: Stage
    reason: Reason
    source_category: SourceCategory
    presence: MetadataPresence
    observed_seconds: float
    duration_seconds: float | None
    threshold_seconds: float | None
    candidate_count: int
    unknown_source_threshold_count: int
    ambiguous_restart_count: int
    seek_count: int
    clock_gap_count: int


@dataclass(slots=True)
class _Play:
    occurrence_id: str
    track_key: bytes = field(repr=False)
    category: SourceCategory
    duration: float | None
    queue_position: int | None
    metadata: TrackMetadata | None = field(repr=False)
    started_at: datetime | None = field(repr=False)
    suppressed: bool = False
    observed: float = 0.0
    qualified: bool = False
    unknown_threshold_reached: bool = False
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class _PositionAnchor:
    seconds: float
    observed_at: float
    advancing: bool


def status_attributes(
    snapshot: ObservationSnapshot,
    *,
    mode: str,
    selected_speakers: int,
    active_groups: int,
) -> dict[str, str | int | float | bool | None]:
    """Return the allowlist shared by the sensor and diagnostics.

    The snapshot covers every selected group at once: the stage is the leading
    group's, the counters are summed over all of them.
    """
    presence = snapshot.presence
    return {
        "mode": mode,
        "selected_speakers": selected_speakers,
        "active_groups": active_groups,
        "aggregated_across_groups": active_groups > 1,
        "estimate_basis": ESTIMATE_BASIS,
        "reason": snapshot.reason.value,
        "source_category": snapshot.source_category.value,
        "has_artist": presence.artist,
        "has_title": presence.title,
        "has_album": presence.album,
        "has_duration": presence.duration,
        "has_position": presence.position,
        "has_content_id": presence.content_id,
        "has_queue_position": presence.queue_position,
        "has_group_members": presence.group_members,
        "observed_seconds": round(snapshot.observed_seconds),
        "duration_seconds": snapshot.duration_seconds,
        "threshold_seconds": snapshot.threshold_seconds,
        "candidate_count": snapshot.candidate_count,
        "unknown_source_threshold_count": snapshot.unknown_source_threshold_count,
        "ambiguous_restart_count": snapshot.ambiguous_restart_count,
        "seek_count": snapshot.seek_count,
        "clock_gap_count": snapshot.clock_gap_count,
    }


def threshold_seconds(duration: float | None) -> float | None:
    """Return the listening time a track needs, or None if it can never qualify."""
    if duration is None or duration <= MIN_DURATION_SECONDS:
        return None
    return min(duration / 2, MAX_THRESHOLD_SECONDS)


def new_occurrence_id() -> str:
    """Return an identity that no other occurrence shares."""
    return uuid.uuid4().hex


def _increment(counter: int) -> int:
    return min(counter + 1, MAX_COUNTER)


class ObservationEngine:
    """Track one play at a time and count threshold crossings."""

    def __init__(
        self, new_occurrence_id: Callable[[], str] = new_occurrence_id
    ) -> None:
        """Start without a play; time only counts from the first sample."""
        self._new_occurrence_id = new_occurrence_id
        self._suppressed_key: bytes | None = None
        self._resume: ActiveOccurrence | None = None
        self._utc_anchor: tuple[datetime, float] | None = None
        self._play: _Play | None = None
        self._sample: PlaybackSample | None = None
        self._anchor: _PositionAnchor | None = None
        self._last_call: float | None = None
        self._clock_gap = False
        self._interrupted = False
        self._metadata_gap_since: float | None = None
        self._candidate_count = 0
        self._unknown_source_threshold_count = 0
        self._ambiguous_restart_count = 0
        self._seek_count = 0
        self._clock_gap_count = 0

    def observe(self, sample: PlaybackSample, now: float) -> tuple[QualifiedPlay, ...]:
        """Account for the time up to now, then apply a new state."""
        events: list[QualifiedPlay] = []
        self._accrue(now, events)
        self._apply(sample, now)
        self._sample = sample
        if sample.sampled_at is not None:
            self._utc_anchor = (sample.sampled_at, now)
        self._evaluate_threshold(now, events)
        return tuple(events)

    def tick(self, now: float) -> tuple[QualifiedPlay, ...]:
        """Account for the time up to now without a new state."""
        events: list[QualifiedPlay] = []
        self._accrue(now, events)
        return tuple(events)

    def end_play(self, now: float) -> tuple[QualifiedPlay, ...]:
        """Account for the time up to now and drop a play that cannot continue."""
        events: list[QualifiedPlay] = []
        self._accrue(now, events)
        self._play = None
        self._anchor = None
        self._interrupted = False
        self._metadata_gap_since = None
        return tuple(events)

    def suppress_until_transition(self, track_key: bytes) -> None:
        """Never emit this track until another track or a proven restart starts."""
        self._suppressed_key = track_key
        if self._play is not None and self._play.track_key == track_key:
            self._play.suppressed = True

    def resume_occurrence(
        self, occurrence: ActiveOccurrence, *, suppressed: bool
    ) -> None:
        """Let the next start of this track continue the given identity.

        Credit restarts from zero. A suppressed resume keeps the identity too, so
        its start is never re-anchored from a position read much later.
        """
        self._resume = occurrence
        if suppressed:
            self.suppress_until_transition(occurrence.track_key)

    @property
    def active_occurrence(self) -> ActiveOccurrence | None:
        """Return the identity of the current play, without its metadata."""
        play = self._play
        if play is None:
            return None
        return ActiveOccurrence(
            occurrence_id=play.occurrence_id,
            track_key=play.track_key,
            started_at=play.started_at,
            qualified=play.qualified,
        )

    def seconds_until_next_tick(self) -> float | None:
        """Return the delay for the next timer, or None while nothing counts."""
        play = self._play
        if play is None or not self._is_counting():
            return None
        threshold = threshold_seconds(play.duration)
        if (
            threshold is None
            or play.qualified
            or play.ambiguous
            or play.unknown_threshold_reached
        ):
            return HEARTBEAT_SECONDS
        return min(HEARTBEAT_SECONDS, max(threshold - play.observed, 0.0))

    @property
    def snapshot(self) -> ObservationSnapshot:
        """Return the current state for sensors and diagnostics."""
        play = self._play
        sample = self._sample
        stage, reason = self._stage_and_reason()
        return ObservationSnapshot(
            stage=stage,
            reason=reason,
            source_category=sample.category if sample else SourceCategory.UNKNOWN,
            presence=sample.presence if sample else MetadataPresence(),
            observed_seconds=play.observed if play else 0.0,
            duration_seconds=play.duration if play else None,
            threshold_seconds=threshold_seconds(play.duration) if play else None,
            candidate_count=self._candidate_count,
            unknown_source_threshold_count=self._unknown_source_threshold_count,
            ambiguous_restart_count=self._ambiguous_restart_count,
            seek_count=self._seek_count,
            clock_gap_count=self._clock_gap_count,
        )

    def _is_counting(self) -> bool:
        play = self._play
        sample = self._sample
        return (
            play is not None
            and sample is not None
            and sample.status is PlaybackStatus.PLAYING
            and sample.track_key == play.track_key
        )

    def _accrue(self, now: float, events: list[QualifiedPlay]) -> None:
        elapsed = None if self._last_call is None else now - self._last_call
        self._last_call = now
        play = self._play
        if elapsed is None or play is None or not self._is_counting():
            return
        if 0 <= elapsed <= MAX_ACCRUAL_GAP_SECONDS:
            play.observed = min(play.observed + elapsed, MAX_MEDIA_SECONDS)
            self._clock_gap = False
            self._evaluate_threshold(now, events)
        else:
            self._clock_gap = True
            self._clock_gap_count = _increment(self._clock_gap_count)
            self._mark_ambiguous(play)

    def _apply(self, sample: PlaybackSample, now: float) -> None:
        if sample.category is SourceCategory.EXCLUDED and (
            sample.status not in _SUSPENDING_STATUSES
        ):
            self._play = None
            self._anchor = None
            self._interrupted = False
            self._metadata_gap_since = None
            return
        if sample.status in _SUSPENDING_STATUSES:
            self._interrupted = self._play is not None
            return
        if sample.track_key is None:
            if self._play is not None and self._metadata_gap_since is None:
                self._metadata_gap_since = now
            return
        play = self._play
        if play is None or sample.track_key != play.track_key:
            self._start_play(sample, sample.track_key, now)
            return
        if self._metadata_gap_since is not None:
            gap = now - self._metadata_gap_since
            if not 0 <= gap <= MAX_ACCRUAL_GAP_SECONDS:
                self._mark_ambiguous(play)
            self._metadata_gap_since = None
        if self._position_jump_starts_new_play(sample, play, now):
            # A proven restart is a distinguishable transition: it lifts suppression.
            self._start_play(sample, sample.track_key, now, proven_restart=True)
            return
        if self._interrupted and not self._position_is_continuous(sample, play, now):
            self._mark_ambiguous(play)
        self._interrupted = False
        play.category = sample.category
        if sample.duration_seconds is not None:
            play.duration = sample.duration_seconds
        if sample.queue_position is not None:
            play.queue_position = sample.queue_position
        self._complete_album(play, sample)
        self._move_anchor(sample, now)

    def _start_play(
        self,
        sample: PlaybackSample,
        key: bytes,
        now: float,
        *,
        proven_restart: bool = False,
    ) -> None:
        if proven_restart or key != self._suppressed_key:
            self._suppressed_key = None
        resume = (
            self._resume if self._resume and self._resume.track_key == key else None
        )
        self._resume = None
        self._play = _Play(
            occurrence_id=resume.occurrence_id if resume else self._new_occurrence_id(),
            track_key=key,
            category=sample.category,
            duration=sample.duration_seconds,
            queue_position=sample.queue_position,
            metadata=sample.metadata,
            started_at=resume.started_at if resume else self._estimate_start(sample),
            suppressed=key == self._suppressed_key,
        )
        self._clock_gap = False
        self._interrupted = False
        self._metadata_gap_since = None
        self._anchor = None
        self._move_anchor(sample, now)

    @staticmethod
    def _estimate_start(sample: PlaybackSample) -> datetime | None:
        if sample.sampled_at is None:
            return None
        return sample.sampled_at - timedelta(seconds=sample.position_seconds or 0.0)

    def _utc_at(self, now: float, excess: float) -> datetime | None:
        """Map a monotonic reading to UTC through the latest sample's pairing."""
        if self._utc_anchor is None:
            return None
        sampled_at, sampled_now = self._utc_anchor
        return sampled_at + timedelta(seconds=now - sampled_now - excess)

    def _move_anchor(self, sample: PlaybackSample, now: float) -> None:
        if sample.position_seconds is not None:
            self._anchor = _PositionAnchor(
                seconds=sample.position_seconds,
                observed_at=now,
                advancing=sample.status is PlaybackStatus.PLAYING,
            )

    def _position_jump_starts_new_play(
        self, sample: PlaybackSample, play: _Play, now: float
    ) -> bool:
        position = sample.position_seconds
        expected = self._expected_position(play, now)
        if position is None or expected is None:
            return False
        if self._position_is_continuous(sample, play, now):
            return False
        self._seek_count = _increment(self._seek_count)
        if (
            position > RESTART_POSITION_MAX_SECONDS
            or expected - position < RESTART_MIN_BACKWARD_SECONDS
        ):
            return False
        if self._queue_moved(sample, play):
            return True
        self._mark_ambiguous(play)
        return False

    def _position_is_continuous(
        self, sample: PlaybackSample, play: _Play, now: float
    ) -> bool:
        position = sample.position_seconds
        expected = self._expected_position(play, now)
        return (
            position is not None
            and expected is not None
            and abs(position - expected) <= SEEK_TOLERANCE_SECONDS
        )

    @staticmethod
    def _complete_album(play: _Play, sample: PlaybackSample) -> None:
        """Add a late album once; nothing else changes and nothing after qualifying."""
        known, late = play.metadata, sample.metadata
        if (
            play.qualified
            or known is None
            or known.album is not None
            or late is None
            or late.album is None
        ):
            return
        play.metadata = replace(known, album=late.album)

    def _mark_ambiguous(self, play: _Play) -> None:
        """Keep a possibly new occurrence from adding to the credit of this play."""
        if not play.ambiguous:
            play.ambiguous = True
            self._ambiguous_restart_count = _increment(self._ambiguous_restart_count)

    def _expected_position(self, play: _Play, now: float) -> float | None:
        anchor = self._anchor
        if anchor is None:
            return None
        expected = anchor.seconds
        if anchor.advancing:
            expected += max(now - anchor.observed_at, 0.0)
        return expected if play.duration is None else min(expected, play.duration)

    @staticmethod
    def _queue_moved(sample: PlaybackSample, play: _Play) -> bool:
        return (
            sample.queue_position is not None
            and play.queue_position is not None
            and sample.queue_position != play.queue_position
        )

    def _evaluate_threshold(self, now: float, events: list[QualifiedPlay]) -> None:
        play = self._play
        # Time after an ambiguous restart may belong to a second play, so two
        # short plays must not add up to a candidate.
        if play is None or play.qualified or play.ambiguous:
            return
        threshold = threshold_seconds(play.duration)
        if threshold is None or play.observed < threshold:
            return
        if play.category is SourceCategory.SPOTIFY_CONFIRMED:
            play.qualified = True
            self._candidate_count = _increment(self._candidate_count)
            qualified_at = self._utc_at(now, play.observed - threshold)
            if (
                not play.suppressed
                and play.metadata is not None
                and play.started_at is not None
                and qualified_at is not None
                and play.duration is not None
            ):
                events.append(
                    QualifiedPlay(
                        occurrence_id=play.occurrence_id,
                        track_key=play.track_key,
                        metadata=play.metadata,
                        started_at=play.started_at,
                        qualified_at=qualified_at,
                        duration_seconds=play.duration,
                    )
                )
        elif not play.unknown_threshold_reached:
            play.unknown_threshold_reached = True
            self._unknown_source_threshold_count = _increment(
                self._unknown_source_threshold_count
            )

    def _stage_and_reason(self) -> tuple[Stage, Reason]:
        sample = self._sample
        if sample is None:
            return Stage.WAITING, Reason.NO_MEDIA
        suspension = self._suspension_reason(sample)
        play = self._play
        if play is None:
            if suspension is not None:
                return Stage.WAITING, suspension
            return Stage.EXCLUDED, Reason.SOURCE_EXCLUDED
        if suspension is not None:
            return Stage.SUSPENDED, suspension
        reason = self._counting_reason(play)
        if play.ambiguous:
            return Stage.RESTART_AMBIGUOUS, reason
        return (Stage.QUALIFIED if play.qualified else Stage.OBSERVING), reason

    def _suspension_reason(self, sample: PlaybackSample) -> Reason | None:
        if sample.status is PlaybackStatus.MISSING:
            return Reason.ENTITY_MISSING
        if sample.status is PlaybackStatus.UNAVAILABLE:
            return Reason.UNAVAILABLE
        if sample.status is PlaybackStatus.TOPOLOGY_UNCERTAIN:
            return Reason.TOPOLOGY_UNCERTAIN
        if sample.status is PlaybackStatus.IDLE:
            return Reason.NOT_PLAYING if self._play else Reason.NO_MEDIA
        if sample.category is SourceCategory.EXCLUDED:
            return None
        if sample.track_key is None:
            return Reason.METADATA_MISSING
        if sample.status is PlaybackStatus.PAUSED:
            return Reason.NOT_PLAYING
        return None

    def _counting_reason(self, play: _Play) -> Reason:
        if play.qualified:
            return Reason.THRESHOLD_REACHED
        if self._clock_gap:
            return Reason.CLOCK_GAP
        if play.duration is None:
            return Reason.DURATION_UNKNOWN
        if play.duration <= MIN_DURATION_SECONDS:
            return Reason.DURATION_TOO_SHORT
        if play.category is SourceCategory.UNKNOWN:
            return Reason.SOURCE_UNKNOWN
        return Reason.COUNTING
