"""Canonical groups come only from lists that every member agrees on."""

from collections.abc import Iterator

import pytest

from custom_components.personal_sonos_scrobbler.normalize import MAX_GROUP_MEMBERS
from custom_components.personal_sonos_scrobbler.topology import (
    MAX_TRACKED_PLAYERS,
    CanonicalGroup,
    MemberView,
    Topology,
    member_view,
    read_group_members,
    resolve_topology,
)

A, B, C = "media_player.a", "media_player.b", "media_player.c"
REGISTERED = frozenset({A, B, C})


def views(**lists: tuple[str, ...] | None) -> dict[str, MemberView]:
    """Build available views from keyword lists, a=(A, B) meaning A publishes it."""
    built = {f"media_player.{name}": members for name, members in lists.items()}
    return {
        entity_id: MemberView(entity_id, True, members)
        for entity_id, members in built.items()
    }


def group(leader: str, *others: str) -> CanonicalGroup:
    return CanonicalGroup(leader, frozenset({leader, *others}))


def test_two_standalone_speakers_are_two_groups() -> None:
    topology = resolve_topology(views(a=(A,), b=(B,)), REGISTERED, frozenset({A, B}))

    assert topology == Topology((group(A), group(B)), frozenset())


def test_group_is_listed_once_with_the_first_member_as_leader() -> None:
    lists = views(a=(B, A, C), b=(B, A, C), c=(B, A, C))

    topology = resolve_topology(lists, REGISTERED, frozenset({A, C}))

    assert topology == Topology((group(B, A, C),), frozenset())


def test_group_without_a_selected_member_is_left_out() -> None:
    topology = resolve_topology(views(a=(A,), b=(B,)), REGISTERED, frozenset({A}))

    assert topology == Topology((group(A),), frozenset())


@pytest.mark.parametrize(
    "members",
    [None, (), (A, A), (B,), (A, "media_player.unregistered")],
    ids=["none", "empty", "duplicate", "without_itself", "unregistered"],
)
def test_invalid_list_is_uncertain(members: tuple[str, ...] | None) -> None:
    topology = resolve_topology(views(a=members, b=(B,)), REGISTERED, frozenset({A}))

    assert topology == Topology((), frozenset({A}))


def test_list_at_the_size_limit_is_valid_and_one_more_is_not() -> None:
    names = [f"media_player.p{index}" for index in range(MAX_GROUP_MEMBERS + 1)]
    registered = frozenset(names)
    fitting = tuple(names[:MAX_GROUP_MEMBERS])
    oversized = tuple(names)

    assert resolve_topology(
        {name: MemberView(name, True, fitting) for name in fitting},
        registered,
        frozenset({names[0]}),
    ).groups
    assert not resolve_topology(
        {name: MemberView(name, True, oversized) for name in oversized},
        registered,
        frozenset({names[0]}),
    ).groups


def test_selected_speaker_without_a_view_is_uncertain() -> None:
    topology = resolve_topology({}, REGISTERED, frozenset({A}))

    assert topology == Topology((), frozenset({A}))


def test_unregistered_selected_speaker_is_uncertain_on_its_own_list() -> None:
    topology = resolve_topology(views(a=(A,)), frozenset({B}), frozenset({A}))

    assert topology == Topology((), frozenset({A}))


@pytest.mark.parametrize("unavailable", [A, B], ids=["leader", "follower"])
def test_unavailable_member_makes_the_group_uncertain(unavailable: str) -> None:
    lists = views(a=(A, B), b=(A, B))
    lists[unavailable] = MemberView(unavailable, False, (A, B))

    topology = resolve_topology(lists, REGISTERED, frozenset({A, B}))

    assert topology == Topology((), frozenset({A, B}))


def test_member_without_a_view_makes_the_group_uncertain() -> None:
    topology = resolve_topology(views(a=(A, B)), REGISTERED, frozenset({A}))

    assert topology == Topology((), frozenset({A}))


def test_leader_published_join_before_the_follower_is_uncertain() -> None:
    topology = resolve_topology(views(a=(A, B), b=(B,)), REGISTERED, frozenset({A, B}))

    assert topology == Topology((), frozenset({A, B}))


def test_soloist_still_claimed_by_a_group_is_uncertain() -> None:
    lists = views(a=(A, B, C), b=(B,), c=(A, B, C))

    topology = resolve_topology(lists, REGISTERED, frozenset({B}))

    assert topology == Topology((), frozenset({B}))


def test_conflicting_order_is_uncertain() -> None:
    topology = resolve_topology(views(a=(A, B), b=(B, A)), REGISTERED, frozenset({A}))

    assert topology == Topology((), frozenset({A}))


def test_uncertain_group_leaves_an_independent_group_alone() -> None:
    lists = views(a=(A, B), b=(B,), c=(C,))

    topology = resolve_topology(lists, REGISTERED, frozenset({A, B, C}))

    assert topology == Topology((group(C),), frozenset({A, B}))


def test_too_many_tracked_players_is_uncertain() -> None:
    limit = frozenset(f"media_player.p{i}" for i in range(MAX_TRACKED_PLAYERS - 1))

    assert resolve_topology(views(a=(A,)), limit | {A}, frozenset({A})).groups
    assert resolve_topology(views(a=(A,)), limit | {A, B}, frozenset({A})) == Topology(
        (), frozenset({A})
    )


class UniterableList(list[str]):
    """A list that proves the bound is checked before any iteration."""

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("iterated an oversized list")


class UniterableTuple(tuple[str, ...]):
    """The same proof for a view that was not built by read_group_members."""

    __slots__ = ()

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("iterated an oversized list")


OVERSIZED = [f"media_player.p{index}" for index in range(MAX_GROUP_MEMBERS + 1)]


def test_oversized_list_is_rejected_before_any_iteration() -> None:
    assert read_group_members(UniterableList(OVERSIZED)) is None
    assert member_view(A, True, UniterableList(OVERSIZED)).unreadable_list is True


def test_oversized_own_list_is_uncertain() -> None:
    lists = {A: member_view(A, True, [A, *OVERSIZED]), **views(b=(B,))}

    topology = resolve_topology(lists, REGISTERED, frozenset({A}))

    assert topology == Topology((), frozenset({A}))


def test_oversized_claimant_keeps_a_soloist_uncertain() -> None:
    claimant = member_view(A, True, [A, B, *OVERSIZED])
    lists = {A: claimant, **views(b=(B,))}

    topology = resolve_topology(lists, REGISTERED, frozenset({B}))

    assert topology == Topology((), frozenset({B}))


@pytest.mark.parametrize(
    "unreadable", [[A, B, 1], "media_player.b", {B: 1}], ids=["item", "text", "map"]
)
def test_malformed_claimant_keeps_a_soloist_uncertain(unreadable: object) -> None:
    lists = {A: member_view(A, True, unreadable), **views(b=(B,))}

    topology = resolve_topology(lists, REGISTERED, frozenset({B}))

    assert topology == Topology((), frozenset({B}))


def test_absent_list_claims_nobody() -> None:
    lists = {A: member_view(A, True, None), **views(b=(B,))}

    topology = resolve_topology(lists, REGISTERED, frozenset({B}))

    assert topology == Topology((group(B),), frozenset())


def test_view_with_an_oversized_tuple_is_not_iterated() -> None:
    oversized = UniterableTuple(OVERSIZED)
    lists = {A: MemberView(A, True, oversized), **views(b=(B,))}

    assert resolve_topology(lists, REGISTERED, frozenset({A})).uncertain == {A}
    assert resolve_topology(lists, REGISTERED, frozenset({B})).uncertain == {B}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([A, B], (A, B)),
        ((A,), (A,)),
        ([], ()),
        (None, None),
        (A, None),
        ([A, 1], None),
        ({A: 1}, None),
    ],
)
def test_read_group_members(value: object, expected: object) -> None:
    assert read_group_members(value) == expected
