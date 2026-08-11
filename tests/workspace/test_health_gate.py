"""Tests for the workload health gate.

The gate decides whether a live run asserts dataflow health. Getting it wrong
in the permissive direction fails a run for the platform's age. Getting it wrong
in the restrictive direction is worse and quieter: every health assertion skips,
the run reports success, and nothing has checked that a deployed dataflow works.

The second is what happened on the first live run, because ARM returns a
deployment output as `{"value": ..., "type": ...}` and the gate read the mapping
directly. These tests pin both the shape and the boundary.
"""

from __future__ import annotations

import pytest

from tests.integration.helpers.assertions import (
    CR_HEALTH_MIN_API_VERSION,
    skip_unless_health_is_reported,
)


def _step(api_version, *, wrapped: bool = True):
    """A family step result carrying `apiVersion`, in ARM's shape by default."""
    value = {"value": api_version, "type": "String"} if wrapped else api_version
    return {"step": "dataflow-resources", "outputs": {"apiVersion": value}}


def _asserts_health(step) -> str:
    """Return the version the gate accepted, failing if it skipped instead.

    The gate signals "do not assert health" by raising pytest's skip exception.
    Calling it directly in a test would propagate that and skip the test too, so
    a gate that wrongly skips everything would leave this file green. Converting
    the skip into a failure is what makes these tests able to catch that.
    """
    try:
        return skip_unless_health_is_reported(step)
    except BaseException as exc:  # noqa: BLE001
        if type(exc).__name__ == "Skipped":
            pytest.fail(
                f"The gate skipped health assertions for {step['outputs']}, so a "
                f"live run would report success without checking that any "
                f"dataflow works. Reason given: {exc}"
            )
        raise


def _skips(step) -> str:
    """Return the skip reason, failing if the gate did not skip."""
    try:
        accepted = skip_unless_health_is_reported(step)
    except BaseException as exc:  # noqa: BLE001
        if type(exc).__name__ == "Skipped":
            return str(exc)
        raise
    pytest.fail(f"The gate accepted {accepted!r}, expected it to skip.")


class TestTheGateReadsArmOutputShape:
    def test_a_wrapped_output_is_unwrapped(self):
        """ARM wraps an output, which is the shape a live deploy produces."""
        assert _asserts_health(_step("2026-07-01")) == "2026-07-01"

    def test_a_raw_output_still_works(self):
        """A raw string is accepted too, so the gate does not depend on the wrapper."""
        assert _asserts_health(_step("2026-07-01", wrapped=False)) == "2026-07-01"

    def test_the_generation_that_ships_today_asserts_health(self):
        """The release a live run uses has to reach the assertions, not skip them.

        This is the case the first live run got wrong. Every health assertion
        skipped and the run still reported success.
        """
        assert _asserts_health(_step("2026-07-01"))


class TestTheGateBoundary:
    @pytest.mark.parametrize("api_version", ["2026-03-01", "2026-07-01"])
    def test_a_reporting_generation_asserts_health(self, api_version):
        assert _asserts_health(_step(api_version)) == api_version

    def test_an_older_generation_skips(self):
        """AIO 1.2 writes no health, so waiting for it would burn the budget."""
        assert "2025-10-01" in _skips(_step("2025-10-01"))

    def test_the_boundary_is_the_documented_one(self):
        """The constant is the contract, so a drift in it is visible here."""
        assert CR_HEALTH_MIN_API_VERSION == "2026-03-01"


class TestTheGateFailsLoudlyOnAnUnexpectedShape:
    def test_a_missing_output_names_what_was_available(self):
        """An absent output is a defect in the template, not a reason to skip."""
        with pytest.raises(AssertionError) as excinfo:
            skip_unless_health_is_reported({"step": "dataflow-resources", "outputs": {}})
        assert "apiVersion" in str(excinfo.value)

    def test_a_non_string_value_reports_what_it_saw(self):
        """A skip has to name the value, so a silent skip cannot be mistaken for a pass."""
        assert "None" in _skips(_step(None))
