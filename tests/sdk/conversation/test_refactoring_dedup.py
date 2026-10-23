"""Tests for refactoring deduplication."""

from openhands.sdk.conversation.refactoring_dedup import dedup_key


def test_dedup_key_uses_description():
    """Dedup key is the refactoring description string."""
    refactoring = {
        "type": "Move Method",
        "description": "Move Method public convert() from A to B",
    }
    assert dedup_key(refactoring) == "Move Method public convert() from A to B"


def test_dedup_key_handles_missing_description():
    """Missing description returns empty string."""
    refactoring = {"type": "Move Method"}
    assert dedup_key(refactoring) == ""


def test_dedup_key_stable_across_line_shifts():
    """Same description with different line numbers produces same key."""
    r1 = {
        "type": "Move Method",
        "description": "Move Method public convert() from A to B",
        "leftSideLocations": [{"startLine": 10}],
    }
    r2 = {
        "type": "Move Method",
        "description": "Move Method public convert() from A to B",
        "leftSideLocations": [{"startLine": 15}],
    }
    assert dedup_key(r1) == dedup_key(r2)


def test_dedup_key_distinguishes_overloads():
    """Different method signatures produce different keys."""
    r1 = {
        "type": "Move Method",
        "description": "Move Method public convert(String) from A to B",
    }
    r2 = {
        "type": "Move Method",
        "description": "Move Method public convert(int) from A to B",
    }
    assert dedup_key(r1) != dedup_key(r2)
