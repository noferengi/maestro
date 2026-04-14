"""
app/tests/test_llm_client_resilience.py
---------------------------------------
Unit tests for LLM client resilience under timeout conditions.
Verifies that ReadTimeout on one request doesn't block other requests
for 15 minutes.
"""

import logging
import pytest
import httpx
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from app.agent.llm_client import call_llm, _endpoint_states, _BACKOFF_MAX_DELAY


@pytest.fixture(autouse=True)
def _reset_endpoint_states():
    import app.agent.llm_client as lc
    lc._endpoint_states.clear()
    yield
    lc._endpoint_states.clear()


@pytest.fixture(autouse=True)
def _restore_llm_client_log_level():
    logger = logging.getLogger("app.agent.llm_client")
    original = logger.level
    yield
    logger.setLevel(original)


@pytest.mark.asyncio
async def test_read_timeout_backoff_isolation():
    """
    Test that a series of ReadTimeouts increments fail_count_response and grows
    the backoff delay, capped at _BACKOFF_RESPONSE_MAX_DELAY (60s), NOT at the
    connect-error cap of 900s.

    Infinite-loop prevention: time.monotonic is advanced by 1000 seconds on each
    call.  The backoff gate sets next_allowed = monotonic() + 3; the next call to
    monotonic() returns a value 1000 seconds later, so next_allowed is always in
    the past when rechecked — the while loop cannot spin.

    asyncio.sleep is mocked as AsyncMock so await returns immediately without
    actually suspending.
    """
    # Silence llm_client WARNING logs: 45+ multi-line messages routed through
    # pytest's log-capture handler interact badly with run_in_executor threads.
    logging.getLogger("app.agent.llm_client").setLevel(logging.CRITICAL)

    url = "http://localhost:1234/v1"
    _endpoint_states.pop(url, None)

    # 50 ReadTimeouts — more than enough for 15 calls × max_retries=2 attempts each.
    mock_responses = [httpx.ReadTimeout("Simulated timeout")] * 50

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=mock_responses)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    mock_sem = MagicMock()

    # Advance the clock by 1000 seconds on every call so any next_allowed
    # value set by the backoff handler is always in the past when rechecked.
    _clock = [0.0]
    def _advancing_clock():
        _clock[0] += 1000.0
        return _clock[0]

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("app.agent.llm_client._get_llm_max_context", return_value=100000), \
         patch("app.agent.llm_client._log_budget_entry"), \
         patch("app.agent.llm_client._get_or_create_semaphore", return_value=mock_sem), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("time.monotonic", side_effect=_advancing_clock):

        for i in range(15):
            try:
                await call_llm(
                    [{"role": "user", "content": f"test {i}"}],
                    base_url=url,
                    llm_id=1,
                    budget_id=1,
                    max_retries=2,
                    timeout=1,
                    stream=False,
                )
            except Exception:
                pass

    st = _endpoint_states.get(url)
    assert st is not None, (
        f"Endpoint {url} not found in _endpoint_states: {list(_endpoint_states.keys())}"
    )

    # Every call makes 2 HTTP attempts (initial + 1 retry before max_retries fires).
    # 15 outer iterations × 2 = 30 attempts minimum.
    assert st.fail_count_response >= 15
    assert st.fail_count_connect == 0

    # Backoff delay must be capped at 60s (response cap), not 900s (connect cap).
    assert st.delay <= _BACKOFF_MAX_DELAY  # sanity: never exceeds the 900s connect cap
    assert st.delay <= 60.0, (
        f"Response backoff delay {st.delay}s exceeds the 60s cap (_BACKOFF_RESPONSE_MAX_DELAY)"
    )
    assert st.delay > 3.0, "Delay should have grown beyond the base 3s after many failures"
