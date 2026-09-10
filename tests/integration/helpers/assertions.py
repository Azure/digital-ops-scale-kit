"""Assertion helpers for integration tests."""

from typing import Any

from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    RunResult,
    SiteResult,
)
from siteops.sanitize import is_redaction_enabled, site_name_for_output


def outcome_reason_for_output(reason: OutcomeReason) -> str:
    """Publish fixed reason text in CI and retain authorized local detail."""
    return reason.summary if is_redaction_enabled() else reason.local_message()


def site_results(result: RunResult) -> tuple[SiteResult, ...]:
    """Return nonempty typed site results."""
    assert result.sites, "Deployment returned no target results."
    return result.sites


def site_names(result: RunResult) -> tuple[str, ...]:
    """Return target names while preserving canonical result order."""
    return tuple(site.target for site in site_results(result))


def find_step(
    result: RunResult,
    site_name: str,
    step_name: str,
) -> OperationResult:
    """Find a step result by site and step name.

    Args:
        result: Full deployment result from orchestrator.deploy()
        site_name: Name of the site
        step_name: Name of the step

    Returns:
        Typed operation result.

    Raises:
        KeyError: If site not found in results
        ValueError: If step not found for the site
    """
    try:
        site_result = next(
            site
            for site in site_results(result)
            if site.target == site_name
        )
    except StopIteration as exc:
        available_sites = [
            site_name_for_output(site.target)
            for site in result.sites
        ]
        raise KeyError(
            f"Site '{site_name_for_output(site_name)}' not found. "
            f"Available: {available_sites}"
        ) from exc
    for operation in site_result.operations:
        if operation.identity.step == step_name:
            return operation
    available = [
        operation.identity.step
        for operation in site_result.operations
    ]
    raise ValueError(
        f"Step '{step_name}' not found for site "
        f"'{site_name_for_output(site_name)}'. Available: {available}"
    )


def assert_step_succeeded(
    result: RunResult,
    site_name: str,
    step_name: str,
) -> OperationResult:
    """Assert a step succeeded and return its result for further assertions."""
    step = find_step(result, site_name, step_name)
    reason = (
        outcome_reason_for_output(step.reason)
        if step.reason is not None
        else None
    )
    if step.status is not OperationStatus.SUCCEEDED:
        raise AssertionError(
            f"Step '{step_name}' did not succeed for site "
            f"'{site_name_for_output(site_name)}': "
            f"status={step.status.value}, reason={reason}"
        )
    return step


def assert_step_skipped(
    result: RunResult,
    site_name: str,
    step_name: str,
) -> OperationResult:
    """Assert a step was skipped and return its result."""
    step = find_step(result, site_name, step_name)
    assert step.status is OperationStatus.SKIPPED, (
        f"Step '{step_name}' was not skipped for site "
        f"'{site_name_for_output(site_name)}': status={step.status.value}"
    )
    return step


def assert_output_exists(
    step_result: OperationResult,
    output_name: str,
) -> Any:
    """Assert an output exists in a step result and return its value.

    Handles both raw values and Azure ARM wrapped format {"value": X, "type": "..."}.
    """
    outputs = step_result.copy_outputs()
    assert output_name in outputs, (
        f"Output '{output_name}' not found in step "
        f"'{step_result.identity.step}'. "
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


def skip_unless_health_is_reported(api_version: str) -> str:
    """Skip the calling test when the deployed generation predates health reporting.

    Args:
        api_version: AIO API version from the target site's release file.

    Returns:
        The API version that deployed, when it reports health.
    """
    import pytest

    if not isinstance(api_version, str) or not api_version:
        raise AssertionError(
            f"The target release declares aioApiVersion as {api_version!r}, "
            "so whether it reports workload health cannot be determined."
        )
    if api_version < CR_HEALTH_MIN_API_VERSION:
        pytest.skip(
            f"AIO reports workload health from {CR_HEALTH_MIN_API_VERSION}, and "
            f"this deploy wrote at {api_version}."
        )
    return api_version


def assert_output_starts_with(
    step_result: OperationResult,
    output_name: str,
    prefix: str,
) -> str:
    """Assert an output value starts with the given prefix."""
    value = assert_output_exists(step_result, output_name)
    assert isinstance(value, str) and value.startswith(prefix), (
        f"Output '{output_name}' expected to start with '{prefix}', got: {value}"
    )
    return value
