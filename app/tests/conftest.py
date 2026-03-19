"""
Test isolation: redirect all DB operations to a per-session temp file.

Setting MAESTRO_TEST_DB at module level (not inside a fixture) ensures it is
in place before pytest imports any test module.  Every test file defers its
`import database` to function scope, so when database.py is first imported
during the test run it reads the env var and builds the engine against the
temp file instead of data/kanban.db.
"""
import os
import sys
import tempfile
import pytest

# ---------------------------------------------------------------------------
# Point database.py at a throw-away SQLite file for this pytest session.
# Must happen at module level — before any test module triggers `import database`.
# ---------------------------------------------------------------------------
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["MAESTRO_TEST_DB"] = _tmp.name


@pytest.fixture(scope="session", autouse=True)
def _test_schema():
    """Create the full schema on the temp DB once, then tear it down."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import database
    database.Base.metadata.create_all(database.engine)
    yield
    try:
        os.unlink(_tmp.name)
    except OSError:
        pass
    os.environ.pop("MAESTRO_TEST_DB", None)
