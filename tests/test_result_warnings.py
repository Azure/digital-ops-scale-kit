"""Registered warnings reach JSON results and CI renderers without Site values."""

import ast
import importlib.util
import json
import logging
import os
import re
import sys
from pathlib import Path

import pytest

from siteops import __version__, cache_layout, cli, command_context
from siteops import executor as executor_module
from siteops.cache_filesystem import CacheError
from siteops.diagnostics import (
    CLEANUP_INCOMPLETE,
    LOCAL_REPRODUCTION,
    REGISTRY,
    WARNING_CODES,
    ContentDiagnostic,
    ValidationResult,
    public_document,
    recording_warnings,
    warn,
)
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    PlanBuildResult,
    PlanIntent,
    PlanProjection,
    PlanStatus,
    content_plan_diagnostic,
    serialize_plan_json,
)
from siteops.reporting import serialize_run_json, serialize_validation_json
from siteops.results import preparation_failure_result
from siteops.runtime import TEMP_DIR_ENV, cleanup_location
from siteops.source_metadata_cache import MetadataKey, SourceMetadataCache
from tests.test_content_diagnostics import SENTINELS, workspace

ROOT = Path(__file__).resolve().parents[1]
SELECTOR = "deprecation.manifest-site-selector"
REPEATED = "warning.manifest-site-repeated"


def _record(code, occurrences=None):
    subject = None if code == CLEANUP_INCOMPLETE else "manifest"
    record = public_document(REGISTRY[code].member, "warning", subject=subject)
    if occurrences is not None:
        record["occurrences"] = occurrences
    return record


def _adapter():
    spec = importlib.util.spec_from_file_location("warning_adapter", ROOT / "scripts" / "render-siteops-output.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deprecated_workspace(tmp_path, case="valid"):
    root = workspace(tmp_path, case)
    manifest = root / "manifests" / "smoke.yaml"
    manifest.write_text(manifest.read_text() + "sites: [selected, selected]\nsiteSelector: environment=smoke\n")
    return root, manifest


def test_recorder_groups_registered_warnings_in_first_order(caplog):
    logger = logging.getLogger("siteops.models")
    with recording_warnings() as recorder:
        warn(logger, REPEATED, "FactoryBlue repeated")
        warn(logger, SELECTOR, "%s uses siteSelector", "PrivateValue")
        warn(logger, REPEATED, "FactoryBlue repeated again")
        warn(logger, "warning.unregistered", "customerA")
        logger.warning("A warning without a registered code.")
        logging.getLogger("outside").warning("Outside the engine.", extra={"siteops_diagnostic": SELECTOR})
    warn(logger, SELECTOR, "After the command.")
    documents = recorder.documents()
    assert documents == [_record(REPEATED, 2), _record(SELECTOR)]
    assert "PrivateValue uses siteSelector" in caplog.text
    assert {record.filename for record in caplog.records} == {Path(__file__).name}
    assert all(sentinel not in json.dumps(documents) for sentinel in SENTINELS)
    assert recorder not in logging.getLogger("siteops").handlers


def _serializers(projection):
    error = ContentDiagnostic("content.unknown-field", "PrivateValue", "site")
    plan = PlanBuildResult(status=PlanStatus.INVALID, executable=False, plan=None, intent=PlanIntent.EXECUTABLE,
                           diagnostics=(content_plan_diagnostic(error),))
    run = preparation_failure_result(plan)
    return {
        "validate": lambda **extra: serialize_validation_json(
            ValidationResult((error,)), projection, engine_version=__version__, **extra),
        "valid": lambda **extra: serialize_validation_json(
            ValidationResult(), projection, engine_version=__version__, **extra),
        "plan": lambda **extra: serialize_plan_json(plan, projection, engine_version=__version__, **extra),
        "run": lambda **extra: serialize_run_json(run, projection, engine_version=__version__, **extra),
    }


@pytest.mark.parametrize("kind", ["validate", "valid", "plan", "run"])
@pytest.mark.parametrize("projection", list(PlanProjection))
def test_serializers_append_warnings_and_keep_the_outcome(kind, projection):
    serialize = _serializers(projection)[kind]
    assert serialize(warnings=()) == serialize()
    original = json.loads(serialize())
    warnings = [_record(SELECTOR, 2), _record(CLEANUP_INCOMPLETE)]
    document = json.loads(serialize(warnings=warnings))
    assert document["diagnostics"] == original["diagnostics"] + warnings
    for field in ("status", "exitCode", "executable", "kind", "projection"):
        assert document.get(field) == original.get(field)
    if "diagnosticCount" in original["summary"]:
        assert document["summary"]["diagnosticCount"] == len(document["diagnostics"])
    assert all(sentinel not in json.dumps(document) for sentinel in SENTINELS if sentinel != "PrivateValue")


@pytest.mark.parametrize("command,status,code", [
    ("validate", "valid", 0), ("plan", "planned", 0), ("deploy", "invalid", 1),
])
@pytest.mark.parametrize("projection", ["publishable", "local-private"])
def test_json_commands_publish_warnings_without_changing_exit(
    tmp_path, monkeypatch, capsys, caplog, command, status, code, projection,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    root, manifest = _deprecated_workspace(tmp_path, "unknown" if command == "deploy" else "valid")
    argv = ["siteops", "-w", str(root), command, "manifests/smoke.yaml", "--output", "json",
            "--projection", projection]
    argv += {"plan": ["--describe"], "deploy": ["--yes"]}.get(command, [])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == code
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == status and document.get("exitCode", code) == code
    warnings = [item for item in document["diagnostics"] if item["severity"] == "warning"]
    assert warnings[0] == _record(SELECTOR)
    if command != "deploy":
        assert warnings == [_record(SELECTOR), _record(REPEATED)]
    else:
        assert document["diagnostics"][0]["severity"] == "error"
    if "diagnosticCount" in document["summary"]:
        assert document["summary"]["diagnosticCount"] == len(document["diagnostics"])
    assert f"{manifest} uses deprecated `siteSelector:`" in caplog.text


def test_plain_validation_keeps_warnings_on_stderr(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    root, _ = _deprecated_workspace(tmp_path)
    monkeypatch.setattr(sys, "argv", ["siteops", "-w", str(root), "validate", "manifests/smoke.yaml"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0
    assert SELECTOR not in capsys.readouterr().out
    assert "uses deprecated `siteSelector:`" in caplog.text


@pytest.mark.parametrize("redacted", [False, True])
def test_repeated_site_warning_names_values_only_locally(tmp_path, monkeypatch, caplog, redacted):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", str(int(redacted)))
    root, manifest = _deprecated_workspace(tmp_path)
    manifest.write_text(manifest.read_text().replace("name: consumer-smoke", "name: customerA"))
    assert Orchestrator(root).validate(manifest).exit_code == 0
    if redacted:
        assert "A Manifest names the same Site more than once. Deploying it once." in caplog.text
        assert all(sentinel not in caplog.text for sentinel in SENTINELS)
        # The migration guide quotes the redacted text.
        guide = " ".join((ROOT / "docs" / "migrating.md").read_text(encoding="utf-8").split())
        assert "`A Manifest names the same Site more than once. Deploying it once.`" in guide
    else:
        assert "Manifest 'customerA' names the same Site more than once (selected)." in caplog.text


@pytest.mark.parametrize("platform", ["ado", "github"])
def test_renderer_groups_warning_occurrences_and_stays_successful(platform):
    adapter = _adapter()
    warnings = [_record(SELECTOR, 3), _record(CLEANUP_INCOMPLETE), _record(CLEANUP_INCOMPLETE)]
    document = json.loads(serialize_validation_json(
        ValidationResult(), PlanProjection.PUBLISHABLE, engine_version=__version__, warnings=warnings,
    ))
    logs, summary, valid = adapter.render(document, "ValidationResult", 0, platform)
    prefix = "##vso[task.logissue type=warning]" if platform == "ado" else "::warning::"
    assert valid and summary[0] == "Validation result: valid. Diagnostic records: 3."
    assert sum(line.startswith(f"{prefix}{SELECTOR} (3 occurrences): ") for line in logs) == 1
    assert sum(line.startswith(f"{prefix}{CLEANUP_INCOMPLETE} (2 occurrences): ") for line in logs) == 1
    assert "- " + adapter._markdown(f"{SELECTOR} (3 occurrences): {_record(SELECTOR)['summary']}") in summary


@pytest.mark.parametrize("platform", ["ado", "github"])
def test_pipeline_cleanup_warning_names_where_the_leftovers_are(platform):
    # The templates keep stderr private, so the annotation is all a pipeline reader sees.
    document = json.loads(serialize_validation_json(
        ValidationResult(), PlanProjection.PUBLISHABLE, engine_version=__version__,
        warnings=[_record(CLEANUP_INCOMPLETE)],
    ))
    logs, _, _ = _adapter().render(document, "ValidationResult", 0, platform)
    [annotation] = [line for line in logs if CLEANUP_INCOMPLETE in line]
    assert "temporary directory, which is `SITEOPS_TEMP_DIR` or the system default" in annotation
    assert "the cache's `staging` folder" in annotation
    assert "When no Site Ops command is running, remove the leftovers." in annotation


@pytest.mark.parametrize("value", [0, -1, True, "2", 2**63])
def test_renderer_refuses_an_invalid_occurrence_count(value):
    adapter = _adapter()
    document = ValidationResult().document("publishable", __version__)
    document["diagnostics"] = [{**_record(SELECTOR), "occurrences": value}]
    document["summary"]["diagnosticCount"] = 1
    logs, summary, valid = adapter.render(document, "ValidationResult", 0, "ado")
    assert valid and summary == ["Validation result: valid. Diagnostic records: 1.", adapter.UNAVAILABLE]


def test_every_engine_warning_names_a_registered_code():
    constants = {"CLEANUP_INCOMPLETE": CLEANUP_INCOMPLETE}
    used = []
    for path in sorted((ROOT / "siteops").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "warn":
                code = node.args[1]
                used.append(code.value if isinstance(code, ast.Constant) else constants.get(getattr(code, "id", "")))
    assert len(used) >= len(WARNING_CODES)
    assert set(used) == WARNING_CODES


def test_every_cleanup_warning_names_its_leftover():
    calls, literals, locations, reasons = [], [], [], []
    for path in sorted((ROOT / "siteops").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "warn"):
                continue
            code = node.args[1]
            if getattr(code, "value", None) == CLEANUP_INCOMPLETE:
                literals.append(f"{path.name}:{node.lineno}")
            if getattr(code, "id", None) == "CLEANUP_INCOMPLETE" or getattr(code, "value", None) == CLEANUP_INCOMPLETE:
                message = node.args[2]
                text = message.value if isinstance(message, ast.Constant) else ast.literal_eval(message)
                calls.append((f"{path.name}:{node.lineno}", text.count("%s") >= 1 and len(node.args) >= 4))
                location = node.args[3] if len(node.args) >= 4 else None
                if not (isinstance(location, ast.Call) and getattr(location.func, "id", None) == "cleanup_location"):
                    locations.append(f"{path.name}:{node.lineno}")
                described = any(
                    isinstance(argument, ast.Call)
                    and getattr(argument.func, "id", None) in {"describe_cleanup_error", "describe_os_error"}
                    for argument in node.args[4:]
                )
                if not described and "because" not in text:
                    reasons.append(f"{path.name}:{node.lineno}")
    assert len(calls) >= 12
    assert [site for site, named in calls if not named] == []
    assert literals == []
    # Every leftover is named by cleanup_location, which keeps a chosen folder out of redacted output.
    assert locations == []
    # Every warning also says why the removal failed.
    assert reasons == []


def _cleanup_warning(caplog, run):
    caplog.set_level(logging.WARNING, logger="siteops")
    with recording_warnings() as recorder:
        run()
    assert recorder.documents() == [_record(CLEANUP_INCOMPLETE)]
    [record] = [item for item in caplog.records if getattr(item, "siteops_diagnostic", None) == CLEANUP_INCOMPLETE]
    return record.getMessage()


@pytest.mark.parametrize("redacted", ["0", "1"])
def test_cache_staging_cleanup_names_the_staging_folder(tmp_path, monkeypatch, caplog, redacted):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", redacted)
    # The cache's own staging folder stays visible, even beneath a chosen folder that looks generated.
    layout = cache_layout.CacheLayout(tmp_path / "a123456789abcdefa123456789abcdef" / "cache")
    rename = Path.rename

    def refuse_publication(path, target):
        if path.parent.name == "staging":
            raise PermissionError(13, "Access is denied", str(path))
        return rename(path, target)

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(Path, "rename", refuse_publication)
    monkeypatch.setattr(cache_layout.shutil, "rmtree", refuse)

    def prepare():
        with pytest.raises(OSError):
            layout.prepare_namespace("metadata")

    message = _cleanup_warning(caplog, prepare)
    [leftover] = (layout.root / "staging").glob("metadata-*")
    assert message == (
        f"Cache staging cleanup could not be completed. Location: ...{os.sep}staging{os.sep}{leftover.name}. "
        "Reason: Access is denied."
    )
    assert str(tmp_path) not in message


@pytest.mark.parametrize("redacted", ["0", "1"])
def test_file_staging_cleanup_withholds_a_chosen_name_when_redacted(tmp_path, monkeypatch, caplog, redacted):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", redacted)
    target = tmp_path / "chosen" / "FactoryBlue.yaml"
    target.parent.mkdir()
    unlink = Path.unlink

    def refuse_target(path, *args, **kwargs):
        if path == target:
            raise PermissionError(13, "Access is denied", str(path))
        return unlink(path, *args, **kwargs)

    def interrupt(descriptor):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "unlink", refuse_target)
    monkeypatch.setattr(cache_layout.os, "fsync", interrupt)

    def write():
        with pytest.raises(OSError):
            cache_layout.write_new(target, b"site: value\n")

    message = _cleanup_warning(caplog, write)
    location = "withheld by output redaction" if redacted == "1" else f"...{os.sep}chosen{os.sep}FactoryBlue.yaml"
    assert message == f"Cache file staging cleanup could not be completed. Location: {location}. Reason: Access is denied."
    assert str(tmp_path) not in message


@pytest.mark.parametrize("folder", ["CustomerBlue-Tenant42", "a123456789abcdefa123456789abcdef"])
@pytest.mark.parametrize("redacted", ["0", "1"])
def test_unpublished_cache_cleanup_keeps_a_chosen_folder_out_of_redacted_output(
    tmp_path, monkeypatch, caplog, redacted, folder,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", redacted)
    chosen = tmp_path / folder
    rename = Path.rename

    def refuse_publication(path, target):
        if path.name.startswith(".siteops-cache-"):
            raise PermissionError(13, "Access is denied", str(path))
        return rename(path, target)

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(Path, "rename", refuse_publication)
    monkeypatch.setattr(cache_layout.shutil, "rmtree", refuse)

    def initialize():
        with pytest.raises(CacheError):
            cache_layout.CacheLayout(chosen / "cache")

    message = _cleanup_warning(caplog, initialize)
    [leftover] = chosen.glob(".siteops-cache-*")
    shown = leftover.name if redacted == "1" else f"{folder}{os.sep}{leftover.name}"
    assert message == (
        f"Cache staging cleanup could not be completed. Location: ...{os.sep}{shown}. Reason: Access is denied."
    )
    # Redacted output still says which leftover is left and why, so the warning stays actionable.
    assert leftover.name in message and "Reason: Access is denied." in message
    assert str(tmp_path) not in message


@pytest.mark.parametrize("redacted", ["0", "1"])
def test_generated_cache_file_cleanup_names_the_file_when_redacted(tmp_path, monkeypatch, caplog, redacted):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", redacted)
    cache = SourceMetadataCache(tmp_path / "CustomerBlue-Tenant42" / "cache")
    unlink = Path.unlink

    def refuse_record(path, *args, **kwargs):
        if path.name.startswith(".record-"):
            raise PermissionError(13, "Access is denied", str(path))
        return unlink(path, *args, **kwargs)

    def interrupt(descriptor):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "unlink", refuse_record)
    monkeypatch.setattr(cache_layout.os, "fsync", interrupt)

    def record():
        with pytest.raises((OSError, CacheError)):
            cache.get_or_fetch(MetadataKey("github-release/v1", "github:example/content", "release", "v1"),
                               lambda: b"{}")

    message = _cleanup_warning(caplog, record)
    [leftover] = (cache.root / "metadata" / "records").glob(".record-*")
    # Site Ops named the record and its folder, so redaction keeps both.
    assert message == (
        f"Cache file staging cleanup could not be completed. Location: ...{os.sep}records{os.sep}{leftover.name}. "
        "Reason: Access is denied."
    )


@pytest.mark.parametrize("redacted", ["0", "1"])
def test_temporary_cleanup_keeps_a_chosen_temp_folder_out_of_redacted_output(
    tmp_path, monkeypatch, caplog, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", redacted)
    chosen = tmp_path / "CustomerBlue-Tenant42"
    chosen.mkdir()
    monkeypatch.setenv(TEMP_DIR_ENV, str(chosen))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = executor_module.AzCliExecutor(workspace=workspace)
    scratch = executor.tmp_dir
    params = executor._write_params_file({"value": "private"}, "step", "FactoryBlue")
    configuration = chosen / "siteops-target-abcd1234"
    configuration.mkdir()

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Access is denied", str(path))

    unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", refuse)
    file_message = _cleanup_warning(caplog, lambda: executor._discard_scratch_file(params))
    monkeypatch.setattr(Path, "unlink", unlink)
    caplog.clear()
    monkeypatch.setattr(executor_module.shutil, "rmtree", refuse)
    directory_message = _cleanup_warning(caplog, executor.close)
    caplog.clear()
    monkeypatch.setattr(command_context.shutil, "rmtree", refuse)
    configuration_message = _cleanup_warning(
        caplog, lambda: command_context._remove_temporary_configuration(configuration),
    )
    folder = "" if redacted == "1" else f"CustomerBlue-Tenant42{os.sep}"
    # The scratch file's own folder is the engine scratch directory, so it stays visible.
    assert file_message == (
        f"Failed to remove engine scratch file ...{os.sep}{scratch.name}{os.sep}{params.name}: Access is denied"
    )
    assert directory_message == (
        f"Failed to remove engine scratch directory ...{os.sep}{folder}{scratch.name}: Access is denied"
    )
    assert configuration_message == (
        f"Temporary Site configuration ...{os.sep}{folder}siteops-target-abcd1234 could not be removed: "
        "Access is denied"
    )
    assert all(str(tmp_path) not in message for message in (file_message, directory_message, configuration_message))


@pytest.mark.parametrize("parent", ["staging", "a123456789abcdefa123456789abcdef", "CustomerBlue-Tenant42"])
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("redacted", ["0", "1"])
def test_redacted_cleanup_location_shows_a_folder_only_when_site_ops_owns_it(
    tmp_path, monkeypatch, parent, owned, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", redacted)
    # The folder's spelling never decides. Only the caller's ownership does.
    shown = f"{parent}{os.sep}leftover" if owned or redacted == "0" else "leftover"
    assert cleanup_location(tmp_path / parent / "leftover", parent_is_engine_owned=owned) == f"...{os.sep}{shown}"


def test_warning_guidance_names_no_file_and_line():
    # A warning's stderr message names its Manifest or files, never a line.
    assert [code for code in sorted(WARNING_CODES) if LOCAL_REPRODUCTION in _record(code)["guidance"]] == []


def test_documented_warning_record_matches_the_registry():
    text = (ROOT / "docs" / "run-output.md").read_text(encoding="utf-8")
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)]
    assert [item for item in examples if item.get("severity") == "warning"] == [_record(SELECTOR, 2)]
