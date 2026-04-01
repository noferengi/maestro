"""Tests for the deduplicate_sorted utility function."""

from app.utils.deduplicate import deduplicate_sorted


class TestDeduplicateSorted:
    """Test cases for deduplicate_sorted function."""

    def test_empty_list(self):
        """Test that empty list returns empty list."""
        result = deduplicate_sorted([])
        assert result == []
        assert result is not []  # Ensure it's a new list object, not the same singleton

    def test_single_element(self):
        """Test that single element list returns same element."""
        result = deduplicate_sorted([5])
        assert result == [5]

    def test_no_duplicates(self):
        """Test list with no duplicates."""
        result = deduplicate_sorted([1, 2, 3, 4, 5])
        assert result == [1, 2, 3, 4, 5]

    def test_all_duplicates(self):
        """Test list with all same elements."""
        result = deduplicate_sorted([1, 1, 1, 1, 1])
        assert result == [1]

    def test_adjacent_duplicates(self):
        """Test list with adjacent duplicates."""
        result = deduplicate_sorted([1, 1, 2, 2, 3, 3, 3])
        assert result == [1, 2, 3]

    def test_alternating_duplicates(self):
        """Test list with alternating duplicates (input must be sorted first)."""
        result = deduplicate_sorted(sorted([1, 2, 1, 2, 1, 2]))
        assert result == [1, 2]

    def test_strings(self):
        """Test with string elements."""
        result = deduplicate_sorted(["a", "a", "b", "c", "c", "d"])
        assert result == ["a", "b", "c", "d"]

    def test_preserves_order(self):
        """Test that original sorted order is preserved."""
        result = deduplicate_sorted([1, 1, 2, 2, 3, 3])
        assert result == [1, 2, 3]

    def test_returns_new_list(self):
        """Test that a new list is returned, not the original."""
        original = [1, 1, 2, 2, 3]
        result = deduplicate_sorted(original)
        assert result is not original
        assert result != original

    def test_does_not_mutate_input(self):
        """Test that the input list is not mutated."""
        original = [1, 1, 2, 2, 3]
        original_copy = original.copy()
        deduplicate_sorted(original)
        assert original == original_copy

    def test_large_list(self):
        """Test with a larger sorted list."""
        items = sorted(list(range(100)) * 2)  # [0, 0, 1, 1, ..., 99, 99]
        result = deduplicate_sorted(items)
        assert result == list(range(100))
        assert len(result) == 100

    def test_negative_numbers(self):
        """Test with negative numbers."""
        result = deduplicate_sorted([-3, -2, -2, -1, 0, 0, 1, 1])
        assert result == [-3, -2, -1, 0, 1]

    def test_floats(self):
        """Test with float elements."""
        result = deduplicate_sorted([1.5, 1.5, 2.5, 3.0, 3.0])
        assert result == [1.5, 2.5, 3.0]
