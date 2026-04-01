"""Utility functions for deduplication."""

from typing import List, TypeVar

T = TypeVar("T")


def deduplicate_sorted(items: List[T]) -> List[T]:
    """Remove duplicates from a sorted list while preserving order.

    This function scans adjacent elements in a sorted list and removes
    duplicates in O(n) time. It returns a new list without mutating the input.

    Args:
        items: A list of comparable items that is guaranteed to be sorted.

    Returns:
        A new list with duplicates removed, preserving the original sorted order.

    Examples:
        >>> deduplicate_sorted([1, 1, 2, 2, 3])
        [1, 2, 3]
        >>> deduplicate_sorted([])
        []
        >>> deduplicate_sorted([5])
        [5]
        >>> deduplicate_sorted(["a", "a", "b", "c", "c"])
        ["a", "b", "c"]
    """
    if not items:
        return []

    result: List[T] = [items[0]]
    for item in items[1:]:
        if item != result[-1]:
            result.append(item)

    return result
