"""Tests for UserRelation operations (RFC-002 Time Horizon).

Tests the relation operations including:
- create() - Create user relation with initial time horizon
- update_time_horizon() - Apply SAFETY_COEFFICIENT on access
- list_inbound()/list_outbound() - Query relations
- expire() - Mark relation for fossilization
- fact_time_horizon() - Compute significance from inbound relations
- is_alive() - Check if relation is still alive
"""

import pytest
from datetime import date, timedelta

from system.core.relation import RelationOperations, SAFETY_COEFFICIENT, USER_RELATION_KINDS
from utils import uid
from utils.time import EPOCH, current_day, day_to_date
from system.exceptions import ResourceNotFound


# ============================================================================
# Test: current_day() utility
# ============================================================================

def test_current_day_returns_positive_int():
    """current_day() should return a positive integer."""
    day = current_day()
    assert isinstance(day, int)
    assert day > 0  # Should be > 0 since epoch is 2020-01-01


def test_day_to_date_roundtrip():
    """day_to_date() should convert days back to dates correctly."""
    test_day = 2229  # 2026-02-07
    converted_date = day_to_date(test_day)
    expected_date = date(2026, 2, 7)
    assert converted_date == expected_date


# ============================================================================
# Test: create() operation
# ============================================================================

def test_create_relation_basic(core, sample_entity):
    """create() should create a user relation with initial time horizon."""
    source = sample_entity
    target = core.entity.create("Artifact")

    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
    )

    # Verify UUID has core_ prefix
    assert relation_uuid.startswith("core_")

    # Verify relation was created
    relation = core.relation.get_by_id(relation_uuid)
    assert relation["kind"] == "explicit_link"
    assert relation["source"] == uid.strip_prefix(source)
    assert relation["target"] == uid.strip_prefix(target)
    assert relation["source_type"] == "entity"
    assert relation["target_type"] == "entity"


def test_create_relation_with_custom_horizon(core, sample_entity):
    """create() should use custom initial_horizon_days when provided."""
    source = sample_entity
    target = core.entity.create("Artifact")

    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=30,
    )

    relation = core.relation.get_by_id(relation_uuid)
    today = current_day()

    # time_horizon should be today + 30
    assert relation["time_horizon"] == today + 30
    assert relation["last_access_at"] == today
    assert relation["created_at"] == today


def test_create_relation_invalid_kind(core, sample_entity):
    """create() should raise ValueError for invalid relation kind."""
    source = sample_entity
    target = core.entity.create("Artifact")

    with pytest.raises(ValueError, match="Invalid relation kind"):
        core.relation.create(
            kind="invalid_kind",
            source=source,
            source_type="entity",
            target=target,
            target_type="entity",
        )


def test_create_relation_accepts_uuid_with_prefix(core, sample_entity):
    """create() should accept UUIDs with or without prefix."""
    source_with_prefix = sample_entity  # Already has core_ prefix
    target = core.entity.create("Artifact")

    # Should work with prefixed source
    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source_with_prefix,
        source_type="entity",
        target=target,
        target_type="entity",
    )

    assert relation_uuid.startswith("core_")


# ============================================================================
# Test: update_time_horizon() operation
# ============================================================================

def test_update_time_horizon_applies_safety_coefficient(core, sample_entity):
    """update_time_horizon() should apply SAFETY_COEFFICIENT on access."""
    source = sample_entity
    target = core.entity.create("Artifact")

    # Create relation with initial horizon
    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=7,
    )

    # Get initial state
    relation_before = core.relation.get_by_id(relation_uuid)
    initial_horizon = relation_before["time_horizon"]
    initial_last_access = relation_before["last_access_at"]

    # Simulate time passing (1 day later)
    # Since we can't actually wait, we'll verify the formula works
    # by manually updating last_access_at to simulate a day passing
    core._conn.execute(
        "UPDATE user_relation SET last_access_at = ? WHERE uuid = ?",
        (initial_last_access - 1, uid.strip_prefix(relation_uuid))
    )
    core._conn.commit()

    # Update time horizon
    core.relation.update_time_horizon(relation_uuid)

    # Verify update
    relation_after = core.relation.get_by_id(relation_uuid)
    delta = 1  # One day passed
    expected_increase = int(delta * SAFETY_COEFFICIENT)

    assert relation_after["time_horizon"] == initial_horizon + expected_increase
    assert relation_after["last_access_at"] == current_day()


def test_update_time_horizon_nonexistent_relation(core):
    """update_time_horizon() should raise ResourceNotFound for invalid UUID."""
    with pytest.raises(ResourceNotFound):
        core.relation.update_time_horizon("core_nonexistent")


# ============================================================================
# Test: list_inbound() and list_outbound() operations
# ============================================================================

def test_list_inbound_returns_relations_pointing_to_target(core, sample_entity):
    """list_inbound() should return all relations pointing to target."""
    target = sample_entity
    source1 = core.entity.create("Artifact")
    source2 = core.entity.create("Artifact")

    # Create two relations pointing to target
    rel1 = core.relation.create(
        kind="explicit_link",
        source=source1,
        source_type="entity",
        target=target,
        target_type="entity",
    )
    rel2 = core.relation.create(
        kind="explicit_link",
        source=source2,
        source_type="entity",
        target=target,
        target_type="entity",
    )

    # List inbound relations
    inbound = core.relation.list_inbound(target)

    assert len(inbound) == 2
    relation_uuids = {uid.add_core_prefix(r["uuid"]) for r in inbound}
    assert rel1 in relation_uuids
    assert rel2 in relation_uuids


def test_list_outbound_returns_relations_from_source(core, sample_entity):
    """list_outbound() should return all relations from source."""
    source = sample_entity
    target1 = core.entity.create("Artifact")
    target2 = core.entity.create("Artifact")

    # Create two relations from source
    rel1 = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target1,
        target_type="entity",
    )
    rel2 = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target2,
        target_type="entity",
    )

    # List outbound relations
    outbound = core.relation.list_outbound(source)

    assert len(outbound) == 2
    relation_uuids = {uid.add_core_prefix(r["uuid"]) for r in outbound}
    assert rel1 in relation_uuids
    assert rel2 in relation_uuids


def test_list_inbound_alive_only_filters_expired_relations(core, sample_entity):
    """list_inbound(alive_only=True) should filter out expired relations."""
    target = sample_entity
    source = core.entity.create("Artifact")

    # Create relation
    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=7,
    )

    # Verify it's in the alive list
    inbound_alive = core.relation.list_inbound(target, alive_only=True)
    assert len(inbound_alive) == 1

    # Expire the relation
    core.relation.expire(relation_uuid)

    # Verify it's no longer in the alive list
    inbound_alive_after = core.relation.list_inbound(target, alive_only=True)
    assert len(inbound_alive_after) == 0

    # But still in the full list
    inbound_all = core.relation.list_inbound(target, alive_only=False)
    assert len(inbound_all) == 1


# ============================================================================
# Test: expire() operation
# ============================================================================

def test_expire_sets_time_horizon_to_past(core, sample_entity):
    """expire() should set time_horizon to yesterday."""
    source = sample_entity
    target = core.entity.create("Artifact")

    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=7,
    )

    # Verify relation is alive
    assert core.relation.is_alive(relation_uuid)

    # Expire the relation
    core.relation.expire(relation_uuid)

    # Verify relation is no longer alive
    assert not core.relation.is_alive(relation_uuid)

    # Verify time_horizon was set to yesterday
    relation = core.relation.get_by_id(relation_uuid)
    assert relation["time_horizon"] == current_day() - 1


def test_expire_nonexistent_relation(core):
    """expire() should raise ResourceNotFound for invalid UUID."""
    with pytest.raises(ResourceNotFound):
        core.relation.expire("core_nonexistent")


# ============================================================================
# Test: fact_time_horizon() operation
# ============================================================================

def test_fact_time_horizon_returns_max_of_inbound_relations(core, sample_entity):
    """fact_time_horizon() should return max time_horizon from inbound relations."""
    target = sample_entity
    source1 = core.entity.create("Artifact")
    source2 = core.entity.create("Artifact")

    # Create two relations with different horizons
    core.relation.create(
        kind="explicit_link",
        source=source1,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=7,
    )
    core.relation.create(
        kind="explicit_link",
        source=source2,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=30,
    )

    # fact_time_horizon should return the max (30 days from today)
    horizon = core.relation.fact_time_horizon(target)
    today = current_day()

    assert horizon is not None
    assert horizon == today + 30


def test_fact_time_horizon_returns_none_for_orphaned_fact(core, sample_entity):
    """fact_time_horizon() should return None for facts with no inbound relations."""
    # Entity has no relations
    horizon = core.relation.fact_time_horizon(sample_entity)
    assert horizon is None


# ============================================================================
# Test: is_alive() operation
# ============================================================================

def test_is_alive_true_for_active_relation(core, sample_entity):
    """is_alive() should return True for relations with time_horizon >= today."""
    source = sample_entity
    target = core.entity.create("Artifact")

    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=7,
    )

    assert core.relation.is_alive(relation_uuid)


def test_is_alive_false_for_expired_relation(core, sample_entity):
    """is_alive() should return False for relations with time_horizon < today."""
    source = sample_entity
    target = core.entity.create("Artifact")

    relation_uuid = core.relation.create(
        kind="explicit_link",
        source=source,
        source_type="entity",
        target=target,
        target_type="entity",
        initial_horizon_days=7,
    )

    # Expire the relation
    core.relation.expire(relation_uuid)

    assert not core.relation.is_alive(relation_uuid)


# ============================================================================
# Test: USER_RELATION_KINDS constant
# ============================================================================

def test_user_relation_kinds_contains_explicit_link():
    """USER_RELATION_KINDS should contain 'explicit_link'."""
    assert "explicit_link" in USER_RELATION_KINDS


def test_safety_coefficient_is_1_2():
    """SAFETY_COEFFICIENT should be 1.2 per RFC-002."""
    assert SAFETY_COEFFICIENT == 1.2
