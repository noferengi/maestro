"""Path normalization utilities for Windows/Unix compatibility."""

import os

def normalize_path(path: str) -> str:
    """
    Normalize a filesystem path for consistent handling across Maestro.
    - Resolves relative paths
    - Converts to OS-specific separators
    - Strips trailing slashes to prevent Git/Subprocess issues on Windows
    """
    if not path:
        return path
    # os.path.normpath handles separator consistency and .. resolution
    # rstrip(os.path.sep) ensures D:\path\ and D:\path are treated identically
    return os.path.normpath(path).rstrip(os.path.sep)
