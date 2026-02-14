"""Tests for Context Frame and View Stream operations (RFC-003).

Tests the context operations including:
- get_context_frame() - Get or create ContextFrame for owner
- update_containers() - LRU-N eviction on visit
- create_view() - Create View record
- append_view() - Append to view timeline
- is_substantive_type() / is_primitive_type() - Type classification

Invariants tested:
- INV-1: Unique View UUID
- INV-12: LRU-N Limit (containers ≤ N)
- INV-17: Substantive vs Primitive classification
- INV-18: Type-Based Classification
- INV-19: Hardcoded Initial Classification
- INV-20: One Primary Context Per Owner
- INV-26: No Shared ContextFrame
"""

import pytest

from system.core.context import (
    ContextOperations,
    ContextFrame,
    View,
    ViewAction,
    DEFAULT_CONTEXT_SIZE,
    CONTEXT_SIZE_MIN,
    CONTEXT_SIZE_MAX,
    SUBSTANTIVE_TYPES,
    PRIMITIVE_TYPES,
)
from system.exceptions import ResourceNotFound, ValidationError
from utils import datetime as isodatetime


# ============================================================================
# Test: get_context_frame() operation
# ============================================================================

def test_get_context_frame_creates_new(core):
    """get_context_frame() should create new ContextFrame if not exists."""
    owner_uuid = core.entity.create("Operator")

    context_frame = core.context.get_context_frame(
        owner=owner_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Verify ContextFrame structure
    assert isinstance(context_frame, ContextFrame)
    assert context_frame.owner == owner_uuid
    assert context_frame.owner_type == "operator"
    assert context_frame.containers == []
    assert context_frame.view_timeline == []
    assert context_frame.is_subordinate is False
    assert context_frame.parent_frame_uuid is None
    assert context_frame.uuid.startswith("core_")


def test_get_context_frame_returns_existing(core):
    """get_context_frame() should return existing ContextFrame."""
    owner_uuid = core.entity.create("Operator")

    # Create first ContextFrame
    context_frame1 = core.context.get_context_frame(
        owner=owner_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Get same ContextFrame again
    context_frame2 = core.context.get_context_frame(
        owner=owner_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Should return same ContextFrame (same UUID)
    assert context_frame1.uuid == context_frame2.uuid
    assert context_frame1.owner == context_frame2.owner


def test_get_context_frame_not_found_raises(core):
    """get_context_frame() should raise ResourceNotFound if not exists and create_if_missing=False."""
    owner_uuid = "nonexistent_uuid"

    with pytest.raises(ResourceNotFound, match="ContextFrame for operator"):
        core.context.get_context_frame(
            owner=owner_uuid,
            owner_type="operator",
            create_if_missing=False
        )


def test_get_context_frame_by_uuid(core):
    """get_context_frame_by_uuid() should return ContextFrame by UUID."""
    owner_uuid = core.entity.create("Operator")

    # Create ContextFrame
    context_frame1 = core.context.get_context_frame(
        owner=owner_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Get by UUID
    context_frame2 = core.context.get_context_frame_by_uuid(context_frame1.uuid)

    # Should return same ContextFrame
    assert context_frame2.uuid == context_frame1.uuid
    assert context_frame2.owner == owner_uuid
    assert context_frame2.owner_type == "operator"


def test_get_context_frame_by_uuid_not_found_raises(core):
    """get_context_frame_by_uuid() should raise ResourceNotFound if UUID doesn't exist."""
    with pytest.raises(ResourceNotFound, match="ContextFrame.*not found"):
        core.context.get_context_frame_by_uuid("nonexistent_uuid")


def test_one_context_per_owner(core):
    """INV-20: Each owner should have exactly ONE primary ContextFrame."""
    owner_uuid = core.entity.create("Operator")

    # Create ContextFrame multiple times
    context_frame1 = core.context.get_context_frame(owner_uuid, "operator")
    context_frame2 = core.context.get_context_frame(owner_uuid, "operator")
    context_frame3 = core.context.get_context_frame(owner_uuid, "operator")

    # All should return same UUID
    assert context_frame1.uuid == context_frame2.uuid
    assert context_frame2.uuid == context_frame3.uuid


# ============================================================================
# Test: update_containers() operation
# ============================================================================

def test_update_containers_adds_new(core):
    """update_containers() should add new UUID to front of containers."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    visited_uuid = core.entity.create("Artifact")

    # Update containers
    updated_frame = core.context.update_containers(context_frame, visited_uuid)

    # Verify visited UUID is at front (most recent)
    assert len(updated_frame.containers) == 1
    assert updated_frame.containers[0] == visited_uuid


def test_update_containers_moves_to_front(core):
    """update_containers() should move existing UUID to front."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    # Add multiple entities
    entity1 = core.entity.create("Artifact")
    entity2 = core.entity.create("Artifact")
    entity3 = core.entity.create("Artifact")

    context_frame = core.context.update_containers(context_frame, entity1)
    context_frame = core.context.update_containers(context_frame, entity2)
    context_frame = core.context.update_containers(context_frame, entity3)

    # Containers should be [entity3, entity2, entity1]
    assert context_frame.containers == [entity3, entity2, entity1]

    # Revisit entity1 (should move to front)
    context_frame = core.context.update_containers(context_frame, entity1)

    # Containers should now be [entity1, entity3, entity2]
    assert context_frame.containers == [entity1, entity3, entity2]


def test_update_containers_lru_eviction(core):
    """INV-12: update_containers() should evict LRU when at capacity."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    # Add entities up to context size (default: 7)
    entities = []
    for i in range(DEFAULT_CONTEXT_SIZE):
        entity_uuid = core.entity.create("Artifact")
        entities.append(entity_uuid)
        context_frame = core.context.update_containers(context_frame, entity_uuid)

    # Containers should have all 7 entities
    assert len(context_frame.containers) == DEFAULT_CONTEXT_SIZE

    # Add one more entity (should evict entity0 - the LRU)
    new_entity = core.entity.create("Artifact")
    context_frame = core.context.update_containers(context_frame, new_entity)

    # Should still have 7 entities
    assert len(context_frame.containers) == DEFAULT_CONTEXT_SIZE

    # First entity should be evicted
    assert entities[0] not in context_frame.containers

    # Most recent entity should be at front
    assert context_frame.containers[0] == new_entity


def test_update_containers_custom_size(core):
    """update_containers() should respect custom context_size."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    custom_size = 5

    # Add entities up to custom size
    entities = []
    for i in range(custom_size):
        entity_uuid = core.entity.create("Artifact")
        entities.append(entity_uuid)
        context_frame = core.context.update_containers(context_frame, entity_uuid, context_size=custom_size)

    # Should have exactly custom_size entities
    assert len(context_frame.containers) == custom_size


def test_update_containers_invalid_size_raises(core):
    """update_containers() should raise ValidationError for invalid context_size."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    visited_uuid = core.entity.create("Artifact")

    # Too small
    with pytest.raises(ValidationError, match="context_size must be between"):
        core.context.update_containers(context_frame, visited_uuid, context_size=CONTEXT_SIZE_MIN - 1)

    # Too large
    with pytest.raises(ValidationError, match="context_size must be between"):
        core.context.update_containers(context_frame, visited_uuid, context_size=CONTEXT_SIZE_MAX + 1)


# ============================================================================
# Test: create_view() operation
# ============================================================================

def test_create_view_basic(core):
    """create_view() should create View with UUID and actions."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    actor_uuid = core.entity.create("Operator")

    actions = [
        ViewAction(
            type="update_entity",
            target=core.entity.create("Artifact"),
            timestamp=isodatetime.now()
        )
    ]

    view = core.context.create_view(
        context_frame_uuid=context_frame.uuid,
        actor=actor_uuid,
        actions=actions
    )

    # Verify View structure
    assert isinstance(view, View)
    assert view.uuid.startswith("core_")
    assert view.actor == actor_uuid
    assert len(view.actions) == 1
    assert view.actions[0].type == "update_entity"
    assert view.started_at == actions[0].timestamp
    assert view.ended_at is None  # Still active
    assert view.prev is None  # First view in stream
    assert view.context_frame_uuid == context_frame.uuid


def test_create_view_empty_actions_raises(core):
    """create_view() should raise ValidationError if actions list is empty."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")
    actor_uuid = core.entity.create("Operator")

    with pytest.raises(ValidationError, match="View must have at least one action"):
        core.context.create_view(
            context_frame_uuid=context_frame.uuid,
            actor=actor_uuid,
            actions=[]
        )


def test_create_view_with_prev(core):
    """create_view() should set prev pointer for linked list structure."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")
    actor_uuid = core.entity.create("Operator")

    # Create first view
    actions1 = [ViewAction(type="update_entity", target=core.entity.create("Artifact"), timestamp=isodatetime.now())]
    view1 = core.context.create_view(context_frame.uuid, actor_uuid, actions1)

    # Create second view with prev
    actions2 = [ViewAction(type="update_entity", target=core.entity.create("Artifact"), timestamp=isodatetime.now())]
    view2 = core.context.create_view(context_frame.uuid, actor_uuid, actions2, prev=view1.uuid)

    # Verify linked list structure (INV-9)
    assert view2.prev == view1.uuid
    assert view1.prev is None


def test_view_to_dict(core):
    """View.to_dict() should serialize View to dictionary."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")
    actor_uuid = core.entity.create("Operator")

    actions = [
        ViewAction(
            type="update_entity",
            target=core.entity.create("Artifact"),
            timestamp=isodatetime.now(),
            visited=[core.entity.create("Artifact")]
        )
    ]

    view = core.context.create_view(context_frame.uuid, actor_uuid, actions)

    # Convert to dict
    view_dict = view.to_dict()

    # Verify serialization
    assert view_dict["uuid"] == view.uuid
    assert view_dict["actor"] == actor_uuid
    assert len(view_dict["actions"]) == 1
    assert view_dict["actions"][0]["type"] == "update_entity"
    assert view_dict["started_at"] == view.started_at
    assert view_dict["ended_at"] is None
    assert view_dict["prev"] is None


# ============================================================================
# Test: append_view() operation
# ============================================================================

def test_append_view_to_timeline(core):
    """append_view() should add View UUID to context frame's view timeline."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")
    actor_uuid = core.entity.create("Operator")

    actions = [ViewAction(type="update_entity", target=core.entity.create("Artifact"), timestamp=isodatetime.now())]
    view = core.context.create_view(context_frame.uuid, actor_uuid, actions)

    # Append view to timeline
    updated_frame = core.context.append_view(context_frame, view)

    # Verify view UUID in timeline
    assert view.uuid in updated_frame.view_timeline
    assert updated_frame.view_timeline[-1] == view.uuid


def test_append_view_chronological(core):
    """append_view() should maintain chronological order."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")
    actor_uuid = core.entity.create("Operator")

    # Create multiple views
    views = []
    for i in range(3):
        actions = [ViewAction(type="update_entity", target=core.entity.create("Artifact"), timestamp=isodatetime.now())]
        view = core.context.create_view(context_frame.uuid, actor_uuid, actions)
        context_frame = core.context.append_view(context_frame, view)
        views.append(view)

    # Verify chronological order
    assert context_frame.view_timeline == [v.uuid for v in views]


# ============================================================================
# Test: Substantive vs Primitive classification
# ============================================================================

def test_is_substantive_type():
    """is_substantive_type() should return True for substantive types."""
    context_ops = ContextOperations(None)

    for entity_type in SUBSTANTIVE_TYPES:
        assert context_ops.is_substantive_type(entity_type), \
            f"{entity_type} should be substantive"


def test_is_primitive_type():
    """is_primitive_type() should return True for primitive types."""
    context_ops = ContextOperations(None)

    for entity_type in PRIMITIVE_TYPES:
        assert context_ops.is_primitive_type(entity_type), \
            f"{entity_type} should be primitive"


def test_substantive_and_primitive_are_mutually_exclusive():
    """Substantive and primitive types should not overlap."""
    context_ops = ContextOperations(None)

    for entity_type in SUBSTANTIVE_TYPES:
        assert not context_ops.is_primitive_type(entity_type), \
            f"{entity_type} cannot be both substantive and primitive"

    for entity_type in PRIMITIVE_TYPES:
        assert not context_ops.is_substantive_type(entity_type), \
            f"{entity_type} cannot be both primitive and substantive"


def test_unknown_type_is_neither():
    """Unknown entity types should be neither substantive nor primitive."""
    context_ops = ContextOperations(None)

    assert not context_ops.is_substantive_type("UnknownType")
    assert not context_ops.is_primitive_type("UnknownType")


# ============================================================================
# Test: ContextFrame.to_dict()
# ============================================================================

def test_context_frame_to_dict(core):
    """ContextFrame.to_dict() should serialize to dictionary."""
    owner_uuid = core.entity.create("Operator")
    context_frame = core.context.get_context_frame(owner_uuid, "operator")

    # Add some containers
    entity1 = core.entity.create("Artifact")
    context_frame = core.context.update_containers(context_frame, entity1)

    # Convert to dict
    frame_dict = context_frame.to_dict()

    # Verify serialization
    assert frame_dict["uuid"] == context_frame.uuid
    assert frame_dict["owner"] == owner_uuid
    assert frame_dict["owner_type"] == "operator"
    assert frame_dict["containers"] == [entity1]
    assert frame_dict["view_timeline"] == []
    assert frame_dict["is_subordinate"] is False
    assert frame_dict["parent_frame_uuid"] is None


# ============================================================================
# Test: Fork inheritance (INV-5)
# ============================================================================

def test_fork_inherits_parent_containers(core):
    """INV-5: Forked context should inherit parent's containers."""
    owner_uuid = core.entity.create("Operator")
    parent_frame = core.context.get_context_frame(owner_uuid, "operator")

    # Add containers to parent
    entity1 = core.entity.create("Artifact")
    entity2 = core.entity.create("Artifact")
    parent_frame = core.context.update_containers(parent_frame, entity1)
    parent_frame = core.context.update_containers(parent_frame, entity2)

    # Create subordinate context (fork)
    subordinate_frame = core.context._create_context_frame(
        owner=core.entity.create("Agent"),
        owner_type="agent",
        parent_frame_uuid=parent_frame.uuid
    )

    # Subordinate should inherit parent's containers
    assert subordinate_frame.containers == parent_frame.containers
    assert subordinate_frame.is_subordinate is True
    assert subordinate_frame.parent_frame_uuid == parent_frame.uuid


# ============================================================================
# Test: INV-26 - No Shared ContextFrame
# ============================================================================

def test_different_users_have_different_contexts(core):
    """INV-26: Different users should have different ContextFrames."""
    user1_uuid = core.entity.create("Operator")
    user2_uuid = core.entity.create("Operator")

    context_frame1 = core.context.get_context_frame(user1_uuid, "operator")
    context_frame2 = core.context.get_context_frame(user2_uuid, "operator")

    # Different UUIDs
    assert context_frame1.uuid != context_frame2.uuid

    # Independent containers
    entity1 = core.entity.create("Artifact")
    context_frame1 = core.context.update_containers(context_frame1, entity1)

    assert entity1 in context_frame1.containers
    assert entity1 not in context_frame2.containers


# ============================================================================
# Test: Scope ContextFrames
# ============================================================================

def test_scope_context_frame(core):
    """ContextFrames should work for scope owners too."""
    scope_uuid = core.entity.create("Artifact")  # Using Artifact as proxy for Scope

    context_frame = core.context.get_context_frame(
        owner=scope_uuid,
        owner_type="scope",
        create_if_missing=True
    )

    assert context_frame.owner_type == "scope"
    assert context_frame.owner == scope_uuid
    assert context_frame.is_subordinate is False


def test_agent_context_frame(core):
    """ContextFrames should work for agent owners."""
    agent_uuid = core.entity.create("Agent")

    context_frame = core.context.get_context_frame(
        owner=agent_uuid,
        owner_type="agent",
        create_if_missing=True
    )

    assert context_frame.owner_type == "agent"
    assert context_frame.owner == agent_uuid


# ============================================================================
# Test: enter_scope() operation
# ============================================================================

def test_enter_scope_adds_to_active_set(core):
    """enter_scope() should add scope to active set."""
    operator_uuid = core.entity.create("Operator")
    scope_uuid = core.entity.create("Artifact")  # Using Artifact as proxy for Scope

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter scope
    context_frame = core.context.enter_scope(context_frame, scope_uuid)

    # Verify scope in active set
    assert scope_uuid in context_frame.active_scopes
    assert len(context_frame.active_scopes) == 1


def test_enter_scope_first_scope_becomes_primary(core):
    """INV-11b: First scope entered should become primary automatically."""
    operator_uuid = core.entity.create("Operator")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter scope
    context_frame = core.context.enter_scope(context_frame, scope_uuid)

    # Verify first scope becomes primary
    assert context_frame.primary_scope == scope_uuid


def test_enter_scope_does_not_change_existing_primary(core):
    """INV-11a: enter should NOT change primary scope if already set."""
    operator_uuid = core.entity.create("Operator")
    scope1_uuid = core.entity.create("Artifact")
    scope2_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter first scope (becomes primary)
    context_frame = core.context.enter_scope(context_frame, scope1_uuid)
    assert context_frame.primary_scope == scope1_uuid

    # Enter second scope
    context_frame = core.context.enter_scope(context_frame, scope2_uuid)

    # Primary should still be first scope (INV-11a: Focus Separation)
    assert context_frame.primary_scope == scope1_uuid
    assert scope2_uuid in context_frame.active_scopes


def test_enter_scope_already_active_raises(core):
    """enter_scope() should raise if scope already in active set."""
    operator_uuid = core.entity.create("Operator")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter scope once
    context_frame = core.context.enter_scope(context_frame, scope_uuid)

    # Try to enter again - should raise
    with pytest.raises(Exception, match="already in active set"):
        core.context.enter_scope(context_frame, scope_uuid)


def test_enter_scope_for_agent_raises(core):
    """Only operators can have active scopes."""
    agent_uuid = core.entity.create("Agent")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=agent_uuid,
        owner_type="agent",
        create_if_missing=True
    )

    # Agent cannot enter scopes
    with pytest.raises(Exception, match="Only operators can have active scopes"):
        core.context.enter_scope(context_frame, scope_uuid)


# ============================================================================
# Test: leave_scope() operation
# ============================================================================

def test_leave_scope_removes_from_active_set(core):
    """leave_scope() should remove scope from active set."""
    operator_uuid = core.entity.create("Operator")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter scope
    context_frame = core.context.enter_scope(context_frame, scope_uuid)
    assert scope_uuid in context_frame.active_scopes

    # Leave scope
    context_frame = core.context.leave_scope(context_frame, scope_uuid)

    # Verify scope removed from active set
    assert scope_uuid not in context_frame.active_scopes
    assert len(context_frame.active_scopes) == 0


def test_leave_scope_clears_primary_if_leaving_primary(core):
    """Leaving primary scope should clear primary_scope."""
    operator_uuid = core.entity.create("Operator")
    scope1_uuid = core.entity.create("Artifact")
    scope2_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter scopes
    context_frame = core.context.enter_scope(context_frame, scope1_uuid)
    context_frame = core.context.enter_scope(context_frame, scope2_uuid)

    # Focus on scope2
    context_frame = core.context.focus_scope(context_frame, scope2_uuid)
    assert context_frame.primary_scope == scope2_uuid

    # Leave primary scope (scope2)
    context_frame = core.context.leave_scope(context_frame, scope2_uuid)

    # Primary should be cleared
    assert context_frame.primary_scope is None
    assert scope1_uuid in context_frame.active_scopes


def test_leave_scope_not_active_raises(core):
    """leave_scope() should raise if scope not in active set."""
    operator_uuid = core.entity.create("Operator")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Try to leave scope that hasn't been entered
    with pytest.raises(Exception, match="not in active set"):
        core.context.leave_scope(context_frame, scope_uuid)


# ============================================================================
# Test: focus_scope() operation
# ============================================================================

def test_focus_scope_switches_primary(core):
    """focus_scope() should switch primary scope among active scopes."""
    operator_uuid = core.entity.create("Operator")
    scope1_uuid = core.entity.create("Artifact")
    scope2_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Enter scopes
    context_frame = core.context.enter_scope(context_frame, scope1_uuid)
    context_frame = core.context.enter_scope(context_frame, scope2_uuid)

    # Focus on scope2
    context_frame = core.context.focus_scope(context_frame, scope2_uuid)

    # Verify primary changed
    assert context_frame.primary_scope == scope2_uuid

    # Focus on scope1
    context_frame = core.context.focus_scope(context_frame, scope1_uuid)

    # Verify primary changed again
    assert context_frame.primary_scope == scope1_uuid


def test_focus_scope_not_active_raises(core):
    """focus_scope() should raise if scope not in active set."""
    operator_uuid = core.entity.create("Operator")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=operator_uuid,
        owner_type="operator",
        create_if_missing=True
    )

    # Try to focus on scope that hasn't been entered
    with pytest.raises(Exception, match="not in active set"):
        core.context.focus_scope(context_frame, scope_uuid)


def test_focus_scope_for_agent_raises(core):
    """Only operators can focus scopes."""
    agent_uuid = core.entity.create("Agent")
    scope_uuid = core.entity.create("Artifact")

    context_frame = core.context.get_context_frame(
        owner=agent_uuid,
        owner_type="agent",
        create_if_missing=True
    )

    # Agent cannot focus scopes
    with pytest.raises(Exception, match="Only operators can have active scopes"):
        core.context.focus_scope(context_frame, scope_uuid)
