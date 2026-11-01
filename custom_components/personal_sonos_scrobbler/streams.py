"""Keep one observation engine per canonical group of selected speakers."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta

from .normalize import PlaybackSample, PlaybackStatus, SourceCategory
from .observation import (
    MAX_COUNTER,
    RESTART_POSITION_MAX_SECONDS,
    SEEK_TOLERANCE_SECONDS,
    ObservationEngine,
    ObservationSnapshot,
    Stage,
    new_occurrence_id,
)
from .playback import (
    ActiveOccurrence,
    CurrentPlayback,
    MarkerDelta,
    QualifiedPlay,
    ResumeHint,
)
from .topology import CanonicalGroup, Topology

TOPOLOGY_SETTLE_SECONDS = 1.0
RESUME_WINDOW = timedelta(hours=6)
# Reported positions are coarse, so a routine reload can estimate just before the
# anchor. Reusing the marker's identity is the safe side: it cannot add a second play.
RESUME_TOLERANCE = timedelta(seconds=SEEK_TOLERANCE_SECONDS)

_STAGE_PRIORITY = (
    Stage.QUALIFIED,
    Stage.OBSERVING,
    Stage.RESTART_AMBIGUOUS,
    Stage.SUSPENDED,
    Stage.EXCLUDED,
    Stage.WAITING,
)
# Statuses that show a track, so a marker of it can be decided.
_RESUMABLE_STATUSES = frozenset({PlaybackStatus.PLAYING, PlaybackStatus.PAUSED})
# A lone lost speaker keeps reporting why; in a group the topology is what broke.
_OWN_STATUS_WINS = frozenset({PlaybackStatus.MISSING, PlaybackStatus.UNAVAILABLE})


@dataclass(frozen=True, slots=True)
class _Counters:
    candidate: int = 0
    unknown_source_threshold: int = 0
    ambiguous_restart: int = 0
    seek: int = 0
    clock_gap: int = 0

    def plus(self, snapshot: ObservationSnapshot) -> _Counters:
        return _Counters(
            candidate=_bounded(self.candidate + snapshot.candidate_count),
            unknown_source_threshold=_bounded(
                self.unknown_source_threshold + snapshot.unknown_source_threshold_count
            ),
            ambiguous_restart=_bounded(
                self.ambiguous_restart + snapshot.ambiguous_restart_count
            ),
            seek=_bounded(self.seek + snapshot.seek_count),
            clock_gap=_bounded(self.clock_gap + snapshot.clock_gap_count),
        )


@dataclass(slots=True)
class _Stream:
    key: str
    members: frozenset[str]
    engine: ObservationEngine
    certain: bool
    # The track the latest certain sample proved, which is all a marker may follow.
    showing: bytes | None = None
    # Whether that sample proves the track started anew.
    fresh_start: bool = False
    # An occurrence too doubtful to replace any marker, or to become one.
    unmarked: str | None = None
    # What this group plays right now, for whoever shows that; never for credit.
    current: CurrentPlayback | None = None


def _bounded(count: int) -> int:
    return min(count, MAX_COUNTER)


def _may_resume(marker: ResumeHint, sample: PlaybackSample) -> bool:
    position = sample.position_seconds
    sampled_at = sample.sampled_at
    if (
        position is None
        or sampled_at is None
        or position <= RESTART_POSITION_MAX_SECONDS
    ):
        return False
    estimated_start = sampled_at - timedelta(seconds=position)
    earliest = marker.started_at - RESUME_TOLERANCE
    return earliest <= estimated_start <= marker.started_at + RESUME_WINDOW


def _is_fresh_start(sample: PlaybackSample) -> bool:
    position = sample.position_seconds
    return position is not None and position <= RESTART_POSITION_MAX_SECONDS


def _shows_track(sample: PlaybackSample) -> bool:
    """Tell whether the sample proves which track is on, so markers can be judged."""
    return (
        sample.track_key is not None
        and sample.status in _RESUMABLE_STATUSES
        and sample.category is not SourceCategory.EXCLUDED
    )


def _held(current: CurrentPlayback | None) -> CurrentPlayback | None:
    return None if current is None else replace(current, settled=False)


def _handoffs(
    streams: Iterable[_Stream], groups: Iterable[CanonicalGroup]
) -> list[tuple[_Stream, CanonicalGroup]]:
    """Pair streams with successors: the group leader, old leader, then overlap."""
    ranked = sorted(
        (
            (
                stream.key != group.leader,
                stream.key not in group.members,
                -len(stream.members & group.members),
                group.leader,
                stream.key,
            ),
            stream,
            group,
        )
        for stream in streams
        for group in groups
        if stream.members & group.members
    )
    pairs: list[tuple[_Stream, CanonicalGroup]] = []
    taken_streams: set[str] = set()
    taken_groups: set[str] = set()
    for _rank, stream, group in ranked:
        if stream.key not in taken_streams and group.leader not in taken_groups:
            taken_streams.add(stream.key)
            taken_groups.add(group.leader)
            pairs.append((stream, group))
    return pairs


class StreamSet:
    """Credit each proven group once and hold back everything unproven."""

    def __init__(
        self, new_occurrence_id: Callable[[], str] = new_occurrence_id
    ) -> None:
        """Start without streams; apply creates them."""
        self._new_occurrence_id = new_occurrence_id
        self._streams: dict[str, _Stream] = {}
        self._retired = _Counters()
        self._lineages: dict[str, str] = {}
        self._markers: dict[str, ResumeHint] = {}
        self._priors: dict[str, ResumeHint] = {}
        # Lineages that joined another speaker's stream and may still hold their own.
        self._joined: set[str] = set()
        # Lineages that lost track of an identity: one slot could not hold them all.
        self._guards: set[str] = set()
        self._marker_changes: dict[tuple[str, bool], ResumeHint | None] = {}
        self._guard_changes: dict[str, bool] = {}

    @property
    def keys(self) -> frozenset[str]:
        """Return the entities whose state the current streams read."""
        return frozenset(self._streams)

    @property
    def active_occurrences(self) -> tuple[ActiveOccurrence, ...]:
        """Return the occurrence each stream follows, paused ones included."""
        streams = sorted(self._streams.values(), key=lambda stream: stream.key)
        active = (stream.engine.active_occurrence for stream in streams)
        return tuple(occurrence for occurrence in active if occurrence is not None)

    @property
    def current_playback(self) -> tuple[CurrentPlayback, ...]:
        """Return what each stream plays right now, one entry per group at most."""
        streams = sorted(self._streams.values(), key=lambda stream: stream.key)
        return tuple(stream.current for stream in streams if stream.current is not None)

    @property
    def snapshot(self) -> ObservationSnapshot:
        """Return the leading stream's state with the counters of every stream."""
        snapshots = {
            key: stream.engine.snapshot for key, stream in self._streams.items()
        }
        totals = self._retired
        for snapshot in snapshots.values():
            totals = totals.plus(snapshot)
        leading = min(
            snapshots.items(),
            key=lambda item: (_STAGE_PRIORITY.index(item[1].stage), item[0]),
            default=(None, ObservationEngine().snapshot),
        )[1]
        return replace(
            leading,
            candidate_count=totals.candidate,
            unknown_source_threshold_count=totals.unknown_source_threshold,
            ambiguous_restart_count=totals.ambiguous_restart,
            seek_count=totals.seek,
            clock_gap_count=totals.clock_gap,
        )

    def set_lineages(self, lineages: Mapping[str, str]) -> None:
        """Name the stable lineage of each selected entity that exists right now."""
        self._lineages = dict(lineages)

    def seed_resume_hints(
        self, hints: Iterable[ResumeHint], guarded: Iterable[str]
    ) -> None:
        """Restore the marker slots each selected speaker held before a restart."""
        restored = tuple(hints)
        self._markers = {hint.lineage: hint for hint in restored if not hint.prior}
        self._priors = {hint.lineage: hint for hint in restored if hint.prior}
        self._guards = set(guarded)

    def take_marker_changes(self) -> MarkerDelta:
        """Return what changed in the durable marker state since the last call."""
        changes = self._marker_changes
        guards = sorted(self._guard_changes.items())
        self._marker_changes = {}
        self._guard_changes = {}
        ordered = sorted(changes.items(), key=lambda change: change[0])
        return MarkerDelta(
            markers_set=tuple(m for _slot, m in ordered if m is not None),
            lineages_cleared=tuple(
                lineage
                for (lineage, prior), marker in ordered
                if marker is None and not prior
            ),
            priors_cleared=tuple(
                lineage
                for (lineage, prior), marker in ordered
                if marker is None and prior
            ),
            guards_set=tuple(lineage for lineage, guarded in guards if guarded),
            guards_cleared=tuple(lineage for lineage, guarded in guards if not guarded),
        )

    def hold_changed_groups(self, topology: Topology) -> Topology:
        """Return the topology with every group new to its streams still uncertain."""
        current = {(stream.key, stream.members) for stream in self._streams.values()}
        settled = tuple(
            group
            for group in topology.groups
            if (group.leader, group.members) in current
        )
        if len(settled) == len(topology.groups):
            return topology
        waiting = frozenset().union(
            *(group.members for group in topology.groups if group not in settled)
        )
        tracked = frozenset().union(
            *(stream.members for stream in self._streams.values())
        )
        return Topology(settled, topology.uncertain | (waiting & tracked))

    def apply(
        self,
        topology: Topology,
        samples: Mapping[str, PlaybackSample],
        now: float,
    ) -> tuple[QualifiedPlay, ...]:
        """Move engines to their groups and account for time in every stream.

        Each stream reads its leader's sample; one without news still gets its
        time, because an event elsewhere re-arms the timer it was waiting for.
        """
        events = self._reconcile(topology, samples, now)
        for stream in self._streams.values():
            sample = samples.get(stream.key)
            if sample is None:
                events.extend(stream.engine.tick(now))
            else:
                events.extend(self._feed(stream, sample, now))
        self._update_markers()
        return tuple(events)

    def tick(self, now: float) -> tuple[QualifiedPlay, ...]:
        """Account for the time up to now in every stream."""
        return tuple(
            play
            for stream in self._streams.values()
            for play in stream.engine.tick(now)
        )

    def seconds_until_next_tick(self) -> float | None:
        """Return the shortest delay any stream needs, or None if nothing counts."""
        delays = (
            stream.engine.seconds_until_next_tick() for stream in self._streams.values()
        )
        return min((delay for delay in delays if delay is not None), default=None)

    def _reconcile(
        self,
        topology: Topology,
        samples: Mapping[str, PlaybackSample],
        now: float,
    ) -> list[QualifiedPlay]:
        previous = [
            (stream.members, stream.engine.active_occurrence)
            for stream in self._streams.values()
        ]
        held = frozenset(stream.unmarked for stream in self._streams.values())
        playing = {
            stream.current.occurrence_id: stream.current
            for stream in self._streams.values()
            if stream.current is not None
        }
        followed = {
            member: (stream.key, stream.members)
            for stream in self._streams.values()
            for member in stream.members
        }
        unmatched = {
            (stream.key, stream.members): stream for stream in self._streams.values()
        }
        streams: dict[str, _Stream] = {}
        open_groups: list[CanonicalGroup] = []
        for group in topology.groups:
            stream = unmatched.pop((group.leader, group.members), None)
            if stream is None:
                open_groups.append(group)
            else:
                stream.certain = True
                streams[group.leader] = stream

        open_streams = list(unmatched.values())
        for stream, group in _handoffs(open_streams, open_groups):
            stream.key, stream.members, stream.certain = (
                group.leader,
                group.members,
                True,
            )
            streams[group.leader] = stream
            open_streams.remove(stream)
            open_groups.remove(group)

        events: list[QualifiedPlay] = []
        grouped = frozenset().union(*(group.members for group in topology.groups))
        for stream in open_streams:
            remaining = stream.members - grouped
            if remaining & topology.uncertain:
                stream.members = remaining
                if stream.key not in remaining:
                    stream.key = min(remaining & topology.uncertain)
                stream.certain = False
                streams[stream.key] = stream
            else:
                events.extend(stream.engine.end_play(now))
                self._retired = self._retired.plus(stream.engine.snapshot)

        for group in open_groups:
            streams[group.leader] = self._successor(
                group.leader,
                group.members,
                previous,
                held,
                playing,
                samples,
                certain=True,
            )
        covered = frozenset().union(*(stream.members for stream in streams.values()))
        for speaker in sorted(topology.uncertain - covered):
            streams[speaker] = self._successor(
                speaker,
                frozenset({speaker}),
                previous,
                held,
                playing,
                samples,
                certain=False,
            )
        self._streams = streams
        self._note_joins(followed)
        return events

    def _note_joins(self, followed: Mapping[str, tuple[str, frozenset[str]]]) -> None:
        """Remember who now reads another speaker's stream.

        Only that makes a marker worth keeping: a leader stays on its own feed and
        a split adds nobody. A leader that lost the lead reads another speaker's
        feed from now on, even with unchanged members. A speaker not seen before
        was alone as far as anyone knows, so a group found at startup counts as a
        join.
        """
        for stream in self._streams.values():
            for member in stream.members:
                lineage = self._lineages.get(member)
                if lineage is None:
                    continue
                key, members = followed.get(member, (member, frozenset({member})))
                if len(stream.members) == 1:
                    self._joined.discard(lineage)
                elif stream.key != key and (key == member or stream.members - members):
                    self._joined.add(lineage)

    def _successor(
        self,
        key: str,
        members: frozenset[str],
        previous: list[tuple[frozenset[str], ActiveOccurrence | None]],
        held: frozenset[str | None],
        playing: Mapping[str, CurrentPlayback],
        samples: Mapping[str, PlaybackSample],
        *,
        certain: bool,
    ) -> _Stream:
        """Start a stream; a track it inherits keeps its identity and cannot earn."""
        engine = ObservationEngine(self._new_occurrence_id)
        inherited = sorted(
            (
                occurrence
                for old_members, occurrence in previous
                if occurrence is not None and old_members & members
            ),
            key=lambda occurrence: occurrence.track_key,
        )
        stream = _Stream(key, members, engine, certain)
        if inherited:
            sample = samples.get(key)
            current = sample.track_key if sample is not None else None
            occurrence = next(
                (o for o in inherited if o.track_key == current), inherited[0]
            )
            engine.resume_occurrence(occurrence, suppressed=True)
            if occurrence.occurrence_id in held:
                stream.unmarked = occurrence.occurrence_id
            stream.current = _held(playing.get(occurrence.occurrence_id))
        return stream

    def _feed(
        self, stream: _Stream, sample: PlaybackSample, now: float
    ) -> tuple[QualifiedPlay, ...]:
        if not stream.certain:
            stream.showing = None
            stream.current = _held(stream.current)
            if len(stream.members) > 1 or sample.status not in _OWN_STATUS_WINS:
                sample = replace(sample, status=PlaybackStatus.TOPOLOGY_UNCERTAIN)
            return stream.engine.observe(sample, now)
        stream.showing = sample.track_key if _shows_track(sample) else None
        stream.fresh_start = _is_fresh_start(sample)
        doubtful = stream.showing is not None and self._decide_marker(
            stream, stream.showing, sample
        )
        plays = stream.engine.observe(sample, now)
        if doubtful:
            active = stream.engine.active_occurrence
            stream.unmarked = active.occurrence_id if active else None
        self._note_current(stream, sample)
        return plays

    @staticmethod
    def _note_current(stream: _Stream, sample: PlaybackSample) -> None:
        """Follow the certain sample; only a stop or a lost source ends the entry."""
        active = stream.engine.active_occurrence
        current = stream.current
        if (
            current is not None
            and active is not None
            and active.occurrence_id == current.occurrence_id
            and sample.status is PlaybackStatus.PLAYING
            and sample.track_key is None
        ):
            stream.current = _held(current)
            return
        stream.current = (
            CurrentPlayback(
                occurrence_id=active.occurrence_id,
                metadata=sample.metadata,
                started_at=active.started_at,
                duration_seconds=sample.duration_seconds,
                speaker=stream.key,
            )
            if active is not None
            and active.started_at is not None
            and active.track_key == sample.track_key
            and sample.metadata is not None
            and sample.status is PlaybackStatus.PLAYING
            and sample.category is SourceCategory.SPOTIFY_CONFIRMED
            else None
        )

    def _decide_marker(
        self, stream: _Stream, track_key: bytes, sample: PlaybackSample
    ) -> bool:
        """Resume this stream's own marker of the track, or suppress on any doubt.

        A prior marker decides like an active one, except that a proven fresh start
        of its track simply earns. Return whether this playback cannot be told apart
        from another identity, because a member's guard is set or markers of
        different occurrences contest the track: it then follows no marker and
        changes none, so it cannot become resumable either.
        """
        active = stream.engine.active_occurrence
        if active is not None and active.track_key == track_key:
            return False
        if not stream.fresh_start and self._guards.intersection(
            self._own_lineages(stream)
        ):
            stream.engine.suppress_until_transition(track_key)
            return True
        active_slots = [
            marker
            for marker in self._own_markers(stream)
            if marker.track_key == track_key
        ]
        prior_slots = [
            marker
            for marker in self._own_priors(stream)
            if marker.track_key == track_key
        ]
        if not active_slots and (not prior_slots or stream.fresh_start):
            return False
        matching = [*active_slots, *prior_slots]
        resumable = [marker for marker in matching if _may_resume(marker, sample)]
        candidates = resumable or matching
        if len({marker.occurrence_id for marker in candidates}) > 1:
            stream.engine.suppress_until_transition(track_key)
            return True
        marker = candidates[0]
        stream.engine.resume_occurrence(
            ActiveOccurrence(
                occurrence_id=marker.occurrence_id,
                track_key=marker.track_key,
                started_at=marker.started_at,
                qualified=False,
            ),
            suppressed=not resumable,
        )
        return False

    def _own_lineages(self, stream: _Stream) -> list[str]:
        return [
            self._lineages[member]
            for member in sorted(stream.members)
            if member in self._lineages
        ]

    def _own_markers(self, stream: _Stream) -> list[ResumeHint]:
        lineages = self._own_lineages(stream)
        return [self._markers[name] for name in lineages if name in self._markers]

    def _own_priors(self, stream: _Stream) -> list[ResumeHint]:
        lineages = self._own_lineages(stream)
        return [self._priors[name] for name in lineages if name in self._priors]

    def _update_markers(self) -> None:
        """Follow what each speaker's own stream proved; nothing else moves a marker."""
        for stream in self._streams.values():
            occurrence = stream.engine.active_occurrence
            self._guard_held_stream(stream, occurrence)
            if (
                occurrence is None
                or occurrence.track_key != stream.showing
                or occurrence.occurrence_id == stream.unmarked
            ):
                continue
            leader = self._lineages.get(stream.key)
            for lineage in self._own_lineages(stream):
                self._set_marker(
                    lineage,
                    occurrence,
                    fresh_start=stream.fresh_start,
                    own_queue=lineage == leader,
                )
        existing = frozenset(self._lineages.values())
        held = self._markers.keys() | self._priors.keys() | self._guards
        for lineage in sorted(held - existing):
            self._markers.pop(lineage, None)
            self._priors.pop(lineage, None)
            self._guards.discard(lineage)
            self._marker_changes[lineage, False] = None
            self._marker_changes.pop((lineage, True), None)
            self._guard_changes.pop(lineage, None)

    def _guard_held_stream(
        self, stream: _Stream, occurrence: ActiveOccurrence | None
    ) -> None:
        """Pass a guard to every member, so a held track cannot leave with one."""
        lineages = self._own_lineages(stream)
        if (
            occurrence is not None
            and occurrence.occurrence_id == stream.unmarked
            and self._guards.intersection(lineages)
        ):
            for lineage in lineages:
                self._set_guard(lineage)

    def _set_marker(
        self,
        lineage: str,
        occurrence: ActiveOccurrence,
        *,
        fresh_start: bool,
        own_queue: bool,
    ) -> None:
        held = self._markers.get(lineage)
        if held is not None and held.occurrence_id == occurrence.occurrence_id:
            # A follower shares this identity only as long as the leader stays on it.
            if own_queue:
                self._joined.discard(lineage)
            return
        if occurrence.started_at is None:
            return
        if held is not None and lineage in self._joined:
            self._set_prior(replace(held, prior=True))
        elif fresh_start:
            self._clear_prior(lineage, occurrence.track_key)
        self._joined.discard(lineage)
        if fresh_start and own_queue:
            self._clear_guard(lineage)
        marker = ResumeHint(
            occurrence_id=occurrence.occurrence_id,
            track_key=occurrence.track_key,
            started_at=occurrence.started_at,
            lineage=lineage,
        )
        self._markers[lineage] = marker
        self._marker_changes[lineage, False] = marker

    def _set_prior(self, marker: ResumeHint) -> None:
        overwritten = self._priors.get(marker.lineage)
        if overwritten is not None and (
            overwritten.occurrence_id != marker.occurrence_id
        ):
            self._set_guard(marker.lineage)
        self._priors[marker.lineage] = marker
        self._marker_changes[marker.lineage, True] = marker

    def _clear_prior(self, lineage: str, track_key: bytes) -> None:
        prior = self._priors.get(lineage)
        if prior is not None and prior.track_key == track_key:
            del self._priors[lineage]
            self._marker_changes[lineage, True] = None

    def _set_guard(self, lineage: str) -> None:
        if lineage not in self._guards:
            self._guards.add(lineage)
            self._guard_changes[lineage] = True

    def _clear_guard(self, lineage: str) -> None:
        if lineage in self._guards:
            self._guards.remove(lineage)
            self._guard_changes[lineage] = False
