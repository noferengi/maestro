"""
Git diff parser utility module.

This module provides functionality for parsing git diff output into structured
data including file paths, hunks, and content with context line filtering.
"""

import re
from typing import List, Optional, Tuple

from app.models.diff_response import DiffResponse, File, Hunk


def parse_hunk_header(header_line: str) -> Tuple[int, int, int, int]:
    """
    Parse a unified diff hunk header line to extract line counts.

    Args:
        header_line: A hunk header line like "@@ -10,5 +15,7 @@"

    Returns:
        Tuple of (old_start, old_count, new_start, new_count)

    Raises:
        ValueError: If the header line format is invalid
    """
    # Pattern to match unified diff hunk header
    # Format: @@ -old_start,old_count +new_start,new_count @@
    pattern = r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@'

    match = re.match(pattern, header_line.strip())
    if not match:
        raise ValueError(f"Invalid hunk header format: {header_line}")

    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) else 1

    return old_start, old_count, new_start, new_count


def parse_git_diff(raw_diff: str, include_context: int = 3) -> DiffResponse:
    """
    Parse a raw git diff string into structured DiffResponse data.

    Args:
        raw_diff: The raw git diff output string
        include_context: Maximum number of context lines to include per hunk

    Returns:
        DiffResponse object containing parsed diff data

    Raises:
        ValueError: If the diff format is invalid
    """
    if not raw_diff or not raw_diff.strip():
        raise ValueError("Empty diff string provided")

    # Split by diff --git boundaries to extract individual file diffs
    file_diffs = re.split(r'^diff --git ', raw_diff, flags=re.MULTILINE)

    files: List[File] = []

    for file_diff in file_diffs:
        if not file_diff.strip():
            continue

        # Extract file path from the first line
        path_match = re.match(r'^a/([^ ]+)$', file_diff, re.MULTILINE)
        if not path_match:
            continue

        file_path = path_match.group(1)

        # Extract hunks from this file diff
        hunks = []
        lines = file_diff.split('\n')

        i = 0
        while i < len(lines):
            line = lines[i]

            # Check if this is a hunk header
            if line.startswith('@@'):
                try:
                    old_start, old_count, new_start, new_count = parse_hunk_header(line)
                except ValueError:
                    # Skip invalid hunk headers
                    i += 1
                    continue

                # Extract hunk content
                hunk_content_lines = [line]
                context_count = 0

                i += 1
                while i < len(lines):
                    current_line = lines[i]

                    # Check if we've hit another hunk header or end of file
                    if current_line.startswith('@@') or current_line.startswith('diff ') or current_line.startswith('---') or current_line.startswith('+++'):
                        break

                    # Count context lines (lines without + or - prefix, except @@)
                    if current_line.startswith(' ') or current_line.startswith('\\ No newline'):
                        context_count += 1
                        hunk_content_lines.append(current_line)

                        # Stop if we've exceeded context limit
                        if context_count >= include_context:
                            break

                    i += 1

                hunks.append(Hunk(
                    old_line=old_start,
                    new_line=new_start,
                    content='\n'.join(hunk_content_lines)
                ))

            i += 1

        if hunks:
            files.append(File(
                path=file_path,
                hunks=hunks
            ))

    return DiffResponse(
        task_id="",
        branch="",
        method="",
        files_changed=files
    )
