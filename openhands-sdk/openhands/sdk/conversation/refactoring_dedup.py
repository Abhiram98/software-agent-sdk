"""Deduplication keys for refactorings detected by RefactoringMiner.

Provides stable identifiers for deduplicating refactorings across sliding
windows or repeated mining runs. RefactoringMiner may report the same
refactoring multiple times (e.g., in overlapping sliding windows); dedup
keys ensure we only nudge once per unique refactoring.

The dedup key strategy is configurable. Currently using description string
as the default, which is stable across line-number shifts and distinguishes
overloads correctly.
"""

from typing import Any


def dedup_key(refactoring: dict[str, Any]) -> str:
    """Return a stable key for deduplicating refactorings.

    Uses the RefactoringMiner description string, which includes:
    - Refactoring type (e.g., "Move Method")
    - Full method/field signature
    - Source and target class names

    This is stable across line-number shifts (important for sliding window
    re-detection) and distinguishes overloaded methods correctly.

    Args:
        refactoring: A RefactoringMiner JSON object with at minimum a
            "description" field.

    Returns:
        A string key suitable for set membership or dict lookup.
    """
    return refactoring.get("description", "")
