"""Qualify the preview-only API boundary without contacting Azure DevOps."""

import importlib.util
import io
import json
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
IDS = {"ci": 10, "deploy": 20, "integration": 30}


@pytest.fixture
def preview():
    spec = importlib.util.spec_from_file_location("ado_preview", ROOT / "scripts" / "preview-ado-pipelines.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _expanded(case):
    if case.override:
        steps = [{
            "displayName": "Install Site Ops",
            "env": {"INSTALL_DEV": case.parameters.get("installDev", False),
                    "SITEOPS_SOURCE": case.parameters.get("siteopsSource", "")},
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
         "MANIFEST": case.parameters.get("manifest", "manifests/aio-install/manifest.yaml")}
        if deploy else
        {"MANIFEST": case.parameters.get("manifest", "all"),
         "INTEGRATION_SKIP_CLEANUP": case.parameters.get("skipCleanup", False)}
    )
    return {"jobs": [{
        "deployment": "siteops_deploy" if deploy else "integration_test",
        "environment": {"name": env}, "strategy": {"runOnce": {"deploy": {"steps": [{
            "displayName": "Prepare executable plan and deploy" if deploy else "Run integration tests",
            "inputs": {"azureSubscription": CONNECTIONS[env], "scriptType": "bash",
                       "inlineScript": "siteops deploy --yes"},
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
                name = next(name for name, value in IDS.items() if value == pipeline_id)
                return {
                    "id": pipeline_id, "repository": {"url": REPOSITORY + ("/other" if fault == "repo" else "")},
                    "process": {"yamlFilename": ".pipelines/" + (
                        "wrong.yaml" if fault == "path" else
                        {"ci": "ci.yaml", "deploy": "deploy.yaml", "integration": "integration-test.yaml"}[name]
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
    report = preview.qualify(client, IDS, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
    assert [call[1] for call in client.calls[:3]] == [None] * 3
    selected = preview.cases()
    assert len(client.calls[3:]) == len(selected) == len(report["cases"])
    assert len({case.name for case in selected}) == len(selected)
    for case, (pipeline_id, request) in zip(selected, client.calls[3:], strict=True):
        assert pipeline_id == IDS[case.pipeline]
        assert request["previewRun"] is True
        assert request["resources"]["repositories"]["self"] == {"refName": REF, "version": SOURCE}
        if case.override:
            assert request["yamlOverride"] == case.override
            assert request["templateParameters"] == {}
        elif case.pipeline != "ci":
            assert request["templateParameters"]["serviceConnections"] == CONNECTIONS
            assert request["templateParameters"]["secretGroups"] == GROUPS
    receipt = json.dumps(report)
    assert report["sourceCommit"] == SOURCE
    assert "private-" not in receipt and "example.invalid" not in receipt
    assert "finalYaml" not in receipt
    assert all(len(case["expandedSha256"]) == 64 for case in report["cases"])


@pytest.mark.parametrize("fault", ["repo", "path", "missing-yaml", "empty-yaml"])
def test_preview_refuses_unbound_definitions_and_absent_expansion(preview, fault):
    client = _client(preview, fault)
    with pytest.raises(preview.PreviewError):
        preview.qualify(client, IDS, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
    assert len(client.calls) == (1 if fault in {"repo", "path"} else 4)


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
        "ADO_PREVIEW_PIPELINE_IDS": json.dumps(IDS),
        "BUILD_REPOSITORY_URI": REPOSITORY,
        "BUILD_SOURCEBRANCH": REF,
        "BUILD_SOURCEVERSION": SOURCE,
        "ADO_PREVIEW_CONNECTIONS": json.dumps(CONNECTIONS),
        "ADO_PREVIEW_GROUPS": json.dumps(GROUPS),
    }
    if fault == "malformed-config":
        environment["ADO_PREVIEW_PIPELINE_IDS"] = "private-malformed-configuration"
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
            assert len(client.calls) == 4


@pytest.mark.parametrize("collection", ["http://dev.azure.com/example", "https://other.invalid/example",
                                      "https://dev.azure.com/example/other",
                                      "https://user@dev.azure.com/example"])
def test_preview_authentication_is_confined_to_the_selected_ado_origin(preview, collection):
    with pytest.raises(preview.PreviewError):
        preview.AdoClient(collection, "project", "synthetic-token")


@pytest.mark.parametrize("ids", [[], {}, {"ci": True, "deploy": 20, "integration": 30},
                               {"ci": 10, "deploy": 10, "integration": 30}])
def test_invalid_pipeline_selection_never_reaches_the_service(preview, ids):
    client = _client(preview)
    with pytest.raises(preview.PreviewError):
        preview.qualify(client, ids, REPOSITORY, REF, SOURCE, CONNECTIONS, GROUPS)
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
    steps = source["jobs"][0]["steps"]
    assert not any(step.get("task", "").startswith("AzureCLI@") for step in steps)
    assert not any("group" in node for node in source.get("variables", []))
    execute = next(step for step in steps if "script" in step)
    assert "scripts/preview-ado-pipelines.py" in execute["script"]
    assert execute["env"]["BUILD_SOURCEVERSION"] == "$(Build.SourceVersion)"
    assert execute["env"]["SYSTEM_ACCESSTOKEN"] == "$(System.AccessToken)"
    artifact = next(step for step in steps if step.get("task") == "PublishPipelineArtifact@1")
    assert artifact.get("condition", "succeeded()") == "succeeded()"
    assert artifact["inputs"]["targetPath"].endswith("/pipeline-preview.json")
