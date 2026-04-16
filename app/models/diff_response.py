"""
Pydantic models for diff endpoint response.

This module defines the data structures for representing git diff responses
in a structured, validated format.
"""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class DiffMethod(str, Enum):
    """Valid methods for computing git diff."""
    BRANCH = "branch"
    MERGE_COMMIT = "merge_commit"


class Hunk(BaseModel):
    """Represents a hunk in a file diff."""
    old_line: int = Field(..., description="Starting line number in the original file")
    new_line: int = Field(..., description="Starting line number in the modified file")
    content: str = Field(..., description="The diff content for this hunk")


class File(BaseModel):
    """Represents a file in a diff response."""
    path: str = Field(..., description="Path to the file that was changed")
    hunks: List[Hunk] = Field(..., description="List of hunks in the file diff")


class DiffResponse(BaseModel):
    """
    Response model for the diff endpoint.

    Contains structured information about git diff results including
    files changed, hunks, and optional error messages.
    """
    task_id: str = Field(..., description="Unique identifier for the task")
    branch: str = Field(..., description="Branch name being compared")
    method: DiffMethod = Field(..., description="Method used: 'branch' or 'merge_commit'")
    stat: Optional[str] = Field(None, description="Git stat output")
    files_changed: List[File] = Field(default_factory=list, description="List of changed files with hunks")
    truncated: bool = Field(default=False, description="Whether the diff was truncated")
    error: Optional[str] = Field(None, description="Error message if diff failed")

    class Config:
        """Pydantic configuration for the DiffResponse model."""
        json_schema_extra = {
            "example": {
                "task_id": "task-123",
                "branch": "feature-branch",
                "method": "branch",
                "stat": None,
                "files_changed": [
                    {
                        "path": "example.py",
                        "hunks": [
                            {
                                "old_line": 10,
                                "new_line": 15,
                                "content": "@@ -10,5 +15,7 @@\n line1\n line2\n line3\n line4\n line5\n new_line1\n new_line2"
                            }
                        ]
                    }
                ],
                "truncated": False,
                "error": None
            }
        }
