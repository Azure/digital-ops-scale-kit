"""Content causes survive preparation, publication and the actual CI task bodies."""

import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import venv
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from siteops import __version__, cli, yamlio
from siteops.cli import cmd_deploy, cmd_plan, cmd_validate
from siteops.diagnostics import (
    REGISTRY,
    ContentDiagnostic,
    ContentError,
    DiagnosticCollector,
    RegistryCode,
    ValidationResult,
    diagnostic_from_error,
    public_document,
    public_entry,
    resolve_member,
)
from siteops.models import ParameterSource, Site, _validate_resource, parse_when_condition
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    DiagnosticSeverity,
    PlanBuildResult,
    PlanDiagnostic,
    PlanExecutionMode,
    PlanIntent,
    PlanProjection,
    PlanStatus,
    serialize_plan,
)
from siteops.reporting import serialize_run_json
from siteops.results import preparation_failure_result
from tests.ado_helpers import step
from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ado-consumer" / "workspace"
SENTINELS = ("FactoryBlue", "AuthorizationFailedPlant", "PrivateValue",
             "SubscriptionNotFoundFactory", "InvalidTemplateCustomer", "customerA")


def workspace(tmp_path, case):
    root = tmp_path / "FactoryBlue" / "workspace"
    shutil.copytree(FIXTURE, root)
    site = root / "sites" / "selected.yaml"
    text = site.read_text(encoding="utf-8").replace("inventory-site", "AuthorizationFailedPlant")
    if case == "duplicate":
        text += "location: PrivateValue\n"
    elif case == "unknown":
        text += "FactoryBlue: PrivateValue\n"
    site.write_text(text, encoding="utf-8")
    return root


def arguments(root, output="json"):
    return Namespace(
        workspace=root, manifest=root / "manifests" / "smoke.yaml",
        selector="environment=smoke", output=output, projection="publishable" if output == "json" else None,
        yes=True, describe=False, parallel=None, site_file=None, input_file=None, input_values=None,
    )


@pytest.mark.parametrize("case,code", [("duplicate", "content.duplicate-key"), ("unknown", "content.unknown-field")])
@pytest.mark.parametrize("command", [cmd_validate, cmd_plan, cmd_deploy])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("output", ["plain", "json"])
def test_content_cause_survives_commands(tmp_path, monkeypatch, case, code, command, output, direct):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setenv("TF_BUILD", "True")
    root = workspace(tmp_path, case)
    stdout, stderr = io.StringIO(), io.StringIO()
    args = arguments(root, output)
    if direct:
        args.site_file = root / "sites" / "selected.yaml"
        args.selector = None
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = command(args, Orchestrator(root))
    assert exit_code == 1
    rendered = stdout.getvalue() + stderr.getvalue()
    assert code in rendered
    assert all(sentinel not in rendered for sentinel in SENTINELS)
    if output == "json":
        document = json.loads(stdout.getvalue())
        diagnostic = next(item for item in document["diagnostics"] if item["code"] == code)
        entry = public_entry(resolve_member(code), "site")
        for field in ("summary", "guidance", "documentation"):
            assert diagnostic[field].encode() == getattr(entry, field).encode()
        if case == "duplicate":
            assert "schemaField" not in diagnostic


def test_provenance_wrapper_keeps_cause(tmp_path):
    root = workspace(tmp_path, "unknown")
    with pytest.raises(ContentError) as caught:
        Orchestrator(root).load_site_with_provenance("selected")
    assert caught.value.diagnostic.code == "content.unknown-field"
    assert "Merged from:" in str(caught.value)
    assert caught.value.diagnostic.source_path == root / "sites" / "selected.yaml"


def test_site_discovery_keeps_typed_cause(tmp_path):
    root = workspace(tmp_path, "duplicate")
    orchestrator = Orchestrator(root)
    assert orchestrator.load_all_sites() == []
    assert orchestrator.skipped_sites[0].diagnostic.code == "content.duplicate-key"
    assert orchestrator.skipped_sites[0].diagnostic.line is not None


def test_valid_neighbor_remains_admitted(tmp_path):
    root = workspace(tmp_path, "valid")
    result = Orchestrator(root).validate(root / "manifests" / "smoke.yaml", "environment=smoke")
    assert result.exit_code == 0 and not result.diagnostics
    logs, summary, valid = _adapter().render(result.document("publishable", __version__),
                                             "ValidationResult", 0, "ado")
    assert valid and "valid" in summary[0] and all("unavailable" not in line for line in logs)


def test_include_wrapper_keeps_cause(tmp_path):
    root = workspace(tmp_path, "valid")
    partial = root / "manifests" / "partial.yaml"
    partial.write_text("kind: Manifest\nFactoryBlue: PrivateValue\nsteps: []\n")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text("kind: Manifest\nsteps:\n  - include: partial.yaml\n")
    result = Orchestrator(root).validate(manifest)
    assert result.diagnostics[0].code == "content.unknown-field"


def test_independent_validation_does_not_fail_fast(tmp_path):
    root = workspace(tmp_path, "unknown")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + "parameters:\n  - parameters/missing.yaml\n")
    result = Orchestrator(root).validate(manifest, "environment=smoke")
    assert {"content.unknown-field", "content.file-missing"} <= {d.code for d in result.diagnostics}
    original = DiagnosticCollector()
    added = original.add("PrivateValue")
    assert not original.diagnostics and len(added.diagnostics) == 1


@pytest.mark.parametrize("projection", [PlanProjection.PUBLISHABLE, PlanProjection.LOCAL_PRIVATE])
def test_registry_public_text_cannot_be_supplied_by_producer(projection):
    for code in REGISTRY:
        if code.startswith("run."):
            continue
        result = PlanBuildResult(
            status=PlanStatus.INVALID, executable=False, plan=None, intent=PlanIntent.EXECUTABLE,
            diagnostics=(PlanDiagnostic(
                code=code, severity=DiagnosticSeverity.ERROR, summary="FactoryBlue",
                detail="PrivateValue", public_summary="AuthorizationFailedPlant",
                serialized_detail="FactoryBlue",
            ),),
        )
        documents = (
            serialize_plan(result, projection, engine_version=__version__),
            json.loads(serialize_run_json(
                preparation_failure_result(result),
                projection,
                engine_version=__version__,
            )),
        )
        for document in documents:
            item = document["diagnostics"][0]
            entry = public_entry(resolve_member(code, document["kind"],
                                                private=projection is PlanProjection.LOCAL_PRIVATE), None)
            assert item["code"] == entry.code
            for field in ("summary", "guidance", "documentation"):
                assert item[field].encode() == getattr(entry, field).encode()
            assert all(sentinel not in json.dumps(document) for sentinel in SENTINELS)


@pytest.mark.parametrize("field", ["selector", "siteSelector"])
def test_site_targeting_migration_is_field_neutral(field):
    with pytest.raises(ContentError) as caught:
        Site.from_data(
            {"subscription": "PrivateValue", "location": "eastus", field: "FactoryBlue"},
            source="private", default_name="private",
        )
    diagnostic = caught.value.diagnostic.document()
    assert diagnostic["code"] == "content.site-selector-misplaced"
    assert diagnostic["summary"] == "A Site contains Manifest targeting fields."
    assert diagnostic.get("schemaField") == ("siteSelector" if field == "siteSelector" else None)


@pytest.mark.parametrize("marker", ["TF_BUILD", "GITHUB_ACTIONS"])
def test_accepted_alias_warning_has_no_private_path(tmp_path, monkeypatch, caplog, marker):
    monkeypatch.delenv("SITEOPS_REDACT_OUTPUT", raising=False)
    monkeypatch.setenv(marker, "True")
    root = workspace(tmp_path, "valid")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + "siteSelector: environment=smoke\n")
    result = Orchestrator(root).validate(manifest)
    assert result.exit_code == 0
    assert "Manifest siteSelector is deprecated. Use selector on the Manifest." in caplog.text
    assert "FactoryBlue" not in caplog.text
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    caplog.clear()
    assert Orchestrator(root).validate(manifest).exit_code == 0
    assert str(manifest) in caplog.text


def test_document_role_and_occurrence_grouping(tmp_path):
    from siteops.reporting import render_plain_validation

    first = ContentDiagnostic("content.unknown-field", "FactoryBlue", "site", source_path=tmp_path / "first")
    second = ContentDiagnostic("content.unknown-field", "PrivateValue", "site", source_path=tmp_path / "second")
    result = ValidationResult((first, second))
    assert "(2 occurrences)" in render_plain_validation(result, redacted=True)
    assert result.document("local-private", __version__)["summary"]["diagnosticCount"] == 2
    assert len(result.document("local-private", __version__)["diagnostics"]) == 2
    manifest_entry = public_entry(resolve_member("content.unknown-field"), "manifest")
    assert "labels or properties" not in manifest_entry.guidance
    assert "labels or properties" in public_entry(resolve_member("content.unknown-field"), "site").guidance


def test_expected_fallback_and_programming_error_are_distinct(tmp_path):
    assert diagnostic_from_error(ValueError("FactoryBlue")).code == "content.other"
    root = workspace(tmp_path, "valid")
    with patch.object(Orchestrator, "load_manifest", side_effect=TypeError("programming defect")):
        with pytest.raises(TypeError, match="programming defect"):
            Orchestrator(root).validate(root / "manifests" / "smoke.yaml")


@pytest.mark.parametrize("change,code", [
    ({"labels": []}, "content.invalid-type"),
    ({"subscription": None}, "content.required-value-empty"),
    ({"subscription": None, "omit": True}, "content.required-field-missing"),
    ({"spec": {"inherits": "FactoryBlue"}}, "content.field-placement-invalid"),
])
def test_model_decision_table(change, code):
    data = {"subscription": "PrivateValue", "location": "eastus"}
    data.update(change)
    if data.pop("omit", False):
        data.pop("subscription")
    with pytest.raises(ContentError) as caught:
        Site.from_data(data, source="FactoryBlue", default_name="private")
    assert caught.value.diagnostic.code == code


def test_parser_schema_and_path_decision_table(tmp_path):
    with pytest.raises(Exception) as syntax:
        yamlio.load("mapping: [")
    assert diagnostic_from_error(syntax.value).code == "content.syntax-invalid"
    with pytest.raises(ContentError) as schema:
        _validate_resource({"apiVersion": "FactoryBlue"}, "Site", tmp_path / "site.yaml")
    assert schema.value.diagnostic.code == "content.schema-unsupported"
    with pytest.raises(ContentError) as condition:
        parse_when_condition("FactoryBlue", "step")
    assert condition.value.diagnostic.code == "content.condition-invalid"
    site = Site(name="private", subscription="private", resource_group="private", location="eastus")
    with pytest.raises(ContentError) as path:
        Orchestrator._require_selected_parameter_file("{{ site.name }}", "../FactoryBlue", site, tmp_path, "Step")
    assert path.value.diagnostic.code == "content.path-invalid"
    root = workspace(tmp_path, "valid")
    manifest = root / "manifests" / "smoke.yaml"
    with patch("builtins.open", side_effect=PermissionError("FactoryBlue")):
        assert Orchestrator(root).validate(manifest).diagnostics[0].code == "content.file-unreadable"
    assert Orchestrator(root).validate(root / "missing.yaml").diagnostics[0].code == "content.file-missing"


def test_legacy_selection_producer(tmp_path):
    root = workspace(tmp_path, "valid")
    orchestrator = Orchestrator(root)
    manifest = orchestrator.load_manifest(root / "manifests" / "smoke.yaml")
    site = Site(name="private", subscription="private", resource_group="private", location="eastus",
                properties={"sets": "FactoryBlue"})
    source = ParameterSource(path="parameters/{{ item }}.yaml", for_each="{{ site.properties.sets }}")
    with pytest.raises(ContentError) as caught:
        orchestrator._expand_manifest_parameter_source(source, manifest, site)
    assert caught.value.diagnostic.code == "content.legacy-resource-selection"


def _adapter():
    spec = importlib.util.spec_from_file_location("diagnostics_adapter", ROOT / "scripts" / "render-siteops-output.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform", ["ado", "github"])
@pytest.mark.parametrize("field,value", [
    ("summary", 17), ("code", "FactoryBlue\n"), ("guidance", "\x1b[31mPrivateValue"),
    ("documentation", "https://[broken"), ("summary", "x" * 513),
])
def test_adapter_rejects_unvalidated_fields(platform, field, value):
    document = ValidationResult((ContentDiagnostic("content.other", "private"),)).document("publishable", __version__)
    document["diagnostics"][0][field] = value
    logs, summary, valid = _adapter().render(document, "ValidationResult", 1, platform)
    assert valid
    assert "Diagnostic details are unavailable." in "\n".join(logs)
    assert "Validation result: invalid." in summary[0]
    assert "PrivateValue" not in "\n".join(logs)


def _scripts(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "render-siteops-output.py", tmp_path / "scripts" / "render-siteops-output.py")
    (tmp_path / "bin").mkdir()
    python = bash_path(Path(sys.executable), preserve_symlinks=True)
    write_executable(tmp_path / "bin" / "siteops",
                     f'#!/usr/bin/env bash\nexec "{python}" -B -m siteops "$@"\n')
    write_executable(tmp_path / "bin" / "python3",
                     f'#!/usr/bin/env bash\nexec "{python}" "$@"\n')


def test_validation_task_launchers_preserve_venv_interpreter(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(runtime)
    python = runtime / "Scripts" / "python.exe" if os.name == "nt" else runtime / "bin" / "python"
    environment = {**os.environ, "PYTHONPATH": ""}
    probe = subprocess.run(
        [str(python), "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=True, timeout=30,
    )
    packages = Path(probe.stdout.strip())
    assert packages.is_relative_to(runtime)
    (packages / "siteops.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'marker': 'venv-only', 'prefix': sys.prefix, 'executable': sys.executable}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "executable", str(python))
    _scripts(tmp_path)
    result = run_script("siteops", tmp_path, {"PYTHONPATH": ""})
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["marker"] == "venv-only"
    assert Path(document["prefix"]) == runtime
    assert Path(document["executable"]) == python
    result = run_script(
        "python3 -I -S -B -c 'import json, sys; "
        "print(json.dumps([sys.executable, sys.flags.isolated, sys.flags.no_site]))'",
        tmp_path, {"PYTHONPATH": "", "PYTHONSAFEPATH": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    executable, isolated, no_site = json.loads(result.stdout)
    assert Path(executable) == python
    assert isolated == no_site == 1


@pytest.mark.parametrize("platform", ["ado-template", "ado-sweep", "github-sweep"])
@pytest.mark.parametrize("case,code", [
    ("duplicate", "content.duplicate-key"), ("unknown", "content.unknown-field"), ("valid", None),
])
def test_real_validation_task_bodies(tmp_path, platform, case, code):
    _scripts(tmp_path)
    root = workspace(tmp_path, case)
    (tmp_path / "state").mkdir()
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + "selector: environment=smoke\n")
    summary = tmp_path / "summary.md"
    exports = {
        "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "SITEOPS_REDACT_OUTPUT": "1",
        "TF_BUILD": "True", "RUNNER_TEMP": bash_path(tmp_path / "state"),
        "STATE_PARENT": bash_path(tmp_path / "state"), "GITHUB_STEP_SUMMARY": bash_path(summary),
    }
    if platform == "ado-template":
        body = step(ROOT / ".pipelines" / "templates" / "siteops-validate.yaml", "Validate caller content")["script"]
        exports.update(WORKSPACE=bash_path(root), MANIFEST="manifests/smoke.yaml",
                       SELECTOR="environment=smoke", SITE_FILE="", TOOL_ROOT=bash_path(tmp_path))
    else:
        destination = tmp_path / "workspaces" / "iot-operations"
        shutil.copytree(root, destination)
        entry = destination / "manifests" / "first"
        entry.mkdir()
        shutil.move(destination / "manifests" / "smoke.yaml", entry / "manifest.yaml")
        (destination / "samples").mkdir()
        if platform == "ado-sweep":
            flow = yaml.safe_load((ROOT / ".pipelines" / "ci.yaml").read_text(encoding="utf-8"))
            body = next(s["script"] for job in flow["jobs"] for s in job.get("steps", [])
                        if s.get("displayName") == "Validate manifest structure")
            body = body.replace("$(Build.ArtifactStagingDirectory)", bash_path(tmp_path))
        else:
            flow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8"))
            body = next(s["run"] for s in flow["jobs"]["validate"]["steps"]
                        if s.get("name") == "Validate manifest structure")
            exports.update(WORKSPACE_DIR="workspaces/iot-operations",
                           MANIFESTS="workspaces/iot-operations/manifests/first/manifest.yaml")
    result = run_script(body, tmp_path, exports, shell_options=())
    assert result.returncode == (1 if code else 0), result.stdout + result.stderr
    output = result.stdout + result.stderr
    if code:
        assert code in output
    assert all(sentinel not in output for sentinel in SENTINELS)
    summary_file = summary if platform == "github-sweep" else tmp_path / "validation-summary.md"
    if summary_file.exists():
        text = summary_file.read_text(encoding="utf-8")
        assert all(sentinel not in text for sentinel in SENTINELS)
        if code:
            assert code in text
    assert not list((tmp_path / "state").iterdir())


@pytest.mark.parametrize("loader", ["load_site", "load_site_with_provenance"])
@pytest.mark.parametrize("case,code", [("unknown", "content.unknown-field"), ("valid", None)])
def test_site_wrapper_neighbors(tmp_path, loader, case, code):
    root = workspace(tmp_path, case)
    orchestrator = Orchestrator(root)
    if code:
        with pytest.raises(ContentError) as caught:
            getattr(orchestrator, loader)("selected")
        assert caught.value.diagnostic.code == code
        assert caught.value.diagnostic.source_path == root / "sites" / "selected.yaml"
        assert "Merged from:" in caught.value.diagnostic.private_detail
    else:
        assert getattr(orchestrator, loader)("selected")


@pytest.mark.parametrize("case,code", [
    ("duplicate", "content.duplicate-key"), ("unknown", "content.unknown-field"), ("valid", None),
])
def test_include_wrapper_neighbors(tmp_path, case, code):
    root = workspace(tmp_path, "valid")
    partial = root / "manifests" / "partial.yaml"
    text = "kind: Manifest\nsteps:\n  - name: smoke\n    template: templates/empty.template.json\n"
    if case == "duplicate":
        text += "customerA: PrivateValue\ncustomerA: FactoryBlue\n"
    elif case == "unknown":
        text += "customerA: PrivateValue\n"
    partial.write_text(text, encoding="utf-8")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text("kind: Manifest\nsteps:\n  - include: partial.yaml\n", encoding="utf-8")
    result = Orchestrator(root).validate(manifest)
    assert result.exit_code == int(code is not None)
    if code:
        assert result.diagnostics[0].code == code
        assert result.diagnostics[0].source_path == partial
    assert all(value not in json.dumps(result.document("publishable", __version__)) for value in SENTINELS)


@pytest.mark.parametrize("duplicate", [False, True])
def test_guided_yaml_wrapper_neighbors(tmp_path, duplicate):
    from siteops.guided_inputs import GuidedInputError, _read_yaml

    path = tmp_path / "customerA.yaml"
    path.write_text("customerA: PrivateValue\n" * (2 if duplicate else 1), encoding="utf-8")
    if duplicate:
        with pytest.raises(GuidedInputError) as caught:
            _read_yaml(path, label="Answers")
        issue = diagnostic_from_error(caught.value)
        assert issue.code == "content.duplicate-key"
        assert issue.source_path == path and issue.line == 2
        assert "schemaField" not in issue.document()
    else:
        assert _read_yaml(path, label="Answers") == {"customerA": "PrivateValue"}


@pytest.mark.parametrize("bad", [False, True])
def test_guided_syntax_wrapper_neighbors(tmp_path, bad):
    from siteops.guided_inputs import GuidedInputError, _read_yaml

    path = tmp_path / "customerA.yaml"
    path.write_bytes(b"customerA: [" if bad else b"customerA: []\n")
    if bad:
        with pytest.raises(GuidedInputError) as caught:
            _read_yaml(path, label="Answers")
        assert diagnostic_from_error(caught.value).code == "content.syntax-invalid"
    else:
        assert _read_yaml(path, label="Answers") == {"customerA": []}


@pytest.mark.parametrize("bad", [False, True])
def test_direct_site_schema_wrapper_neighbors(tmp_path, bad):
    from siteops.guided_inputs import GuidedInputError, load_direct_site

    root = workspace(tmp_path, "valid")
    path = root / "sites" / "selected.yaml"
    if bad:
        path.write_text(path.read_text().replace("siteops/v1", "FactoryBlue"))
        with pytest.raises(GuidedInputError) as caught:
            load_direct_site(path)
        assert diagnostic_from_error(caught.value).code == "content.schema-unsupported"
    else:
        assert load_direct_site(path)


@pytest.mark.parametrize("name", [
    "FactoryBlue", "AuthorizationFailedPlant", "SubscriptionNotFoundFactory",
    "InvalidTemplateCustomer", "customerA", "location", "idleTimeoutInMinutes",
    "::error::", "##vso[task.logissue type=error]", "%0A`[link]", "\x1b\u202e",
])
def test_unknown_authored_identifiers_are_not_schema_fields(name):
    with pytest.raises(ContentError) as caught:
        Site.from_data({"subscription": "private", "location": "eastus", "properties": {name: "private"},
                        "parameters": {name: "private"}, "customerA" + name: "private"},
                       source="FactoryBlue", default_name="private")
    item = caught.value.diagnostic.document()
    assert item["code"] == "content.unknown-field" and "schemaField" not in item
    assert "customerA" + name not in json.dumps(item)
    assert Site.from_data({"subscription": "private", "location": "eastus", "properties": {name: "private"},
                          "parameters": {name: "private"}}, source="private", default_name="private")


@pytest.mark.parametrize("command", [cmd_validate, cmd_plan, cmd_deploy])
def test_independent_plan_and_deploy_preparation_collects_errors(tmp_path, command, capsys, monkeypatch):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    root = workspace(tmp_path, "unknown")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + "parameters:\n  - parameters/missing.yaml\n")
    assert command(arguments(root), Orchestrator(root)) == 1
    document = json.loads(capsys.readouterr().out)
    assert {"content.unknown-field", "content.file-missing"} <= {d["code"] for d in document["diagnostics"]}


@pytest.mark.parametrize("version", ["1.0.0b1", "1.0.0b1+build.123.1.gabcdef123456", "FactoryBlue"])
@pytest.mark.parametrize("kind", ["validate", "plan", "deploy"])
def test_documentation_uses_fixed_index_independent_of_engine_version(version, kind):
    issue = ContentDiagnostic("content.duplicate-key", "private", "site")
    if kind == "validate":
        document = ValidationResult((issue,)).document("publishable", version)
    else:
        result = Orchestrator._invalid_preparation((issue,), PlanIntent.EXECUTABLE)
        document = (serialize_plan(result, PlanProjection.PUBLISHABLE, engine_version=version) if kind == "plan" else
                    json.loads(serialize_run_json(preparation_failure_result(result), PlanProjection.PUBLISHABLE,
                                                  engine_version=version)))
    assert document["engine"]["version"] == version
    for record in document["diagnostics"]:
        assert record["documentation"] == (
            "https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/diagnostics.md#"
            + record["code"].replace(".", "")
        )
        assert version not in record["documentation"]


def test_every_registered_diagnostic_has_a_permanent_code_heading():
    headings = {line[3:].strip() for line in (ROOT / "docs" / "diagnostics.md").read_text().splitlines()
                if line.startswith("## ")}
    assert set(REGISTRY) <= headings


def test_registry_rejects_a_forged_member():
    with pytest.raises(ValueError, match="registered"):
        public_document(RegistryCode("content.unknown-field"), "error")


@pytest.mark.parametrize("marker", ["TF_BUILD", "GITHUB_ACTIONS"])
@pytest.mark.parametrize("output", ["plain", "json"])
def test_alias_real_verbose_cli_is_path_free(tmp_path, monkeypatch, caplog, capsys, marker, output):
    monkeypatch.delenv("SITEOPS_REDACT_OUTPUT", raising=False)
    monkeypatch.setenv(marker, "True")
    caplog.set_level(logging.DEBUG, logger="siteops")
    root = workspace(tmp_path, "valid")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + "siteSelector: environment=smoke\n")
    monkeypatch.setattr(sys, "argv", ["siteops", "-v", "-w", str(root), "validate",
                                     str(manifest), "--output", output])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert "Manifest siteSelector is deprecated. Use selector on the Manifest." in caplog.text
    assert all(s not in captured.out + captured.err + caplog.text for s in SENTINELS)


@pytest.mark.parametrize("redacted,output,projection,exit_code", [
    (False, "json", None, 0), (True, "json", None, 0),
    (False, "json", "publishable", 0), (False, "json", "local-private", 0),
    (True, "json", "local-private", 1), (False, "plain", "publishable", 1),
])
def test_validate_output_decision_table(tmp_path, monkeypatch, capsys, redacted, output, projection, exit_code):
    root = workspace(tmp_path, "valid")
    args = ["siteops", "-w", str(root), "validate", "manifests/smoke.yaml", "--output", output]
    if projection:
        args += ["--projection", projection]
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", str(int(redacted)))
    monkeypatch.setattr(sys, "argv", args)
    guard = patch.object(cli, "open_command_context", side_effect=AssertionError("Content access"))
    with guard if exit_code else patch.object(Orchestrator, "_acquire_plan_templates",
                                               side_effect=AssertionError("Compilation")):
        with pytest.raises(SystemExit) as caught:
            cli.main()
    assert caught.value.code == exit_code
    captured = capsys.readouterr()
    if exit_code:
        assert captured.out == ""
    else:
        document = json.loads(captured.out)
        assert document["kind"] == "ValidationResult"
        assert document["projection"] == (projection or ("publishable" if redacted else "local-private"))


@pytest.mark.parametrize("code", [0, 1, 2, 17, 130])
@pytest.mark.parametrize("helper", [False, True])
def test_validate_template_exit_and_missing_helper(tmp_path, code, helper):
    _scripts(tmp_path)
    document = ValidationResult(
        () if code == 0 else (ContentDiagnostic("content.duplicate-key", "PrivateValue", "site"),),
        "complete",
    ).document("publishable", __version__)
    script = tmp_path / "scripts" / "render-siteops-output.py"
    if not helper:
        script.unlink()
    write_executable(tmp_path / "bin" / "siteops",
                     "#!/usr/bin/env bash\n" + f"echo '{json.dumps(document)}'\n"
                     + f"echo FactoryBlue >&2\nexit {code}\n")
    state = tmp_path / "state"
    state.mkdir()
    body = step(ROOT / ".pipelines" / "templates" / "siteops-validate.yaml", "Validate caller content")["script"]
    result = run_script(body, tmp_path, {
        "WORKSPACE": "private workspace", "MANIFEST": "private.yaml", "SELECTOR": "", "SITE_FILE": "",
        "STATE_PARENT": bash_path(state), "TOOL_ROOT": bash_path(tmp_path), "PYTHONSAFEPATH": "1",
    }, shell_options=())
    assert result.returncode == (1 if code == 0 and not helper else code)
    assert "FactoryBlue" not in result.stdout + result.stderr
    if code == 2:
        assert _adapter().MISMATCH in result.stdout
    assert not list(state.iterdir())


@pytest.mark.parametrize("raw", [
    b'{"kind":"FactoryBlue","kind":"ValidationResult"}', b'{"customerA":NaN}',
    b'{"customerA":' + b"9" * 100 + b"}", b"FactoryBlue", b"x" * (2 * 1024 * 1024 + 1),
], ids=["duplicate-key", "non-finite", "oversized-integer", "not-json", "oversized-file"])
def test_standalone_adapter_rejects_malformed_bounded_json(tmp_path, raw):
    result_path = tmp_path / "private.json"
    result_path.write_bytes(raw)
    helper = tmp_path / "render.py"
    shutil.copyfile(ROOT / "scripts" / "render-siteops-output.py", helper)
    (tmp_path / "siteops.py").write_text("raise AssertionError('Engine imported')")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(helper), str(result_path),
                             "--kind", "ValidationResult", "--exit-code", "17", "--platform", "ado"],
                            cwd=tmp_path, env={**os.environ, "PYTHONSAFEPATH": "1"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert _adapter().UNAVAILABLE in result.stdout
    assert result.stderr == ""
    assert all(s not in result.stdout for s in SENTINELS)


@pytest.mark.parametrize("platform", ["github", "ado"])
def test_adapter_encodes_delimiters_and_ignores_extra_fields(platform):
    document = ValidationResult((ContentDiagnostic("content.other", "private"),)).document("publishable", __version__)
    document["diagnostics"][0]["summary"] = "::error:: ##vso[task.logissue] %0A `code` [link]"
    document["diagnostics"][0]["private"] = "FactoryBlue"
    logs, summary, valid = _adapter().render(document, "ValidationResult", 1, platform)
    assert valid and ("%AZP250A" if platform == "ado" else "%250A") in "\n".join(logs)
    assert "\\`code\\`" in "\n".join(summary)
    assert all("FactoryBlue" not in line for line in logs + summary)


@pytest.mark.parametrize("change", ["bool-count", "wrong-kind", "private", "wrong-exit", "noncanonical-exit"])
def test_adapter_rejects_unsupported_core(change):
    document = ValidationResult((ContentDiagnostic("content.other", "private"),)).document("publishable", __version__)
    code = 1
    if change == "bool-count":
        document["summary"]["diagnosticCount"] = True
    elif change == "wrong-kind":
        document["kind"] = "DeploymentPlan"
    elif change == "private":
        document["projection"] = "local-private"
    elif change == "wrong-exit":
        document["exitCode"] = 0
    else:
        document["exitCode"] = code = 17
    logs, summary, valid = _adapter().render(document, "ValidationResult", code, "ado")
    assert not valid and summary == [_adapter().UNAVAILABLE]
    assert "Validation result:" not in "\n".join(logs)


def test_adapter_groups_occurrences_and_supports_old_fields():
    record = {"code": "content.other", "severity": "error", "summary": "An expected content error."}
    document = ValidationResult((ContentDiagnostic("content.other", "private"),
                                 ContentDiagnostic("content.other", "private"))).document("publishable", __version__)
    document["diagnostics"] = [record.copy(), record.copy()]
    logs, summary, valid = _adapter().render(document, "ValidationResult", 1, "ado")
    assert valid and "(2 occurrences)" in "\n".join(logs)
    assert document["summary"]["diagnosticCount"] == 2 and len(document["diagnostics"]) == 2
    assert len(logs) == 2


def test_warnings_do_not_invalidate_structural_validation():
    result = ValidationResult((ContentDiagnostic("content.other", "private", severity="warning"),))
    assert result.status == "valid" and result.exit_code == 0
    logs, summary, valid = _adapter().render(result.document("publishable", __version__),
                                             "ValidationResult", 0, "ado")
    assert valid and "Validation result: valid." in summary[0]


@pytest.mark.parametrize("bad", [False, True])
def test_manifest_identifier_wrapper_neighbors(tmp_path, bad):
    root = workspace(tmp_path, "valid")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + f"sites: ['{'../customerA' if bad else 'selected'}']\n")
    result = Orchestrator(root).validate(manifest)
    assert result.exit_code == int(bad)
    if bad:
        assert result.diagnostics[0].code == "content.path-invalid"
        assert result.diagnostics[0].source_path == manifest


@pytest.mark.parametrize("stage", ["prepare", "runtime"])
@pytest.mark.parametrize("bad", [False, True])
def test_wait_wrapper_neighbors(tmp_path, stage, bad):
    root = workspace(tmp_path, "valid")
    manifest_path = root / "manifests" / "wait.yaml"
    manifest_path.write_text(
        "kind: Manifest\nsites: [selected]\nsteps:\n"
        "  - name: wait\n    type: wait\n    condition:\n"
        "      type: arm-tag\n      resourceId: /subscriptions/sub/providers/Example/items/item\n"
        "      tagKey: state\n      expectedValue: ready\n",
    )
    orchestrator = Orchestrator(root)
    manifest = orchestrator.load_manifest(manifest_path)
    site = orchestrator.load_site("selected")
    result = orchestrator.build_plan(manifest_path)
    target = result.plan.targets[0]
    operation = target.operations[0]
    details, _ = orchestrator._prepare_operation_details(
        manifest.steps[0], site, manifest, {}, {}, operation.identity,
    )
    rejection = ContentError("FactoryBlue", code="content.required-value-empty", subject="manifest")
    from siteops.models import ArmTagCondition
    from siteops.planning import PlanValueResolutionError

    with patch("siteops.orchestrator.ArmTagCondition", side_effect=rejection if bad else ArmTagCondition):
        def call():
            if stage == "prepare":
                return orchestrator._prepare_operation_details(
                    manifest.steps[0], site, manifest, {}, {}, operation.identity,
                )
            return orchestrator._execute_prepared_operation(
                result.plan, target, replace(operation, details=details), "test", {}, PlanExecutionMode.APPLY,
            )

        expected = ContentError if stage == "prepare" else PlanValueResolutionError
        with patch.object(orchestrator.executor, "wait_for_condition", return_value=object()):
            if bad:
                with pytest.raises(expected) as caught:
                    call()
                assert caught.value.diagnostic.code == "content.required-value-empty"
            else:
                assert call()


@pytest.mark.parametrize("duplicate", [False, True])
def test_json_parameter_duplicate_has_no_schema_field(tmp_path, duplicate):
    root = workspace(tmp_path, "valid")
    manifest = root / "manifests" / "smoke.yaml"
    path = root / "parameters" / "customerA.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text('{"location":"FactoryBlue"' + (',"location":"PrivateValue"' if duplicate else "") + "}")
    manifest.write_text(manifest.read_text() + "parameters: [parameters/customerA.json]\n")
    result = Orchestrator(root).validate(manifest)
    assert result.exit_code == int(duplicate)
    if duplicate:
        record = result.document("publishable", __version__)["diagnostics"][0]
        assert record["code"] == "content.duplicate-key" and "schemaField" not in record


def test_legacy_aliases_publish_registry_bytes_and_ignore_all_producer_text():
    from siteops.diagnostics import _PUBLIC_NAMES

    for code in [*_PUBLIC_NAMES, "inputs.resource.customerA"]:
        issue = PlanDiagnostic(code, DiagnosticSeverity.ERROR, "FactoryBlue",
                               detail="PrivateValue", public_summary="customerA", serialized_detail="InvalidTemplateCustomer")
        object.__setattr__(issue, "guidance", "AuthorizationFailedPlant")
        object.__setattr__(issue, "documentation", "SubscriptionNotFoundFactory")
        result = PlanBuildResult(PlanStatus.INVALID, False, None, (issue,), PlanIntent.EXECUTABLE)
        documents = (
            serialize_plan(result, PlanProjection.PUBLISHABLE, engine_version=__version__),
            json.loads(serialize_run_json(preparation_failure_result(result),
                                         PlanProjection.PUBLISHABLE, engine_version=__version__)),
        )
        for document in documents:
            record = document["diagnostics"][0]
            entry = public_entry(resolve_member(code, document["kind"]), None)
            assert record["code"] == entry.code
            for field in ("summary", "guidance", "documentation"):
                assert record[field].encode("utf-8") == getattr(entry, field).encode("utf-8")
            assert all(s not in json.dumps(document) for s in SENTINELS)


@pytest.mark.parametrize("kind", ["content", "compilation", "capability"])
def test_review_local_plan_renders_distinct_causes_on_original_stream(kind, capsys):
    from siteops.planning import PlanNotExecutableError

    if kind == "content":
        result = Orchestrator._invalid_preparation(
            (ContentDiagnostic("content.file-missing", "First missing file."),
             ContentDiagnostic("content.file-missing", "Second missing file.")),
            PlanIntent.EXECUTABLE,
        )
        assert len(result.diagnostics) == 4
        assert "2 blocking problems" in PlanNotExecutableError(result).message(redacted=False)
    else:
        code = "compilation.failed" if kind == "compilation" else "capability.kubectl.missing"
        result = PlanBuildResult(PlanStatus.INVALID, False, None,
                                 (PlanDiagnostic(code, DiagnosticSeverity.ERROR, "Missing.", detail="First missing file."),),
                                 PlanIntent.EXECUTABLE)
    cli._write_plan_result(result, json_output=False, projection=PlanProjection.LOCAL_PRIVATE)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("Deployment plan")
    assert captured.out.count("First missing file.") == 1
    assert "Validation failed" not in captured.out
    if kind == "content":
        assert captured.out.count("Second missing file.") == 1


@pytest.mark.parametrize("redacted", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_review_sites_renders_retained_load_causes(tmp_path, monkeypatch, capsys, redacted, partial):
    root = workspace(tmp_path, "unknown")
    if partial:
        path = root / "sites" / "good.yaml"
        path.write_text((root / "sites" / "selected.yaml").read_text().split("FactoryBlue:")[0]
                        .replace("AuthorizationFailedPlant", "good"))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", str(int(redacted)))
    args = Namespace(name=None, selector=None, output="plain", show_sources=False)
    assert cli.cmd_sites(args, Orchestrator(root)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    if redacted:
        assert "content.unknown-field" in captured.err
        assert all(s not in captured.err for s in SENTINELS)
        assert "files named above" not in captured.err
    else:
        assert "unknown top-level key" in captured.err
        assert "FactoryBlue" in captured.err


def test_review_validate_no_match_keeps_targeting_category(tmp_path):
    root = workspace(tmp_path, "valid")
    result = Orchestrator(root).validate(root / "manifests" / "smoke.yaml", "environment=nope")
    assert result.diagnostics[0].code == "plan.targeting.empty"
    assert result.diagnostics[0].subject == "targeting"
    assert result.document("publishable", __version__)["diagnostics"][0]["code"] == "plan.targeting-empty"


@pytest.mark.parametrize("family,expected", [
    ("composition", "plan.composition-invalid"), ("parameters", "plan.parameter-selection-invalid"),
])
def test_review_redacted_family_fallback_keeps_category(monkeypatch, family, expected):
    from siteops.composition import CompositionError, report_composition_error
    from siteops.models import ParameterSelectionError
    from siteops.sanitize import report_parameter_selection_error

    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    error_type, report = ((CompositionError, report_composition_error) if family == "composition"
                          else (ParameterSelectionError, report_parameter_selection_error))
    assert report(error_type("FactoryBlue")).startswith(expected + ":")
    assert report(error_type("FactoryBlue", code="content.invalid-type")).startswith("content.invalid-type:")


@pytest.mark.parametrize("platform", ["ado", "github"])
def test_review_annotation_percent_round_trip(platform):
    original = (
        "Documentation: https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/diagnostics.md"
        "#contentduplicate-key. Literal %0A and %25."
    )
    annotation = _adapter()._annotation(original, platform, "error")
    decoded = annotation.replace("%0D", "\r").replace("%0A", "\n")
    decoded = decoded.replace("%AZP25" if platform == "ado" else "%25", "%")
    assert decoded.endswith(original)


def test_review_capability_aliases_keep_distinct_public_remedies():
    from siteops.planning import PlanNotExecutableError, render_plain_plan

    diagnostics = tuple(PlanDiagnostic(code, DiagnosticSeverity.ERROR, "FactoryBlue", public_summary="PrivateValue")
                        for code in ("capability.arm-control-plane.missing", "capability.kubectl.missing"))
    result = PlanBuildResult(PlanStatus.INVALID, False, None, diagnostics, PlanIntent.EXECUTABLE)
    documents = (serialize_plan(result, PlanProjection.PUBLISHABLE, engine_version=__version__),
                 json.loads(serialize_run_json(preparation_failure_result(result), PlanProjection.PUBLISHABLE,
                                               engine_version=__version__)))
    for document in documents:
        records = document["diagnostics"]
        assert len({record["summary"] for record in records}) == 2
        assert {record["code"] for record in records} == {
            "plan.capability-unavailable" if document["kind"] == "DeploymentPlan" else "run.capability-unavailable",
        }
        assert all(s not in json.dumps(document) for s in SENTINELS)
    assert "2 blocking problems" in PlanNotExecutableError(result).message(redacted=True)
    assert "2 occurrences" not in render_plain_plan(result, redacted=True)


def test_review_local_private_artifact_keeps_real_code(tmp_path, monkeypatch, capsys):
    from siteops.artifacts import ArtifactError

    root = workspace(tmp_path, "valid")
    args = arguments(root)
    args.projection = "local-private"
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    orchestrator = Orchestrator(root)
    with patch.object(orchestrator, "build_plan", side_effect=ArtifactError("FactoryBlue", code="path.alias")):
        assert cmd_deploy(args, orchestrator) == 1
    document = json.loads(capsys.readouterr().out)
    record = document["diagnostics"][0]
    assert record["code"] == "path.alias"
    assert record["summary"] == "The deployment run reported a diagnostic."
    assert "FactoryBlue" not in json.dumps(document)


@pytest.mark.parametrize("kind", ["DeploymentPlan", "DeploymentRun"])
def test_review_adapter_does_not_accept_unimplemented_document_kinds(kind):
    result = PlanBuildResult(PlanStatus.INVALID, False, None, intent=PlanIntent.EXECUTABLE)
    document = (serialize_plan(result, PlanProjection.PUBLISHABLE, engine_version=__version__)
                if kind == "DeploymentPlan" else
                json.loads(serialize_run_json(preparation_failure_result(result), PlanProjection.PUBLISHABLE,
                                              engine_version=__version__)))
    logs, summary, valid = _adapter().render(document, kind, 1, "ado")
    assert not valid and summary == [_adapter().UNAVAILABLE]


def test_review_resource_progress_retains_existing_surface(tmp_path, monkeypatch, capsys):
    from siteops.arm_resources import ArmResourceError

    root = workspace(tmp_path, "valid")
    contract = MagicMock()
    contract.bind.return_value = SimpleNamespace(resources={"cluster": object()})
    contract.resource_fields.return_value = [SimpleNamespace(name="cluster")]
    contract.resource_request.return_value = SimpleNamespace(ref=object(), required_facts=frozenset())
    reader = MagicMock(identity=SimpleNamespace(name="azure-cli"))
    reader.read.side_effect = ArmResourceError("FORBIDDEN")
    args = Namespace(input_file=None, input_values=None, command="plan", read_resources=False)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    with patch("siteops.cli.new_arm_reader", return_value=reader):
        with pytest.raises(cli.ResourceReadError):
            cli._resolve_typed_site(contract, args, manifest_path=root / "manifests" / "smoke.yaml",
                                    orchestrator=Orchestrator(root))
    assert "Reading declared resource cluster 1/1 using azure-cli." in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["yaml", "os"])
def test_review_include_preserves_library_exception_family(tmp_path, failure):
    from siteops import models

    root = workspace(tmp_path, "valid")
    partial = root / "manifests" / "partial.yaml"
    partial.write_text("kind: Manifest\nlocation: first\nlocation: second\nsteps: []\n")
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text("kind: Manifest\nsteps:\n  - include: partial.yaml\n")
    original = models._read_manifest_spec

    def read(path, **options):
        if failure == "os" and path == partial:
            raise PermissionError("FactoryBlue")
        return original(path, **options)

    expected = yamlio.DuplicateKeyError if failure == "yaml" else PermissionError
    with patch("siteops.models._read_manifest_spec", side_effect=read):
        with pytest.raises(expected):
            models.Manifest.from_file(manifest, workspace_root=root)


@pytest.mark.parametrize("family,phase,code,summary", [
    ("composition", "resolve", "plan.composition-invalid", "Resource composition failed."),
    ("parameters", "resolve", "plan.parameter-selection-invalid", "Parameter file selection failed."),
    ("parameters", "expand", "plan.parameter-selection-invalid", "Parameter file selection failed."),
])
@pytest.mark.parametrize("output", ["json", "plain"])
def test_validate_family_fallback_survives_publication(tmp_path, monkeypatch, capsys, family, phase, code, summary, output):
    from siteops.composition import CompositionError
    from siteops.models import ParameterSelectionError

    root = workspace(tmp_path, "valid")
    manifest = root / "manifests" / "smoke.yaml"
    if phase == "expand":
        manifest.write_text(manifest.read_text() + "parameters: [parameters/missing.yaml]\n")
    error = (CompositionError if family == "composition" else ParameterSelectionError)("FactoryBlue private-device")
    method = "_resolve_manifest_parameters" if phase == "resolve" else "_expand_manifest_parameter_source"
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    with patch.object(Orchestrator, method, side_effect=error):
        assert cmd_validate(arguments(root, output), Orchestrator(root)) == 1
    captured = capsys.readouterr()
    assert "FactoryBlue" not in captured.out + captured.err
    assert "private-device" not in captured.out + captured.err
    if output == "json":
        records = json.loads(captured.out)["diagnostics"]
        assert [record["code"] for record in records] == [code]
        assert records[0]["summary"] == summary
    else:
        assert code in captured.err and summary in " ".join(captured.err.split())
        assert "content.other" not in captured.err
