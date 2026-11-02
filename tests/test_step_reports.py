"""Step reports: `reports.warnings` on a template step, with Manifest `requiredFeatures`.

The fixture is a generic queue check, so the contract is proven without any
workspace content.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from siteops import package_builder
from siteops.artifacts import ArtifactError
from siteops.compilation import PreparedTemplateUnit, extract_template_output_types
from siteops.diagnostics import (
    STEP_REPORT_INVALID,
    STEP_REPORTED,
    ContentError,
    recording_warnings,
)
from siteops.executor import DeploymentResult
from siteops.models import DeploymentStep, Manifest
from siteops.orchestrator import Orchestrator, PlanValueResolutionError
from siteops.results import OperationStatus, SiteStatus
from siteops.step_reports import publish_step_warnings, step_warnings
from siteops.workspace_compatibility import STEP_REPORTS_FEATURE, SUPPORTED_FEATURES

QUEUE_CHECK = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {"queueName": {"type": "string"}},
    "resources": [],
    "outputs": {
        "warnings": {"type": "array", "value": [{"message": "[format('Queue {0} has no dead letter target.', parameters('queueName'))]"}]},
        "queueId": {"type": "string", "value": "[parameters('queueName')]"},
    },
}


def _write_workspace(workspace: Path, steps: list[dict], *, features: list[str] | None = None) -> Path:
    (workspace / "sites" / "plant.yaml").write_text(yaml.safe_dump({
        "name": "plant", "subscription": "00000000-0000-0000-0000-000000000000",
        "resourceGroup": "rg-plant", "location": "eastus",
        "parameters": {"queueName": "orders"},
    }), encoding="utf-8")
    (workspace / "templates" / "queue-check.json").write_text(json.dumps(QUEUE_CHECK), encoding="utf-8")
    manifest: dict = {"name": "queues", "sites": ["plant"], "steps": steps}
    if features is not None:
        manifest["requiredFeatures"] = features
    path = workspace / "manifests" / "queues.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def _check_step(**extra) -> dict:
    return {"name": "check-queue", "template": "templates/queue-check.json", **extra}


def _load(path: Path) -> Manifest:
    return Manifest.from_file(path, workspace_root=path.parent.parent)


class TestDeclaration:
    def test_a_deployment_step_binds_an_output(self, tmp_workspace):
        manifest = _load(_write_workspace(
            tmp_workspace, [_check_step(reports={"warnings": "warnings"})], features=[STEP_REPORTS_FEATURE],
        ))
        [step] = manifest.steps
        assert isinstance(step, DeploymentStep) and step.reports_warnings == "warnings"
        assert manifest.required_features == (STEP_REPORTS_FEATURE,)

    @pytest.mark.parametrize("reports", [None, {}, [], "warnings", {"warnings": ""}, {"warnings": " "},
                                         {"warnings": "a" * 257}, {"warnings": "bad\u0007name"},
                                         {"warnings": 3}])
    def test_a_malformed_binding_is_rejected(self, tmp_workspace, reports):
        path = _write_workspace(tmp_workspace, [_check_step(reports=reports)], features=[STEP_REPORTS_FEATURE])
        with pytest.raises(ContentError) as error:
            _load(path)
        assert error.value.diagnostic.code in {"content.invalid-type"}

    def test_an_unknown_report_key_is_rejected(self, tmp_workspace):
        path = _write_workspace(
            tmp_workspace, [_check_step(reports={"warnings": "warnings", "findings": "x"})],
            features=[STEP_REPORTS_FEATURE],
        )
        with pytest.raises(ContentError) as error:
            _load(path)
        assert error.value.diagnostic.code == "content.unknown-field"

    @pytest.mark.parametrize("step", [
        {"name": "apply", "type": "kubectl", "operation": "apply",
         "arc": {"name": "c", "resourceGroup": "rg"}, "files": ["a.yaml"], "reports": {"warnings": "w"}},
        {"name": "settle", "type": "wait", "reports": {"warnings": "w"}},
    ])
    def test_other_step_kinds_reject_reports(self, tmp_workspace, step):
        path = _write_workspace(tmp_workspace, [step], features=[STEP_REPORTS_FEATURE])
        with pytest.raises(ContentError) as error:
            _load(path)
        assert error.value.diagnostic.code == "content.field-placement-invalid"

    def test_an_include_step_rejects_reports(self, tmp_workspace):
        path = _write_workspace(
            tmp_workspace, [{"include": "other.yaml", "reports": {"warnings": "w"}}],
            features=[STEP_REPORTS_FEATURE],
        )
        with pytest.raises(ValueError, match="Only `include` and `when` are allowed"):
            _load(path)

    def test_a_binding_needs_its_feature(self, tmp_workspace):
        path = _write_workspace(tmp_workspace, [_check_step(reports={"warnings": "warnings"})])
        with pytest.raises(ContentError) as error:
            _load(path)
        assert error.value.diagnostic.code == "content.required-field-missing"
        assert STEP_REPORTS_FEATURE in str(error.value)

    def test_an_unsupported_feature_is_refused_before_anything_runs(self, tmp_workspace):
        path = _write_workspace(tmp_workspace, [_check_step()], features=["future-reports/v9"])
        with pytest.raises(ContentError) as error:
            _load(path)
        assert error.value.diagnostic.code == "content.feature-unsupported"
        assert "future-reports/v9" in str(error.value)

    @pytest.mark.parametrize("features", [["a", "a"], [""], ["x" * 129], "step-reports/v1",
                                          [f"f{index}" for index in range(65)], ["tab\tname"],
                                          ["line\u2028separator"]])
    def test_a_malformed_feature_list_is_rejected(self, tmp_workspace, features):
        path = _write_workspace(tmp_workspace, [_check_step()], features=features)
        with pytest.raises(ContentError) as error:
            _load(path)
        assert error.value.diagnostic.code == "content.invalid-type"

    @pytest.mark.parametrize("nested", [False, True])
    def test_an_explicit_null_feature_list_is_rejected_and_an_empty_one_is_not(self, tmp_workspace, nested):
        path = tmp_workspace / "manifests" / "features.yaml"
        _write_workspace(tmp_workspace, [_check_step()])
        for features, accepted in ((None, False), ([], True)):
            body = {"sites": ["plant"], "requiredFeatures": features, "steps": [_check_step()]}
            path.write_text(yaml.safe_dump(
                {"apiVersion": "siteops/v1", "kind": "Manifest", "metadata": {"name": "features"}, "spec": body}
                if nested else {"name": "features", **body}
            ), encoding="utf-8")
            if accepted:
                assert _load(path).required_features == ()
                continue
            with pytest.raises(ContentError) as error:
                _load(path)
            assert error.value.diagnostic.code == "content.invalid-type"

    def test_an_included_manifest_contributes_its_requirements(self, tmp_workspace):
        (tmp_workspace / "manifests" / "check.yaml").write_text(yaml.safe_dump({
            "name": "check", "requiredFeatures": [STEP_REPORTS_FEATURE],
            "steps": [_check_step(reports={"warnings": "warnings"})],
        }), encoding="utf-8")
        manifest = _load(_write_workspace(tmp_workspace, [{"include": "check.yaml"}]))
        assert manifest.required_features == (STEP_REPORTS_FEATURE,)
        assert manifest.steps[0].reports_warnings == "warnings"

    def test_the_nested_shape_accepts_required_features(self, tmp_workspace):
        path = tmp_workspace / "manifests" / "nested.yaml"
        _write_workspace(tmp_workspace, [_check_step()])
        path.write_text(yaml.safe_dump({
            "apiVersion": "siteops/v1", "kind": "Manifest", "metadata": {"name": "nested"},
            "spec": {"sites": ["plant"], "requiredFeatures": [STEP_REPORTS_FEATURE],
                     "steps": [_check_step(reports={"warnings": "warnings"})]},
        }), encoding="utf-8")
        assert _load(path).required_features == (STEP_REPORTS_FEATURE,)

    def test_the_engine_supports_step_reports(self):
        assert STEP_REPORTS_FEATURE in SUPPORTED_FEATURES


class TestOutputType:
    def test_output_types_resolve_local_definitions(self):
        template = {
            "definitions": {"notes": {"type": "array"}},
            "outputs": {"a": {"type": "Array"}, "b": {"$ref": "#/definitions/notes"},
                        "c": {"type": "secureObject"}, "d": {"$ref": "#/definitions/missing"}},
        }
        assert extract_template_output_types(template) == (
            ("a", "array"), ("b", "array"), ("c", "secureobject"), ("d", ""),
        )

    @pytest.mark.parametrize("output", ["queueId", "absent"])
    def test_preparation_requires_an_array_output(self, output):
        unit = PreparedTemplateUnit.__new__(PreparedTemplateUnit)
        object.__setattr__(unit, "output_types", extract_template_output_types(QUEUE_CHECK))
        with pytest.raises(PlanValueResolutionError):
            Orchestrator._validate_report_binding(unit, output)
        Orchestrator._validate_report_binding(unit, "warnings")
        Orchestrator._validate_report_binding(unit, None)


def _wrapped(records):
    return {"warnings": {"type": "Array", "value": records}}


class TestReportValue:
    @pytest.mark.parametrize("count", [0, 1, 16])
    def test_up_to_sixteen_records_are_accepted(self, count):
        records = [{"message": f"note {index}"} for index in range(count)]
        assert step_warnings(_wrapped(records), "warnings") == tuple(r["message"] for r in records)

    @pytest.mark.parametrize("outputs", [
        {}, {"warnings": [{"message": "unwrapped"}]}, _wrapped({"message": "x"}),
        _wrapped([{"message": "x"}] * 17), _wrapped([{"message": "x", "code": "c"}]),
        _wrapped([{"message": ""}]), _wrapped([{"message": "   "}]), _wrapped([{"message": "x" * 501}]),
        _wrapped([{"message": "line\nbreak"}]), _wrapped([{"message": "para\u2029graph"}]),
        _wrapped([{"message": 5}]), _wrapped(["text"]),
    ])
    def test_a_malformed_value_is_invalid(self, outputs):
        assert step_warnings(outputs, "warnings") is None

    def test_a_valid_message_keeps_its_text_literally(self):
        message = "Use [b]queue[/b] <dlq> & retry 100%"
        assert step_warnings(_wrapped([{"message": message}]), "warnings") == (message,)

    def test_the_message_limit_counts_code_points_not_bytes(self):
        assert step_warnings(_wrapped([{"message": "\U0001F600" * 500}]), "warnings") == ("\U0001F600" * 500,)
        assert step_warnings(_wrapped([{"message": "\U0001F600" * 501}]), "warnings") is None


class TestPublication:
    def test_messages_print_with_site_and_step_and_record_one_code(self, caplog, monkeypatch):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
        with recording_warnings() as recorder, caplog.at_level(logging.WARNING, logger="siteops"):
            publish_step_warnings("plant", "check-queue", _wrapped([{"message": "first"}, {"message": "second"}]), "warnings")
        assert "Site 'plant' step 'check-queue' reported: first" in caplog.text
        assert "second" in caplog.text
        [document] = recorder.documents()
        assert document["code"] == STEP_REPORTED and document["occurrences"] == 2
        assert document["subject"] == "template"
        assert "first" not in json.dumps(document)

    def test_redaction_logs_only_registry_text(self, caplog, monkeypatch):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
        with caplog.at_level(logging.WARNING, logger="siteops"):
            publish_step_warnings("plant", "check-queue", _wrapped([{"message": "secret-ish"}]), "warnings")
            publish_step_warnings("plant", "check-queue", {}, "warnings")
        assert "secret-ish" not in caplog.text and "plant" not in caplog.text and "check-queue" not in caplog.text
        assert "A deployment step reported a warning." in caplog.text
        assert "A deployment step returned an invalid warning report." in caplog.text

    def test_an_invalid_value_records_one_invalid_code_and_no_message(self, caplog, monkeypatch):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
        with recording_warnings() as recorder, caplog.at_level(logging.WARNING, logger="siteops"):
            publish_step_warnings("plant", "check-queue", _wrapped([{"message": "ok"}, {"message": 1}]), "warnings")
        assert [document["code"] for document in recorder.documents()] == [STEP_REPORT_INVALID]
        assert "reported:" not in caplog.text


def _deploy(manifest_path: Path, result: DeploymentResult):
    orchestrator = Orchestrator(manifest_path.parent.parent)
    with patch.object(orchestrator.executor, "deploy_resource_group", return_value=result):
        with recording_warnings() as recorder:
            outcome = orchestrator.deploy(manifest_path)
    return outcome, recorder.documents()


class TestDeployment:
    def test_a_succeeded_step_reports_and_keeps_its_status(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
        path = _write_workspace(
            tmp_workspace, [_check_step(reports={"warnings": "warnings"}, parameters=[])],
            features=[STEP_REPORTS_FEATURE],
        )
        outcome, documents = _deploy(path, DeploymentResult(
            success=True, step_name="check-queue", site_name="plant", deployment_name="d",
            outputs=_wrapped([{"message": "Queue orders has no dead letter target."}]),
        ))
        [site] = outcome.sites
        assert site.status is SiteStatus.SUCCEEDED
        assert [operation.status for operation in site.operations] == [OperationStatus.SUCCEEDED]
        assert [document["code"] for document in documents] == [STEP_REPORTED]

    def test_a_failed_step_reports_nothing(self, tmp_workspace):
        path = _write_workspace(
            tmp_workspace, [_check_step(reports={"warnings": "warnings"})], features=[STEP_REPORTS_FEATURE],
        )
        outcome, documents = _deploy(path, DeploymentResult(
            success=False, step_name="check-queue", site_name="plant", deployment_name="d", error="denied",
            outputs=_wrapped([{"message": "never shown"}]),
        ))
        assert outcome.sites[0].status is SiteStatus.FAILED
        assert documents == []

    def test_an_unbound_output_is_never_read(self, tmp_workspace):
        path = _write_workspace(tmp_workspace, [_check_step()])
        _, documents = _deploy(path, DeploymentResult(
            success=True, step_name="check-queue", site_name="plant", deployment_name="d",
            outputs=_wrapped([{"message": "ignored"}]),
        ))
        assert documents == []


def test_the_producer_requires_each_manifest_feature(tmp_workspace):
    _write_workspace(tmp_workspace, [_check_step(reports={"warnings": "warnings"})], features=[STEP_REPORTS_FEATURE])
    with pytest.raises(ArtifactError, match=re.escape(STEP_REPORTS_FEATURE)):
        package_builder._require_manifest_features(tmp_workspace, ("manifests/queues.yaml",), ("manifest/v1",))
    package_builder._require_manifest_features(
        tmp_workspace, ("manifests/queues.yaml",), ("manifest/v1", STEP_REPORTS_FEATURE),
    )
