# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for the `type: wait` step.

Covers the three layers:
- Model parse + validation (siteops.models)
- The executor poll loop, error taxonomy, and dry-run (siteops.executor)
- Orchestrator dispatch, template resolution, runtime guard, validation, preview
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from siteops.executor import (
    DEFAULT_WAIT_POLL_AZ_TIMEOUT_SECONDS,
    WAIT_MAX_CONSECUTIVE_ERRORS,
    AzCliExecutor,
    WaitResult,
    WaitState,
    _classify_az_error,
)
from siteops.models import (
    ArmTagCondition,
    Site,
    WaitStep,
    _parse_inline_step,
)
from siteops.orchestrator import Orchestrator

ARC_ID = "/subscriptions/s/resourceGroups/r/providers/Microsoft.HybridCompute/machines/m"


def _wait_step_data(**overrides):
    """Build a minimal valid `type: wait` step dict, with overrides."""
    data = {
        "name": "wait-bootstrap",
        "type": "wait",
        "condition": {
            "type": "arm-tag",
            "resourceId": ARC_ID,
            "tagKey": "siteops.bootstrap.state",
            "expectedValue": "succeeded",
            "failurePattern": "failed-*",
        },
        "timeoutMinutes": 45,
        "pollIntervalSeconds": 30,
    }
    data.update(overrides)
    return data


def _condition(**overrides) -> ArmTagCondition:
    params = {
        "type": "arm-tag",
        "resource_id": ARC_ID,
        "tag_key": "siteops.bootstrap.state",
        "expected_value": "succeeded",
        "failure_pattern": "failed-*",
    }
    params.update(overrides)
    return ArmTagCondition(**params)


def _tags_json(value):
    """Mocked `az resource show --query tags` stdout for one tag."""
    import json

    return json.dumps({"siteops.bootstrap.state": value})


# ---------------------------------------------------------------------------
# Model: parse + validation
# ---------------------------------------------------------------------------


class TestWaitStepParse:
    """Parsing a `type: wait` step into a WaitStep."""

    def test_parse_happy_path(self):
        step = _parse_inline_step(_wait_step_data(), Path("m.yaml"), 0)
        assert isinstance(step, WaitStep)
        assert step.timeout_minutes == 45
        assert step.poll_interval_seconds == 30
        assert step.condition.type == "arm-tag"
        assert step.condition.resource_id == ARC_ID
        assert step.condition.expected_value == "succeeded"
        assert step.condition.failure_pattern == "failed-*"

    def test_parse_defaults(self):
        data = _wait_step_data()
        del data["timeoutMinutes"]
        del data["pollIntervalSeconds"]
        del data["condition"]["failurePattern"]
        step = _parse_inline_step(data, Path("m.yaml"), 0)
        assert step.timeout_minutes == 30
        assert step.poll_interval_seconds == 30
        assert step.condition.failure_pattern is None

    def test_parse_coerces_expected_value_to_str(self):
        data = _wait_step_data()
        data["condition"]["expectedValue"] = True  # YAML bool
        data["condition"]["failurePattern"] = None
        step = _parse_inline_step(data, Path("m.yaml"), 0)
        assert step.condition.expected_value == "True"

    def test_missing_condition_errors(self):
        data = _wait_step_data()
        del data["condition"]
        with pytest.raises(ValueError, match="requires a 'condition' mapping"):
            _parse_inline_step(data, Path("m.yaml"), 0)

    def test_condition_not_mapping_errors(self):
        data = _wait_step_data()
        data["condition"] = "arm-tag"
        with pytest.raises(ValueError, match="requires a 'condition' mapping"):
            _parse_inline_step(data, Path("m.yaml"), 0)

    def test_missing_condition_type_errors(self):
        data = _wait_step_data()
        del data["condition"]["type"]
        with pytest.raises(ValueError, match="condition requires a 'type' field"):
            _parse_inline_step(data, Path("m.yaml"), 0)

    def test_unknown_condition_type_errors(self):
        data = _wait_step_data()
        data["condition"]["type"] = "bogus"
        with pytest.raises(ValueError, match="unknown condition type 'bogus'"):
            _parse_inline_step(data, Path("m.yaml"), 0)

    @pytest.mark.parametrize("missing", ["resourceId", "tagKey", "expectedValue"])
    def test_missing_required_condition_field_errors(self, missing):
        data = _wait_step_data()
        del data["condition"][missing]
        with pytest.raises(ValueError, match=f"missing '{missing}'"):
            _parse_inline_step(data, Path("m.yaml"), 0)

    def test_non_integer_timer_errors(self):
        data = _wait_step_data(timeoutMinutes="not-a-number")
        with pytest.raises(ValueError, match="must be an integer"):
            _parse_inline_step(data, Path("m.yaml"), 0)


class TestArmTagConditionValidation:
    """ArmTagCondition post-init validation."""

    @pytest.mark.parametrize("field", ["resource_id", "tag_key", "expected_value"])
    def test_empty_field_rejected(self, field):
        with pytest.raises(ValueError, match="non-empty"):
            _condition(**{field: "  "})

    def test_expected_value_matching_failure_glob_rejected(self):
        with pytest.raises(ValueError, match="also matches failurePattern"):
            _condition(expected_value="succeeded", failure_pattern="succ*")

    def test_no_failure_pattern_is_valid(self):
        cond = _condition(failure_pattern=None)
        assert cond.failure_pattern is None


class TestWaitStepValidation:
    """WaitStep post-init validation of timers and `when`."""

    def test_zero_timeout_rejected(self):
        with pytest.raises(ValueError, match="timeoutMinutes must be positive"):
            WaitStep(name="w", condition=_condition(), timeout_minutes=0)

    def test_negative_poll_rejected(self):
        with pytest.raises(ValueError, match="pollIntervalSeconds must be positive"):
            WaitStep(name="w", condition=_condition(), poll_interval_seconds=-1)

    def test_poll_greater_than_timeout_rejected(self):
        with pytest.raises(ValueError, match="exceeds timeoutMinutes"):
            WaitStep(name="w", condition=_condition(), timeout_minutes=1, poll_interval_seconds=120)

    def test_invalid_when_rejected(self):
        with pytest.raises(ValueError, match="Invalid 'when' condition syntax"):
            WaitStep(name="w", condition=_condition(), when="not a template")

    def test_valid_when_accepted(self):
        step = WaitStep(
            name="w",
            condition=_condition(),
            when="{{ site.labels.environment == 'prod' }}",
        )
        assert step.when is not None


# ---------------------------------------------------------------------------
# Executor: error classification
# ---------------------------------------------------------------------------


class TestClassifyAzError:
    @pytest.mark.parametrize(
        "stderr,expected",
        [
            ("ERROR: (ResourceNotFound) The Resource was not found", "resource_not_found"),
            ("ERROR: status code: 404 Not Found", "resource_not_found"),
            ("AuthorizationFailed: does not have authorization", "permanent"),
            ("ERROR: status code: 403 Forbidden", "permanent"),
            ("Please run 'az login' to setup account", "permanent"),
            ("SubscriptionNotFound: subscription was not recognized", "resource_not_found"),
            ("ERROR: status code: 429 TooManyRequests", "transient"),
            ("ERROR: status code: 503 ServiceUnavailable", "transient"),
            ("Command timed out after 60s", "transient"),
            ("connection reset by peer", "transient"),
            ("some unrecognized failure", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_classification(self, stderr, expected):
        assert _classify_az_error(stderr) == expected


# ---------------------------------------------------------------------------
# Executor: single observation (_evaluate_arm_tag)
# ---------------------------------------------------------------------------


class TestEvaluateArmTag:
    def _executor(self, tmp_workspace):
        return AzCliExecutor(workspace=tmp_workspace, dry_run=False)

    def test_satisfied(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("succeeded"), "")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.SATISFIED
        assert observed == "succeeded"
        assert error is None

    def test_failure_glob(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("failed-phase-2"), "")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.FAILED
        assert observed == "failed-phase-2"
        assert "failurePattern" in error

    def test_intermediate_value_is_pending(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("running"), "")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.PENDING
        assert observed == "running"
        assert error is None

    def test_tag_absent_is_pending(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, "{}", "")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.PENDING
        assert observed is None

    def test_permanent_error_fails(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(False, "", "AuthorizationFailed")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.FAILED
        assert "permanent error" in error

    def test_resource_not_found_fails(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(False, "", "(ResourceNotFound) was not found")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.FAILED
        assert "resource not found" in error

    def test_transient_error_is_pending_with_error(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(False, "", "status code: 429 TooManyRequests")):
            state, observed, error = ex._evaluate_arm_tag(_condition(), "sub")
        assert state == WaitState.PENDING
        assert error is not None

    def test_satisfied_before_failure_glob(self, tmp_workspace):
        # expectedValue exact match wins even if a loose failure glob could match.
        ex = self._executor(tmp_workspace)
        cond = _condition(expected_value="done", failure_pattern="d*g")  # no overlap at construct
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("done"), "")):
            state, _observed, _error = ex._evaluate_arm_tag(cond, "sub")
        assert state == WaitState.SATISFIED

    def test_subscription_passed_inline(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("succeeded"), "")) as mock_az:
            ex._evaluate_arm_tag(_condition(), "my-sub")
        args = mock_az.call_args[0][0]
        assert "--subscription" in args
        assert "my-sub" in args
        # short per-poll timeout, not the 3600s deploy default
        assert mock_az.call_args.kwargs.get("timeout") == DEFAULT_WAIT_POLL_AZ_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Executor: poll loop (wait_for_condition)
# ---------------------------------------------------------------------------


class TestWaitForCondition:
    def _executor(self, tmp_workspace, dry_run=False):
        return AzCliExecutor(workspace=tmp_workspace, dry_run=dry_run)

    def test_satisfied_first_poll(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("succeeded"), "")):
            result = ex.wait_for_condition(_condition(), 1, 1, "sub", "step", "site")
        assert isinstance(result, WaitResult)
        assert result.success is True
        assert result.error is None

    def test_satisfied_after_pending(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        responses = [
            (True, _tags_json("running"), ""),
            (True, _tags_json("running"), ""),
            (True, _tags_json("succeeded"), ""),
        ]
        with patch.object(ex, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = ex.wait_for_condition(_condition(), 600, 1, "sub", "step", "site")
        assert result.success is True

    def test_failure_glob_fails_fast(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(True, _tags_json("failed-phase-3"), "")):
            with patch("siteops.executor.time.sleep") as mock_sleep:
                result = ex.wait_for_condition(_condition(), 45, 30, "sub", "step", "site")
        assert result.success is False
        assert "failed-phase-3" in result.error
        mock_sleep.assert_not_called()

    def test_permanent_error_fails_fast(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(False, "", "AuthorizationFailed: 403")):
            with patch("siteops.executor.time.sleep") as mock_sleep:
                result = ex.wait_for_condition(_condition(), 45, 30, "sub", "step", "site")
        assert result.success is False
        mock_sleep.assert_not_called()

    def test_timeout_carries_last_value_and_poll_count(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        # monotonic: start, then a value past the deadline on the remaining check.
        clock = iter([0.0, 0.0, 0.0, 10_000.0, 10_000.0])

        def fake_monotonic():
            try:
                return next(clock)
            except StopIteration:
                return 10_000.0

        with patch.object(ex, "_run_az", return_value=(True, _tags_json("running"), "")):
            with patch("siteops.executor.time.monotonic", side_effect=fake_monotonic):
                with patch("siteops.executor.time.sleep"):
                    result = ex.wait_for_condition(_condition(), 1, 30, "sub", "step", "site")
        assert result.success is False
        assert "timed out" in result.error
        assert "running" in result.error
        assert "Polls:" in result.error

    def test_circuit_breaker_on_consecutive_errors(self, tmp_workspace):
        ex = self._executor(tmp_workspace)
        with patch.object(ex, "_run_az", return_value=(False, "", "status code: 503")):
            with patch("siteops.executor.time.sleep"):
                result = ex.wait_for_condition(_condition(), 600, 1, "sub", "step", "site")
        assert result.success is False
        assert "consecutive" in result.error

    def test_transient_errors_reset_on_success(self, tmp_workspace):
        # A run of transient errors shorter than the breaker, then satisfied.
        ex = self._executor(tmp_workspace)
        responses = [(False, "", "status code: 503")] * (WAIT_MAX_CONSECUTIVE_ERRORS - 1)
        responses.append((True, _tags_json("succeeded"), ""))
        with patch.object(ex, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = ex.wait_for_condition(_condition(), 600, 1, "sub", "step", "site")
        assert result.success is True

    def test_dry_run_never_polls(self, tmp_workspace):
        ex = self._executor(tmp_workspace, dry_run=True)
        with patch.object(ex, "_run_az", side_effect=AssertionError("must not poll in dry-run")):
            result = ex.wait_for_condition(_condition(), 45, 30, "sub", "step", "site")
        assert result.success is True
        assert result.error is None


# ---------------------------------------------------------------------------
# Orchestrator: dispatch, resolution, runtime guard, validation, preview
# ---------------------------------------------------------------------------


def _site(**overrides) -> Site:
    params = {
        "name": "munich-dev",
        "subscription": "00000000-0000-0000-0000-000000000000",
        "resource_group": "rg-test",
        "location": "westus2",
        "labels": {"environment": "dev"},
        "properties": {},
        "parameters": {"aksee": {"machineName": "arc-vm-1"}},
    }
    params.update(overrides)
    return Site(**params)


class TestExecuteWaitStep:
    def test_resolves_templates_and_runs(self, tmp_workspace):
        orch = Orchestrator(tmp_workspace)
        step = WaitStep(
            name="wait-bs",
            condition=ArmTagCondition(
                type="arm-tag",
                resource_id="/subscriptions/{{ site.subscription }}/resourceGroups/{{ site.resourceGroup }}/providers/Microsoft.HybridCompute/machines/{{ site.parameters.aksee.machineName }}",
                tag_key="siteops.bootstrap.state",
                expected_value="succeeded",
                failure_pattern="failed-*",
            ),
        )
        captured = {}

        def fake_wait(condition, **kwargs):
            captured["resource_id"] = condition.resource_id
            captured["subscription"] = kwargs["subscription"]
            return WaitResult(success=True, step_name="wait-bs", site_name="munich-dev")

        with patch.object(orch.executor, "wait_for_condition", side_effect=fake_wait):
            result = orch._execute_wait_step(_site(), step, {})
        assert result.success is True
        assert captured["resource_id"] == (
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/"
            "providers/Microsoft.HybridCompute/machines/arc-vm-1"
        )
        assert captured["subscription"] == "00000000-0000-0000-0000-000000000000"

    def test_resolves_step_outputs(self, tmp_workspace):
        orch = Orchestrator(tmp_workspace)
        step = WaitStep(
            name="wait-bs",
            condition=ArmTagCondition(
                type="arm-tag",
                resource_id="{{ steps.bootstrap.outputs.machineId.value }}",
                tag_key="siteops.bootstrap.state",
                expected_value="succeeded",
            ),
        )
        step_outputs = {"bootstrap": {"machineId": {"value": ARC_ID}}}
        captured = {}

        def fake_wait(condition, **kwargs):
            captured["resource_id"] = condition.resource_id
            return WaitResult(success=True, step_name="wait-bs", site_name="munich-dev")

        with patch.object(orch.executor, "wait_for_condition", side_effect=fake_wait):
            orch._execute_wait_step(_site(), step, step_outputs)
        assert captured["resource_id"] == ARC_ID

    def test_unresolved_template_fails_fast(self, tmp_workspace):
        orch = Orchestrator(tmp_workspace)
        step = WaitStep(
            name="wait-bs",
            condition=ArmTagCondition(
                type="arm-tag",
                resource_id="/subscriptions/{{ site.parameters.missing.path }}/x",
                tag_key="k",
                expected_value="succeeded",
            ),
        )
        with patch.object(
            orch.executor, "wait_for_condition", side_effect=AssertionError("must not poll")
        ):
            result = orch._execute_wait_step(_site(), step, {})
        assert result.success is False
        assert "unresolved or empty resourceId" in result.error


class TestWaitStepDispatch:
    def test_compatibility_runs_on_any_site(self, tmp_workspace):
        orch = Orchestrator(tmp_workspace)
        step = WaitStep(name="w", condition=_condition())
        assert orch._check_step_site_compatibility(step, _site()) is None

    def test_type_label(self, tmp_workspace):
        orch = Orchestrator(tmp_workspace)
        step = WaitStep(name="w", condition=_condition())
        assert orch._get_step_type_label(step) == "wait"


class TestWaitStepManifestValidation:
    """Output-reference validation for wait conditions via validate_manifest."""

    def _write_manifest(self, tmp_workspace, steps):
        import yaml

        # A site so the manifest has targeting.
        site = {
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "munich-dev",
            "subscription": "00000000-0000-0000-0000-000000000000",
            "resourceGroup": "rg-test",
            "location": "westus2",
            "labels": {"environment": "dev"},
        }
        (tmp_workspace / "sites" / "munich-dev.yaml").write_text(yaml.dump(site), encoding="utf-8")

        manifest = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "test",
            "description": "test",
            "siteSelector": "environment=dev",
            "steps": steps,
        }
        path = tmp_workspace / "manifests" / "test.yaml"
        path.write_text(yaml.dump(manifest), encoding="utf-8")
        return path

    def test_reference_to_unknown_step_errors(self, tmp_workspace):
        steps = [
            {
                "name": "wait-bs",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "{{ steps.nonexistent.outputs.id.value }}",
                    "tagKey": "k",
                    "expectedValue": "succeeded",
                },
            }
        ]
        path = self._write_manifest(tmp_workspace, steps)
        orch = Orchestrator(tmp_workspace)
        errors = orch.validate(path)
        assert any("references unknown step 'nonexistent'" in e for e in errors)

    def test_reference_to_later_step_errors(self, tmp_workspace):
        steps = [
            {
                "name": "wait-bs",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "{{ steps.later.outputs.id.value }}",
                    "tagKey": "k",
                    "expectedValue": "succeeded",
                },
            },
            {"name": "later", "template": "templates/x.bicep"},
        ]
        (tmp_workspace / "templates" / "x.bicep").write_text("// noop", encoding="utf-8")
        path = self._write_manifest(tmp_workspace, steps)
        orch = Orchestrator(tmp_workspace)
        errors = orch.validate(path)
        assert any("does not execute before it" in e for e in errors)

    def test_valid_prior_reference_passes(self, tmp_workspace):
        steps = [
            {"name": "bootstrap", "template": "templates/x.bicep"},
            {
                "name": "wait-bs",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "{{ steps.bootstrap.outputs.id.value }}",
                    "tagKey": "k",
                    "expectedValue": "succeeded",
                },
            },
        ]
        (tmp_workspace / "templates" / "x.bicep").write_text("// noop", encoding="utf-8")
        path = self._write_manifest(tmp_workspace, steps)
        orch = Orchestrator(tmp_workspace)
        errors = orch.validate(path)
        assert not any("wait condition" in e for e in errors)


class TestWaitStepDryRunPreview:
    def test_preview_renders_wait_step(self, tmp_workspace, capsys):
        import yaml

        site = {
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "munich-dev",
            "subscription": "00000000-0000-0000-0000-000000000000",
            "resourceGroup": "rg-test",
            "location": "westus2",
            "labels": {"environment": "dev"},
        }
        (tmp_workspace / "sites" / "munich-dev.yaml").write_text(yaml.dump(site), encoding="utf-8")
        manifest = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "test",
            "description": "test",
            "siteSelector": "environment=dev",
            "steps": [_wait_step_data()],
        }
        path = tmp_workspace / "manifests" / "test.yaml"
        path.write_text(yaml.dump(manifest), encoding="utf-8")

        orch = Orchestrator(tmp_workspace)
        orch.show_plan(path)
        out = capsys.readouterr().out
        assert "wait-bootstrap (wait)" in out
        assert "siteops.bootstrap.state == succeeded" in out
        assert "failurePattern: failed-*" in out
