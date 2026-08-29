"""Tests for the workload health gate."""

from __future__ import annotations

import pytest

from tests.integration.helpers.assertions import (
    CR_HEALTH_MIN_API_VERSION,
    skip_unless_health_is_reported,
)


def _asserts_health(api_version) -> str:
    """Return the version the gate accepted, failing if it skipped instead.

    The gate signals "do not assert health" by raising pytest's skip exception.
    Calling it directly in a test would propagate that and skip the test too, so
    a gate that wrongly skips everything would leave this file green. Converting
    the skip into a failure is what makes these tests able to catch that.
    """
    try:
        return skip_unless_health_is_reported(api_version)
    except BaseException as exc:  # noqa: BLE001
        if type(exc).__name__ == "Skipped":
            pytest.fail(
                f"The gate skipped health assertions for {api_version!r}, so a "
                f"live run would report success without checking that any "
                f"dataflow works. Reason given: {exc}"
            )
        raise


def _skips(api_version) -> str:
    """Return the skip reason, failing if the gate did not skip."""
    try:
        accepted = skip_unless_health_is_reported(api_version)
    except BaseException as exc:  # noqa: BLE001
        if type(exc).__name__ == "Skipped":
            return str(exc)
        raise
    pytest.fail(f"The gate accepted {accepted!r}, expected it to skip.")


class TestTheGateBoundary:
    @pytest.mark.parametrize("api_version", ["2026-03-01", "2026-07-01"])
    def test_a_reporting_generation_asserts_health(self, api_version):
        assert _asserts_health(api_version) == api_version

    def test_an_older_generation_skips(self):
        """AIO 1.2 writes no health, so waiting for it would burn the budget."""
        assert "2025-10-01" in _skips("2025-10-01")

    def test_the_boundary_is_the_documented_one(self):
        """The constant is the contract, so a drift in it is visible here."""
        assert CR_HEALTH_MIN_API_VERSION == "2026-03-01"


class TestTheGateFailsLoudlyOnAnUnexpectedShape:
    @pytest.mark.parametrize("api_version", [None, "", {"value": "2026-07-01"}])
    def test_an_invalid_release_value_fails(self, api_version):
        with pytest.raises(AssertionError, match="aioApiVersion"):
            skip_unless_health_is_reported(api_version)
