"""Direct release selection retains independent trust and no implicit Site inventory."""

import hashlib
import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

from siteops import cli, command_context
from siteops.planning import PlanStatus
from siteops.project import ProjectError
from siteops.results import RunResult
from siteops.workspace_acquisition import WorkspaceAcquisition
from tests.workspace_acquisition_helpers import make_source


@pytest.fixture
def direct(tmp_path, monkeypatch):
    fixture = make_source(
        tmp_path, provider="github-release/v1",
        reference="github:example/content", revision="a" * 40,
    )
    policy, root = tmp_path / "policy.json", tmp_path / "root.json"
    policy.write_bytes(b"independent-policy")
    root.write_bytes(b"independent-root")
    fixture.verifier.changes.update(
        policy_sha256=hashlib.sha256(policy.read_bytes()).hexdigest(),
        root_sha256=hashlib.sha256(root.read_bytes()).hexdigest(),
    )
    acquisition = WorkspaceAcquisition(fixture.cache, verify=fixture.verifier)
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    acquisition.publish(fixture.source, fixture.archive)
    requests = []

    def acquire(request, cache, policy_file, trusted_root, *, workspace=None, progress=None):
        assert request.reference == "github:example/content"
        assert request.release == "release-7"
        assert (policy_file, trusted_root) == (policy, root)
        requests.append(workspace)
        return fixture.source

    monkeypatch.setattr(command_context, "default_cache_root", lambda: fixture.cache.root)
    monkeypatch.setattr(command_context, "acquire_release", acquire)
    monkeypatch.setattr(command_context, "project_acquirer", lambda *args, **kwargs: acquisition)
    monkeypatch.setenv("SITEOPS_TEMP_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    monkeypatch.chdir(tmp_path)
    return fixture, policy, root, requests


def open_direct(policy, root, **kwargs):
    return command_context.open_command_context(
        workspace=None, project=None, command="plan", policy=policy,
        trusted_root=root, offline=False, discover=lambda _: None,
        source="github:example/content@release-7", **kwargs,
    )


def test_direct_content_has_no_project_pin_or_packaged_site_inventory(direct, tmp_path):
    fixture, policy, root, requests = direct
    with open_direct(policy, root) as context:
        assert context.project is None and context.pin is None
        assert context.source == fixture.source
        assert context.workspace == context.package.package_root / "workspace"
        temporary = context.site_root
        assert temporary != context.workspace
        assert temporary.is_dir() and list(temporary.iterdir()) == []
        assert context.revalidate is not None
        context.revalidate()
    assert not temporary.exists()
    assert not (tmp_path / "siteops.pin").exists()
    assert requests == [None]


def test_direct_selection_can_use_explicit_project_without_changing_its_pin(direct, tmp_path):
    fixture, policy, root, _ = direct
    project = tmp_path / "project"
    project.mkdir()
    pin = project / "siteops.pin"
    pin.write_bytes(b"unused existing pin")
    with command_context.open_command_context(
        workspace=None, project=project, command="plan", policy=policy,
        trusted_root=root, offline=False, discover=lambda _: None,
        source="github:example/content@release-7",
    ) as context:
        assert context.site_root == project
        assert context.source == fixture.source
        assert context.pin is None
    assert pin.read_bytes() == b"unused existing pin"


def test_direct_source_does_not_adopt_a_current_directory_pin(direct, tmp_path):
    _, policy, root, _ = direct
    (tmp_path / "siteops.pin").write_bytes(b"not selected")
    with open_direct(policy, root) as context:
        assert context.project is None and context.site_root != tmp_path


@pytest.mark.parametrize("changed", ["policy", "root"])
def test_authority_changed_after_preparation_is_rejected(direct, changed):
    _, policy, root, _ = direct
    with open_direct(policy, root) as context:
        (policy if changed == "policy" else root).write_bytes(b"changed")
        with pytest.raises(ValueError, match="changed"):
            context.revalidate()


def test_authority_expiry_is_checked_at_execution_boundary(direct, monkeypatch):
    _, policy, root, _ = direct
    with open_direct(policy, root) as context:
        later = context.package.verification.valid_until + timedelta(seconds=1)
        monkeypatch.setattr(command_context, "datetime", SimpleNamespace(now=lambda _: later))
        with pytest.raises(ValueError, match="expired"):
            context.revalidate()


def test_release_selection_is_explicit_and_precedes_acquisition(monkeypatch, tmp_path):
    monkeypatch.setattr(
        command_context, "WorkspaceCache",
        lambda *_: pytest.fail("Invalid selection cannot initialize content storage."),
    )
    for source in ("github:example/content", "approved"):
        monkeypatch.setattr(command_context, "read_source", lambda *a, **kw: SimpleNamespace(
            reference="github:example/content",
        ))
        with pytest.raises(ProjectError, match="release"):
            with command_context.open_command_context(
                workspace=None, project=None, command="plan", policy=tmp_path / "policy",
                trusted_root=tmp_path / "root", offline=False, discover=lambda _: None, source=source,
            ):
                pytest.fail("A direct source without an identified release was admitted.")


def test_source_alias_selects_only_consumer_enrollment(monkeypatch):
    calls = []

    def profile(name, *, require_valid=True):
        calls.append((name, require_valid))
        return SimpleNamespace(reference="github:example/content")

    monkeypatch.setattr(command_context, "read_source", profile)
    selected = command_context.resolve_source_request("approved@release-7")
    assert (selected.reference, selected.release, selected.approved_source) == (
        "github:example/content", "release-7", "approved",
    )
    assert calls == [("approved", True)]
    command_context.resolve_source_request("approved", for_inspection=True)
    assert calls[-1] == ("approved", False)
    with pytest.raises(ProjectError, match="approval"):
        command_context.resolve_source_request("approved@release-7", approved_source="other")


def invoke(arguments):
    with patch.object(sys, "argv", ["siteops", *arguments]):
        with pytest.raises(SystemExit) as stopped:
            cli.main()
    return stopped.value.code


def test_direct_source_cli_keeps_operator_inputs_outside_acquired_content(direct, tmp_path, capsys):
    fixture, policy, root, _ = direct
    site_file = tmp_path / "operator.yaml"
    site_file.write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "operator-target",
        "subscription": "00000000-0000-0000-0000-000000000001",
        "resourceGroup": "operator-rg", "location": "eastus",
    }), encoding="utf-8")
    assert invoke([
        "--trust-policy", str(policy), "--trusted-root", str(root),
        "plan", "storage", "--source", "github:example/content@release-7",
        "--site-file", str(site_file), "--describe", "--output", "json",
    ]) == 0
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert document["plan"]["manifest"]["targetSelection"] == "explicit-site"
    assert [target["name"] for target in document["plan"]["targets"]] == ["operator-target"]
    assert "github:example/content" in output.err
    assert not (tmp_path / "siteops.pin").exists()

    prepared = SimpleNamespace(
        executable=True, status=PlanStatus.PLANNED, plan=SimpleNamespace(targets=(object(),)),
    )
    engine = Mock()
    engine.build_plan.return_value = prepared
    engine.execute_plan.return_value = RunResult.from_sites((), elapsed=0)
    with patch.object(cli, "Orchestrator", return_value=engine) as factory:
        assert invoke([
            "--trust-policy", str(policy), "--trusted-root", str(root),
            "deploy", "storage", "--source", "github:example/content@release-7",
            "--site-file", str(site_file), "--yes", "--output", "json",
        ]) == 0
    options = factory.call_args.kwargs
    assert options["site_config_root"] != options["workspace"]
    assert options["materialized_package"].inspection.metadata.source_revision == fixture.source.source.revision
    assert engine.build_plan.call_args.kwargs["sites"][0].name == "operator-target"
    assert engine.execute_plan.call_args.args[0] is prepared
    assert not options["site_config_root"].exists()


def test_direct_cli_refuses_implicit_packaged_targeting_before_acquisition(monkeypatch, capsys):
    with patch.object(cli, "open_command_context", side_effect=AssertionError("No acquisition")):
        assert invoke([
            "deploy", "storage", "--source", "approved@release-7", "--yes",
        ]) == 2
    assert "explicit Site inputs" in capsys.readouterr().err
