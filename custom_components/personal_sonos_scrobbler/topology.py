"""Derive canonical Sonos groups from public group lists, failing closed.

Sonos writes a regroup one entity at a time, so two entities can briefly disagree.
A group only counts once every entity that mentions one of its members publishes
exactly the same list. A list that cannot be read could mention anyone, so while
one exists no group counts.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .normalize import MAX_GROUP_MEMBERS

MAX_TRACKED_PLAYERS = 128


@dataclass(frozen=True, slots=True)
class MemberView:
    """What one entity publishes about its own group.

    group_members is None when nothing readable was published; unreadable_list
    tells a list that was published but rejected from no list at all.
    """

    entity_id: str
    available: bool
    group_members: tuple[str, ...] | None
    unreadable_list: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalGroup:
    """A group every member agrees on, led by the first entry of its list."""

    leader: str
    members: frozenset[str]


@dataclass(frozen=True, slots=True)
class Topology:
    """Groups with a selected member, and selected speakers without a proven group."""

    groups: tuple[CanonicalGroup, ...]
    uncertain: frozenset[str]


def read_group_members(value: object) -> tuple[str, ...] | None:
    """Return a published list as strings, or None if it is not a usable one.

    The length is checked first: this is public input of any size.
    """
    if not isinstance(value, list | tuple) or len(value) > MAX_GROUP_MEMBERS:
        return None
    if not all(isinstance(member, str) for member in value):
        return None
    return tuple(value)


def member_view(entity_id: str, available: bool, value: object) -> MemberView:
    """Read one entity's published list, remembering a rejected one."""
    members = read_group_members(value)
    return MemberView(
        entity_id, available, members, value is not None and members is None
    )


def resolve_topology(
    views: Mapping[str, MemberView],
    registered: frozenset[str],
    selected: frozenset[str],
) -> Topology:
    """Return the proven groups; every other selected speaker is uncertain."""
    if len(registered) > MAX_TRACKED_PLAYERS:
        return Topology((), selected)
    groups: dict[str, CanonicalGroup] = {}
    for speaker in sorted(selected):
        group = _proven_group(speaker, views, registered)
        if group is not None:
            groups[group.leader] = group
    grouped = frozenset().union(*(group.members for group in groups.values()))
    return Topology(tuple(groups[key] for key in sorted(groups)), selected - grouped)


def _proven_group(
    speaker: str, views: Mapping[str, MemberView], registered: frozenset[str]
) -> CanonicalGroup | None:
    view = views.get(speaker)
    published = view.group_members if view is not None else None
    if not published or not _is_valid_list(speaker, published, registered):
        return None
    members = frozenset(published)
    if not all(_confirms(views.get(member), published) for member in members):
        return None
    if any(_contradicts(other, published, members) for other in views.values()):
        return None
    return CanonicalGroup(published[0], members)


def _is_valid_list(
    speaker: str, published: tuple[str, ...], registered: frozenset[str]
) -> bool:
    if len(published) > MAX_GROUP_MEMBERS:
        return False
    members = set(published)
    return (
        speaker in members and len(members) == len(published) and members <= registered
    )


def _confirms(view: MemberView | None, published: tuple[str, ...]) -> bool:
    return view is not None and view.available and view.group_members == published


def _contradicts(
    view: MemberView, published: tuple[str, ...], members: frozenset[str]
) -> bool:
    """Tell whether another entity still claims a member under a different list."""
    other = view.group_members
    if view.unreadable_list or (other is not None and len(other) > MAX_GROUP_MEMBERS):
        return True
    return other is not None and other != published and not members.isdisjoint(other)
