"""Qualify the preview-only API boundary without contacting Azure DevOps."""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler
from urllib.response import addinfourl

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
REF = "refs/heads/validation"
REPOSITORY = "https://github.com/example/repository"
CONNECTIONS = {name: f"private-{name}-connection" for name in ("dev", "staging", "prod")}
GROUPS = {name: f"private-{name}-group" for name in CONNECTIONS}
PIPELINE_ID = 10


@pytest.fixture
def preview_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("ado_preview", ROOT / "scripts" / "preview-ado-pipelines.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preview(preview_module, monkeypatch):
    monkeypatch.setattr(preview_module, "source_documents", lambda commit: {
        name: (ROOT / path).read_text(encoding="utf-8")
        for name, path in preview_module.PIPELINES.items()
    })
    return preview_module


def _expanded(case):
    if case.name.startswith("consumer-validate-"):
        return {"stages": [{"stage": "validate", "jobs": [{"job": "siteops_validate", "steps": [
            {"displayName": "Install Site Ops", "env": {
                "SITEOPS_RELEASE": case.parameters["release"],
                "SITEOPS_SOURCE_COMMIT": case.parameters["sourceCommit"],
            }},
            {"displayName": "Validate caller content", "env": {
                "WORKSPACE": case.parameters["workspace"], "MANIFEST": case.parameters["manifest"],
                "SELECTOR": case.parameters.get("selector", ""), "SITE_FILE": case.parameters.get("siteFile", ""),
            }},
        ]}]}]}
    if case.override:
        steps = [{
            "displayName": "Install Site Ops",
            "env": {"INSTALL_DEV": case.parameters.get("installDev", False),
                    "SITEOPS_SOURCE": case.parameters.get("siteopsSource", ""),
                    "SITEOPS_RELEASE": case.parameters.get("release", ""),
                    "SITEOPS_SOURCE_COMMIT": case.parameters.get("sourceCommit", "")},
        }]
        if case.parameters["enableCache"]:
            steps.append({"displayName": "Cache pip packages"})
        return {"jobs": [{"job": "setup_preview", "steps": steps}]}
    if case.pipeline == "ci":
        return {"jobs": [
            {"job": "lint"}, {"job": "validate"}, {"job": "test", "steps": [
                {"displayName": "Run unit tests"},
                {"displayName": "Prepare native Linux uv fixtures",
                 "script": "bash tests/fixtures/prepare-native-uv.sh"},
            ]},
        ]}
    env = case.parameters.get("environment", "dev")
    deploy = case.pipeline == "deploy"
    task_env = (
        {"SELECTOR": case.selector, "DRY_RUN": case.parameters.get("dryRun", False),
         "MANIFEST": case.parameters.get("manifest", "manifests/aio-install/manifest.yaml"),
         "SITE_FILE": case.parameters.get("siteFile", "")}
        if deploy else
        {"MANIFEST": case.parameters.get("manifest", "all"),
         "INTEGRATION_SKIP_CLEANUP": case.parameters.get("skipCleanup", False)}
    )
    return {"jobs": [{
        "deployment": "siteops_deploy" if deploy else "integration_test",
        "environment": {"name": env}, "strategy": {"runOnce": {"deploy": {"steps": [{
            "displayName": "Prepare executable plan and deploy" if deploy else "Run integration tests",
            "inputs": {"azureSubscription": CONNECTIONS[env], "scriptType": "bash",
                       "keepAzSessionActive": case.parameters.get("keepAzSessionActive", False),
                       "inlineScript": "siteops deploy --yes" + (" --site-file target.yaml" if case.parameters.get("siteFile") else "")},
            "env": task_env,
        }]}}},
    }]}


def _client(preview, fault=""):
    class Client:
        def __init__(self):
            self.calls = []
            self.cases = iter(preview.cases())

        def request(self, pipeline_id, *, preview=None):
            self.calls.append((pipeline_id, preview))
            if preview is None:
                return {
                    "id": pipeline_id + (1 if fault == "id" else 0),
                    "repository": {"url": REPOSITORY + ("/other" if fault == "repo" else "")},
                    "process": {"yamlFilename": ".pipelines/" + (
                        "wrong.yaml" if fault == "path" else "validate-pipelines.yaml"
                    )},
                }
            if fault == "missing-yaml":
                return {}
            if fault == "empty-yaml":
                return {"finalYaml": ""}
            case = next(self.cases)
            return {"finalYaml": yaml.safe_dump(_expanded(case))}

    return Client()


def test_preview_binds_every_request_to_the_exact_source_and_retains_only_safe_receipts(preview):
    client = _client(preview)
    report = preview.qualify(client, PIPELINE_ID, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
    assert client.calls[0] == (PIPELINE_ID, None)
    selected = preview.cases()
    assert len(client.calls[1:]) == len(selected) == len(report["cases"])
    assert len({case.name for case in selected}) == len(selected)
    for case, (pipeline_id, request) in zip(selected, client.calls[1:], strict=True):
        assert pipeline_id == PIPELINE_ID
        assert request["previewRun"] is True
        assert request["resources"]["repositories"]["self"] == {"refName": REF, "version": SOURCE}
        if case.override:
            assert request["yamlOverride"] == case.override
            assert request["templateParameters"] == {}
        else:
            assert request["yamlOverride"] == preview.source_documents(SOURCE)[case.pipeline]
            values = request["templateParameters"]
            assert all(isinstance(value, str) for value in values.values())
            for name, value in case.parameters.items():
                expected = ("true" if value else "false") if isinstance(value, bool) else value
                assert values[name] == expected
            if case.pipeline != "ci":
                assert set(values) == set(case.parameters) | {"serviceConnections", "secretGroups"}
                assert yaml.safe_load(values["serviceConnections"]) == CONNECTIONS
                assert yaml.safe_load(values["secretGroups"]) == GROUPS
    receipt = json.dumps(report)
    assert report["sourceCommit"] == SOURCE
    assert report["expectedCases"] == [case.name for case in selected]
    assert "private-" not in receipt and "example.invalid" not in receipt
    assert "finalYaml" not in receipt
    assert all(len(case["expandedSha256"]) == 64 for case in report["cases"])
    assert all(len(case["inputSha256"]) == 64 for case in report["cases"])
    assert len({case["inputSha256"] for case in report["cases"]}) == len(selected)
    for receipt, (_, request) in zip(report["cases"], client.calls[1:], strict=True):
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert receipt["inputSha256"] == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("value", [None, 1, 1.5])
def test_template_parameters_reject_values_without_a_string_encoding(preview, value):
    with pytest.raises(preview.PreviewError):
        preview._template_parameter(value)


def test_generated_overrides_lead_each_item_with_its_type_key(preview):
    overrides = [case for case in preview.cases() if case.override]
    assert {case.name for case in overrides} == {
        "setup-development", "setup-external", "setup-release",
        "consumer-validate-selector", "consumer-validate-site-file",
    }
    for case in overrides:
        items = []
        pending = [yaml.safe_load(case.override)]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key in ("stages", "jobs", "steps"):
                    items.extend(node.get(key, []))
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        assert items
        for item in items:
            first = next(iter(item))
            assert first in {"template", "stage", "job"}, (case.name, first)


@pytest.mark.parametrize("fault", ["repo", "path", "id", "missing-yaml", "empty-yaml"])
def test_preview_refuses_unbound_definitions_and_absent_expansion(preview, fault):
    client = _client(preview, fault)
    with pytest.raises(preview.PreviewError):
        preview.qualify(client, PIPELINE_ID, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
    assert len(client.calls) == (1 if fault in {"repo", "path", "id"} else 2)


@pytest.mark.parametrize("fault", ["wrong-selector", "wrong-connection", "wrong-dry-run",
                                   "wrong-manifest", "missing-consent", "missing-step", "template"])
def test_preview_checks_actual_expanded_deployment_inputs(preview, fault):
    case = next(case for case in preview.cases() if case.name == "deploy-dev-plan")
    document = _expanded(case)
    step = document["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"][0]
    if fault == "wrong-selector":
        step["env"]["SELECTOR"] = "environment=prod"
    elif fault == "wrong-connection":
        step["inputs"]["azureSubscription"] = "private-other"
    elif fault == "wrong-dry-run":
        step["env"]["DRY_RUN"] = False
    elif fault == "wrong-manifest":
        step["env"]["MANIFEST"] = "other"
    elif fault == "missing-consent":
        step["inputs"]["inlineScript"] = "siteops deploy"
    elif fault == "missing-step":
        step["displayName"] = "different-step"
    else:
        step["template"] = "unexpanded.yaml"
    with pytest.raises(preview.PreviewError):
        preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)


@pytest.mark.parametrize("pipeline", ["deploy", "integration"])
@pytest.mark.parametrize("enabled", [False, True])
def test_preview_checks_the_requested_wif_session_refresh(preview, pipeline, enabled):
    selected = [case for case in preview.cases() if case.pipeline == pipeline and not case.override
                and case.parameters.get("keepAzSessionActive", False) is enabled]
    assert selected, "Both enabled and default-disabled paths need service preview coverage."
    case = selected[0]
    document = _expanded(case)
    preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)
    inputs = document["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"][0]["inputs"]
    inputs["keepAzSessionActive"] = str(enabled)
    preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)
    inputs["keepAzSessionActive"] = not enabled
    with pytest.raises(preview.PreviewError, match="session refresh"):
        preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)


@pytest.mark.parametrize("fault", [None, "azure", "release", "site-file"])
def test_preview_qualifies_lightweight_consumer_validation(preview, fault):
    case = next(case for case in preview.cases() if case.name == "consumer-validate-site-file")
    document = _expanded(case)
    job = document["stages"][0]["jobs"][0]
    if fault == "azure":
        job["steps"].append({"task": "AzureCLI@2"})
    elif fault == "release":
        job["steps"][0]["env"]["SITEOPS_RELEASE"] = "other"
    elif fault == "site-file":
        job["steps"][1]["env"]["SITE_FILE"] = "other.yaml"
    if fault:
        with pytest.raises(preview.PreviewError):
            preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)
    else:
        preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)


class Response(io.BytesIO):
    status = 200


def test_http_client_uses_only_definition_reads_and_the_dedicated_preview_endpoint(preview):
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")
    calls = []

    def open_request(request, *, timeout):
        calls.append(request)
        assert timeout == 30
        assert request.get_header("Authorization") == "Bearer synthetic-token"
        return Response(b'{"finalYaml":"jobs: []"}')

    client.opener.open = open_request
    client.request(10)
    client.request(10, preview={"previewRun": True})
    assert [(request.method, request.full_url) for request in calls] == [
        ("GET", "https://dev.azure.com/example/project/_apis/build/definitions/10?api-version=7.1"),
        ("POST", "https://dev.azure.com/example/project/_apis/pipelines/10/preview?api-version=7.1"),
    ]
    with pytest.raises(preview.PreviewError, match="previewRun"):
        client.request(10, preview={"previewRun": False})
    assert len(calls) == 2


@pytest.mark.parametrize("fault", ["http", "transport", "oversized", "invalid-json", "array", "redirect"])
def test_http_failures_never_queue_a_run_or_publish_private_diagnostics(preview, fault):
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")
    calls = []

    def open_request(request, *, timeout):
        calls.append(request)
        if fault == "http":
            raise HTTPError(request.full_url, 403, "private-diagnostic", {}, None)
        if fault == "transport":
            raise URLError("private-diagnostic")
        if fault == "redirect":
            return preview.NoRedirect().redirect_request(
                request, None, 302, "private-diagnostic", {}, "https://other.invalid",
            )
        raw = {"oversized": b" " * (preview.MAX_RESPONSE + 1),
               "invalid-json": b"private-diagnostic", "array": b"[]"}[fault]
        return Response(raw)

    client.opener.open = open_request
    with pytest.raises(preview.PreviewError) as caught:
        client.request(10, preview={"previewRun": True})
    assert "private-diagnostic" not in str(caught.value)
    assert "synthetic-token" not in str(caught.value)
    assert len(calls) == 1 and "/preview?" in calls[0].full_url


@pytest.mark.parametrize("type_key,category", [
    ("NullReferenceException", "service-null-reference"),
    ("AccessDeniedException", "access-denied"),
    ("VssUnauthorizedException", "authentication-rejected"),
    ("PipelineValidationException", "yaml-validation"),
    ("JsonReaderException", "request-json"),
    ("ArgumentNullException", "missing-argument"),
    ("private-service-type", "unclassified"),
])
def test_http_error_categories_preserve_failure_without_private_response_text(
    preview, type_key, category,
):
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")
    body = io.BytesIO(json.dumps({
        "typeKey": type_key, "message": "private-resource-name synthetic-token",
        "innerException": {"message": "private-inner-detail"},
    }).encode())
    calls = []

    def fail(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 500, "private-reason", {}, body)

    client.opener.open = fail
    with pytest.raises(preview.PreviewError) as caught:
        client.request(10, preview={"previewRun": True})
    diagnostic = str(caught.value)
    assert "HTTP 500" in diagnostic and f"category: {category}" in diagnostic
    assert "private-" not in diagnostic and "synthetic-token" not in diagnostic
    assert body.closed
    assert len(calls) == 1 and "/preview?" in calls[0].full_url


@pytest.mark.parametrize("fault", ["malformed", "array", "bad-type", "oversized", "read", "close", "none"])
def test_unavailable_http_diagnostics_do_not_mask_the_original_failure(preview, fault):
    class Body(io.BytesIO):
        def read(self, size=-1):
            assert size == preview.MAX_ERROR_RESPONSE + 1
            if fault == "read":
                raise OSError("private-read-detail")
            return super().read(size)

        def close(self):
            super().close()
            if fault == "close":
                raise OSError("private-close-detail")

    raw = json.dumps({"typeKey": "NullReferenceException", "message": "private-detail"}).encode()
    if fault == "malformed":
        raw = b"private-malformed"
    elif fault == "array":
        raw = b"[]"
    elif fault == "bad-type":
        raw = b'{"typeKey":["private-detail"]}'
    elif fault == "oversized":
        raw = raw.ljust(preview.MAX_ERROR_RESPONSE + 1, b" ")
    body = None if fault == "none" else Body(raw)
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")

    def fail(request, *, timeout):
        raise HTTPError(request.full_url, 503, "private-reason", {}, body)

    client.opener.open = fail
    with pytest.raises(preview.PreviewError) as caught:
        client.request(10, preview={"previewRun": True})
    assert "HTTP 503" in str(caught.value)
    assert "private-" not in str(caught.value)
    assert "diagnostic-unavailable" in str(caught.value) or "unclassified" in str(caught.value)


@pytest.mark.parametrize("qualified_name,category", [
    ("System.NullReferenceException, private-assembly-detail", "service-null-reference"),
    ("Newtonsoft.Json.JsonReaderException, private-assembly-detail", "request-json"),
    ("private.Namespace.NullReferenceException", "unclassified"),
])
@pytest.mark.parametrize("padding", [False, True])
def test_http_categories_admit_only_known_qualified_types_and_the_exact_size_boundary(
    preview, qualified_name, category, padding,
):
    raw = json.dumps({"typeName": qualified_name, "message": "private-detail"}).encode()
    if padding:
        raw = raw.ljust(preview.MAX_ERROR_RESPONSE, b" ")
    body = io.BytesIO(raw)
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")

    def fail(request, *, timeout):
        raise HTTPError(request.full_url, 500, "private-reason", {}, body)

    client.opener.open = fail
    with pytest.raises(preview.PreviewError) as caught:
        client.request(10, preview={"previewRun": True})
    assert f"category: {category}" in str(caught.value)
    assert "private" not in str(caught.value)
    assert body.closed


def _service_failure(preview, document):
    body = io.BytesIO(json.dumps(document).encode())
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")

    def fail(request, *, timeout):
        raise HTTPError(request.full_url, 500, "private-reason", {}, body)

    client.opener.open = fail
    with pytest.raises(preview.PreviewError) as caught:
        client.request(10, preview={"previewRun": True})
    assert body.closed
    return str(caught.value)


@pytest.mark.parametrize("type_key", [
    "PipelinePreviewServiceException",
    "X" + "a" * 95 + "Exception",
])
def test_unlisted_service_exception_class_names_are_reported_without_message_text(preview, type_key):
    diagnostic = _service_failure(preview, {
        "typeKey": type_key,
        "typeName": "Private.Namespace.PrivateType, private-assembly-detail",
        "message": "private-resource-name synthetic-token",
        "innerException": {"message": "private-inner-detail"},
    })
    assert f"(HTTP 500; category: unclassified; service type: {type_key})." in diagnostic
    assert "private" not in diagnostic.casefold() and "synthetic-token" not in diagnostic


@pytest.mark.parametrize("document", [
    {"typeKey": "private-service-type"},
    {"typeKey": "privateServiceException"},
    {"typeKey": "Private.ServiceException"},
    {"typeKey": "Private ServiceException"},
    {"typeKey": "PrivateServiceExceptionDetail"},
    {"typeKey": "\u00c4privateServiceException"},
    {"typeKey": "PrivateServiceException\n"},
    {"typeKey": "X" + "a" * 96 + "Exception"},
    {"typeKey": ["PrivateServiceException"]},
    {"typeName": "Private.Namespace.PrivateServiceException, private-assembly-detail"},
])
def test_service_type_report_rejects_values_outside_the_class_name_shape(preview, document):
    diagnostic = _service_failure(preview, {**document, "message": "private-detail"})
    assert diagnostic.endswith("(HTTP 500; category: unclassified).")
    assert "service type" not in diagnostic and "private" not in diagnostic.casefold()


@pytest.mark.parametrize("destination", [
    "https://other.invalid/private-diagnostic",
    "https://dev.azure.com/example/project/_apis/pipelines/10/runs",
])
def test_real_opener_refuses_redirects_before_forwarding_credentials(preview, destination):
    client = preview.AdoClient("https://dev.azure.com/example", "project", "synthetic-token")
    calls = []

    class ClosedHTTPS(BaseHandler):
        handler_order = 100

        def https_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            headers["Location"] = destination
            response = addinfourl(io.BytesIO(b"private-diagnostic"), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    client.opener.add_handler(ClosedHTTPS())
    with pytest.raises(preview.PreviewError) as caught:
        client.request(10, preview={"previewRun": True})
    assert calls == ["https://dev.azure.com/example/project/_apis/pipelines/10/preview?api-version=7.1"]
    assert "unexpected redirect" in str(caught.value)
    assert "private-diagnostic" not in str(caught.value)


@pytest.mark.parametrize("fault", ["none", "missing-yaml", "existing-output", "malformed-config"])
def test_preview_command_publishes_a_receipt_only_after_all_cases_pass(
    preview, tmp_path, monkeypatch, capsys, fault,
):
    output = tmp_path / "receipt.json"
    environment = {
        "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example",
        "SYSTEM_TEAMPROJECTID": "project",
        "SYSTEM_ACCESSTOKEN": "synthetic-token",
        "SYSTEM_DEFINITIONID": str(PIPELINE_ID),
        "BUILD_REPOSITORY_URI": REPOSITORY,
        "BUILD_SOURCEBRANCH": REF,
        "BUILD_SOURCEVERSION": SOURCE,
        "ADO_PREVIEW_CONNECTIONS": json.dumps(CONNECTIONS),
        "ADO_PREVIEW_GROUPS": json.dumps(GROUPS),
    }
    if fault == "malformed-config":
        environment["SYSTEM_DEFINITIONID"] = "private-malformed-configuration"
    if fault == "existing-output":
        output.write_text("existing-receipt", encoding="utf-8")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["preview-ado-pipelines.py", "--output", str(output)])
    client = _client(preview, fault)
    monkeypatch.setattr(preview, "AdoClient", lambda *args: client)

    assert preview.main() == (0 if fault == "none" else 1)
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err
    assert "synthetic-token" not in captured.out + captured.err
    if fault == "none":
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["status"] == "passed" and report["sourceCommit"] == SOURCE
        assert len(report["cases"]) == len(preview.cases())
        assert "finalYaml" not in output.read_text(encoding="utf-8")
    elif fault == "existing-output":
        assert output.read_text(encoding="utf-8") == "existing-receipt"
        assert client.calls == []
    else:
        assert not output.exists()
        if fault == "malformed-config":
            assert client.calls == []
        else:
            assert len(client.calls) == 2


@pytest.mark.parametrize("collection", ["http://dev.azure.com/example", "https://other.invalid/example",
                                      "https://dev.azure.com/example/other",
                                      "https://user@dev.azure.com/example"])
def test_preview_authentication_is_confined_to_the_selected_ado_origin(preview, collection):
    with pytest.raises(preview.PreviewError):
        preview.AdoClient(collection, "project", "synthetic-token")


@pytest.mark.parametrize("pipeline_id", [True, 0, -1, "", "10", {}, None])
def test_invalid_pipeline_selection_never_reaches_the_service(preview, pipeline_id):
    client = _client(preview)
    with pytest.raises(preview.PreviewError):
        preview.qualify(client, pipeline_id, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
    assert client.calls == []


@pytest.mark.parametrize("field", ["env", "inputs"])
def test_malformed_expansion_fields_fail_with_fixed_diagnostics(preview, field):
    case = next(case for case in preview.cases() if case.name == "deploy-dev-plan")
    document = _expanded(case)
    document["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"][0][field] = "private-malformed"
    with pytest.raises(preview.PreviewError) as caught:
        preview.validate_expansion(case, yaml.safe_dump(document), CONNECTIONS)
    assert "private-malformed" not in str(caught.value)


@pytest.mark.parametrize("recursive", [False, True])
def test_preview_bounds_recursive_and_oversized_yaml_structures(preview, recursive):
    text = "jobs: &loop [*loop]" if recursive else yaml.safe_dump({"jobs": [{}] * 20001})
    with pytest.raises(preview.PreviewError, match="structure limits"):
        preview.validate_expansion(preview.cases()[0], text, CONNECTIONS)


def test_preview_structure_limits_accept_the_boundary_and_reject_the_next_node(preview):
    assert list(preview._nodes({"jobs": [None] * 19998}))
    with pytest.raises(preview.PreviewError, match="structure limits"):
        list(preview._nodes({"jobs": [None] * 19999}))
    value = {}
    for _ in range(64):
        value = [value]
    assert list(preview._nodes(value)) == [{}]
    with pytest.raises(preview.PreviewError, match="structure limits"):
        list(preview._nodes([value]))


def test_validation_pipeline_is_manual_and_has_no_deployment_identity():
    source = yaml.safe_load((ROOT / ".pipelines" / "validate-pipelines.yaml").read_text())
    assert source["trigger"] == source["pr"] == "none"
    assert "pipelineIds" not in {parameter["name"] for parameter in source["parameters"]}
    stage = next(stage for stage in source["stages"] if stage.get("stage") == "preview")
    steps = stage["jobs"][0]["steps"]
    assert not any(step.get("task", "").startswith("AzureCLI@") for step in steps)
    assert source["variables"] == {"SITE_OVERRIDES": ""}
    execute = next(step for step in steps if "script" in step)
    assert "scripts/preview-ado-pipelines.py" in execute["script"]
    assert execute["env"]["BUILD_SOURCEVERSION"] == "$(Build.SourceVersion)"
    assert execute["env"]["SYSTEM_ACCESSTOKEN"] == "$(System.AccessToken)"
    assert execute["env"]["SYSTEM_DEFINITIONID"] == "$(System.DefinitionId)"
    assert "ADO_PREVIEW_PIPELINE_IDS" not in execute["env"]
    artifact = next(step for step in steps if step.get("task") == "PublishPipelineArtifact@1")
    assert artifact.get("condition", "succeeded()") == "succeeded()"
    assert artifact["inputs"]["targetPath"].endswith("/pipeline-preview.json")


def test_full_preview_matrix_is_retained(preview):
    integration = yaml.safe_load((ROOT / preview.PIPELINES["integration"]).read_text())
    phases = next(parameter["values"] for parameter in integration["parameters"]
                  if parameter["name"] == "manifest")
    expected = {"ci", "deploy-custom-selector", "deploy-wif-session", "deploy-site-file",
                "integration-wif-session", "integration-staging", "integration-prod",
                "setup-development", "setup-external", "setup-release",
                "consumer-validate-selector", "consumer-validate-site-file"}
    expected.update(f"deploy-{environment}-{action}"
                    for environment in ("dev", "staging", "prod") for action in ("plan", "apply"))
    expected.update(f"deploy-{sample}-{selector}"
                    for sample in ("resource-set-basic", "resource-set-composition")
                    for selector in ("default", "custom"))
    expected.update(f"integration-{phase}" for phase in phases)
    assert len(preview.cases()) == len(expected) == 29
    assert {case.name for case in preview.cases()} == expected


def test_source_read_failure_stops_before_service_access(preview, monkeypatch):
    def fail(commit):
        assert commit == SOURCE
        raise preview.PreviewError("The reviewed source could not be read.")

    monkeypatch.setattr(preview, "source_documents", fail)
    client = _client(preview)
    with pytest.raises(preview.PreviewError, match="source"):
        preview.qualify(client, PIPELINE_ID, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
    assert client.calls == []


@pytest.mark.parametrize("fault", [None, "checkout", "size", "oversized", "read", "encoding"])
def test_source_documents_use_bounded_committed_blobs(preview_module, tmp_path, monkeypatch, fault):
    module = preview_module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    documents = {name: f"# committed {name}\njobs: []\n" for name in module.PIPELINES}
    validated = []
    calls = []
    for path in module.PIPELINES.values():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("uncommitted private marker", encoding="utf-8")

    def validate(root, commit):
        validated.append((root, commit))
        if fault == "checkout":
            raise module.SourceSnapshotError("The source checkout changed.")

    def git(root, arguments):
        assert root == tmp_path
        calls.append(arguments)
        assert arguments[:2] == ["--no-replace-objects", "cat-file"]
        name = next(name for name, path in module.PIPELINES.items()
                    if arguments[-1] == SOURCE + ":" + path)
        raw = b"\xff" if fault == "encoding" else documents[name].encode()
        if arguments[2] == "-s":
            size = module.MAX_RESPONSE + 1 if fault == "oversized" else len(raw)
            return subprocess.CompletedProcess(arguments, int(fault == "size"), str(size).encode())
        assert arguments[2] == "blob"
        return subprocess.CompletedProcess(arguments, int(fault == "read"), raw)

    monkeypatch.setattr(module, "validate_repository", validate)
    monkeypatch.setattr(module, "_git", git)
    if fault:
        with pytest.raises(module.PreviewError):
            module.source_documents(SOURCE)
    else:
        assert module.source_documents(SOURCE) == documents
        assert len(calls) == 6
    assert validated == [(tmp_path, SOURCE)]
    if fault in {"size", "oversized"}:
        assert len(calls) == 1


def test_azure_repo_organization_hint_is_not_a_credential(preview):
    plain = "https://dev.azure.com/example/project/_git/repository"
    hinted = "https://example@dev.azure.com/example/project/_git/repository"
    assert preview._repository(plain) == preview._repository(hinted)


@pytest.mark.parametrize("repository", [
    "https://user@github.com/example/repository",
    "https://other@dev.azure.com/example/project/_git/repository",
    "https://example:private@dev.azure.com/example/project/_git/repository",
    "https://example@other.invalid/example/project/_git/repository",
])
def test_repository_hints_do_not_admit_credentials_or_other_hosts(preview, repository):
    with pytest.raises(preview.PreviewError, match="credentials"):
        preview._repository(repository)


def test_case_inventory_uses_the_selected_committed_integration_source(preview):
    document = yaml.safe_dump({
        "parameters": [{"name": "manifest", "values": ["selected-phase"]}],
    })
    selected = preview.cases(document)
    assert any(case.name == "integration-selected-phase" for case in selected)
    assert not any(case.name == "integration-all" for case in selected)


@pytest.mark.parametrize("document", [
    "", "[]", "parameters: []", "parameters: [\n",
    "parameters: [{name: manifest, values: []}]",
    "parameters: [{name: manifest, values: [all, all]}]",
    "parameters: [{name: manifest, values: [1]}]",
    "parameters: [{name: manifest, values: [{}]}]",
])
def test_bad_committed_phase_inventory_is_a_fixed_failure(preview, document):
    with pytest.raises(preview.PreviewError, match="phase inventory"):
        preview.cases(document)


@pytest.mark.parametrize("length", [0, 31, 32, 33])
def test_committed_source_size_boundaries(preview_module, tmp_path, monkeypatch, length):
    module = preview_module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "MAX_RESPONSE", 32)
    monkeypatch.setattr(module, "validate_repository", lambda root, commit: None)
    reads = []

    def git(root, arguments):
        reads.append(arguments)
        raw = str(length).encode() if arguments[2] == "-s" else b"x" * length
        return subprocess.CompletedProcess(arguments, 0, raw)

    monkeypatch.setattr(module, "_git", git)
    if length in {0, 33}:
        with pytest.raises(module.PreviewError, match="empty or too large"):
            module.source_documents(SOURCE)
        assert len(reads) == 1
    else:
        assert all(len(text) == length for text in module.source_documents(SOURCE).values())
        assert len(reads) == 6
