"""Assertion helpers for integration tests."""

from typing import Any


def find_step(result: dict[str, Any], site_name: str, step_name: str) -> dict[str, Any]:
    """Find a step result by site and step name.

    Args:
        result: Full deployment result from orchestrator.deploy()
        site_name: Name of the site
        step_name: Name of the step

    Returns:
        Step result dict with keys: step, status, outputs, error, reason

    Raises:
        KeyError: If site not found in results
        ValueError: If step not found for the site
    """
    site_result = result["sites"][site_name]
    for step in site_result["steps"]:
        if step["step"] == step_name:
            return step
    available = [s["step"] for s in site_result["steps"]]
    raise ValueError(f"Step '{step_name}' not found for site '{site_name}'. Available: {available}")


def assert_step_succeeded(result: dict[str, Any], site_name: str, step_name: str) -> dict[str, Any]:
    """Assert a step succeeded and return its result for further assertions."""
    step = find_step(result, site_name, step_name)
    assert step["status"] == "success", (
        f"Step '{step_name}' did not succeed for site '{site_name}': "
        f"status={step['status']}, error={step.get('error')}"
    )
    return step


def assert_step_skipped(result: dict[str, Any], site_name: str, step_name: str) -> dict[str, Any]:
    """Assert a step was skipped and return its result."""
    step = find_step(result, site_name, step_name)
    assert step["status"] == "skipped", (
        f"Step '{step_name}' was not skipped for site '{site_name}': status={step['status']}"
    )
    return step


def assert_output_exists(step_result: dict[str, Any], output_name: str) -> Any:
    """Assert an output exists in a step result and return its value.

    Handles both raw values and Azure ARM wrapped format {"value": X, "type": "..."}.
    """
    outputs = step_result.get("outputs", {})
    assert output_name in outputs, (
        f"Output '{output_name}' not found in step '{step_result['step']}'. "
        f"Available: {sorted(outputs.keys())}"
    )
    output = outputs[output_name]
    if isinstance(output, dict) and "value" in output:
        return output["value"]
    return output


# AIO reports unified workload health from the generation below onward. A site
# on an older release deploys the same resources, and the catalog assertions
# above still hold, but nothing writes `status.healthState`, so waiting for it
# would spend the budget and then fail for the platform's age rather than for
# anything the declaration did.
CR_HEALTH_MIN_API_VERSION = "2026-03-01"


def skip_unless_health_is_reported(step_result: dict[str, Any]) -> str:
    """Skip the calling test when the deployed generation predates health reporting.

    Reads the generation the family entry point reports, so the decision
    follows what was actually deployed rather than what a release file names.

    Args:
        step_result: The result of the catalog family step.

    Returns:
        The API version that deployed, when it reports health.
    """
    import pytest

    # Read through the unwrapping helper. ARM returns an output as
    # `{"value": ..., "type": ...}`, so reading the mapping directly yields a
    # dict and every caller skips on a release that does report health.
    api_version = assert_output_exists(step_result, "apiVersion")
    if not isinstance(api_version, str) or not api_version:
        pytest.skip(
            f"The family step reported apiVersion as {api_version!r}, so whether "
            f"this release reports workload health cannot be determined."
        )
    if api_version < CR_HEALTH_MIN_API_VERSION:
        pytest.skip(
            f"AIO reports workload health from {CR_HEALTH_MIN_API_VERSION}, and "
            f"this deploy wrote at {api_version}."
        )
    return api_version


def assert_output_starts_with(
    step_result: dict[str, Any], output_name: str, prefix: str
) -> str:
    """Assert an output value starts with the given prefix."""
    value = assert_output_exists(step_result, output_name)
    assert isinstance(value, str) and value.startswith(prefix), (
        f"Output '{output_name}' expected to start with '{prefix}', got: {value}"
    )
    return value
