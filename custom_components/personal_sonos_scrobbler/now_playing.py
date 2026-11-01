"""Decide which current track the account and the sensor show (stdlib only).

Only the arrival of an occurrence can change the answer, so a static update of an
older room never flips the display back. A failed or superseded notice is not tried
again. An occurrence is forgotten once it leaves the snapshot, which makes a resume
a new notice. Everything here lives in RAM and is bounded by the snapshot.
"""

from .playback import CurrentPlayback


class NowPlayingBoard:
    """Hold the latest snapshot and at most one notice still to send."""

    def __init__(self) -> None:
        """Start with nothing playing."""
        self._current: tuple[CurrentPlayback, ...] = ()
        self._announced: set[str] = set()
        self._pending: str | None = None

    @property
    def ready(self) -> CurrentPlayback | None:
        """Return the notice that may leave now, read from the latest snapshot."""
        return next(
            (
                item
                for item in self._current
                if item.settled and item.occurrence_id == self._pending
            ),
            None,
        )

    def update(self, current: tuple[CurrentPlayback, ...]) -> None:
        """Take the whole current state; the newest arrival replaces a queued notice."""
        self._current = current
        playing = {item.occurrence_id for item in current}
        self._announced &= playing
        if self._pending not in playing:
            self._pending = None
        arrivals = [
            item
            for item in current
            if item.settled and item.occurrence_id not in self._announced
        ]
        if arrivals:
            self._pending = max(
                arrivals, key=lambda item: (item.started_at, item.occurrence_id)
            ).occurrence_id
            self._announced.update(item.occurrence_id for item in arrivals)

    def take(self) -> CurrentPlayback | None:
        """Hand out the ready notice once."""
        notice = self.ready
        if notice is not None:
            self._pending = None
        return notice

    def reset(self) -> None:
        """Forget what was announced: a new account or enable starts from now."""
        self._announced.clear()
        self._pending = None
        self.update(self._current)


class TrackDisplay:
    """Choose the one track the local sensor shows; it sends and queues nothing.

    The newest arrival shows for as long as it plays and is proven. While it is
    unproven, or once it ends, the arrival before it shows, if that one is proven. An
    unsettled track keeps its place, so it takes the display back when it settles.
    """

    def __init__(self) -> None:
        """Start with nothing playing."""
        self._arrivals: dict[str, int] = {}
        self._count = 0

    def update(self, current: tuple[CurrentPlayback, ...]) -> CurrentPlayback | None:
        """Take the whole current state and return what to show now."""
        # Settled last: of two streams of one occurrence, a proven one speaks for it.
        playing = {
            item.occurrence_id: item
            for item in sorted(current, key=lambda item: item.settled)
        }
        self._arrivals = {
            occurrence_id: order
            for occurrence_id, order in self._arrivals.items()
            if occurrence_id in playing
        }
        arrivals = (
            item
            for item in playing.values()
            if item.settled and item.occurrence_id not in self._arrivals
        )
        for item in sorted(
            arrivals, key=lambda item: (item.started_at, item.occurrence_id)
        ):
            self._count += 1
            self._arrivals[item.occurrence_id] = self._count
        return max(
            (playing[key] for key in self._arrivals if playing[key].settled),
            key=lambda item: self._arrivals[item.occurrence_id],
            default=None,
        )
