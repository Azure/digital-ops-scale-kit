"""Bind release acceptance evidence to one exact candidate, attempt by attempt."""

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fleet_workflow import named_job  # noqa: E402
from release_acceptance import (  # noqa: E402
    ASSERTIONS,
    INSTALLER_CELLS,
    SCENARIOS,
    SITE_JOB,
    SLOT_SCENARIOS,
    aggregate,
    installer_cells,
    installer_row,
    matrix_cells,
    select_evidence,
    site_outcome,
)

from tests.acceptance_helpers import (  # noqa: E402
    RUN,
    SOURCE,
    Jobs,
    artifact,
    context,
    evidence_artifacts,
    fleet_receipt,
    installer_workflow,
    parse,
    producer_jobs,
    site_receipt,
    write_candidate,
)
from tests.actions_expressions import evaluate, truthy  # noqa: E402
from tests.shell_helpers import bash_path, run_script  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
NEEDS = {key: {"result": "success"} for key in ("fleet-request", "fleet", "prep", "site-groups", "e2e")}


def documents(selection, attempts=None, fleet_attempt=1, site_groups="ephemeral", fleet_groups="ephemeral"):
    attempts = attempts or {}
    values = {slot: site_receipt(selection, slot, attempts.get(slot, 1), groups=site_groups)
              for slot in ("disabled", "enabled", "existing")}
    values["fleet"] = fleet_receipt(selection, fleet_attempt, groups=fleet_groups)
    return values


def run_aggregate(selection, *, jobs=None, artifacts=None, evidence=None, producer=None, plan=None, needs=None,
                  environment="dev", workflow=None):
    plan = plan or {
        "kind": "ReleaseCandidate", "active": True, "source": SOURCE, "dryRun": selection["producer"]["preview"],
        "siteops": {"bundle": True},
        "workspaces": [{"id": "azure.iot-operations", "workspace": "workspaces/iot-operations"}],
    }
    return aggregate(
        parse(selection), plan=plan, bound=True,
        producer_jobs=producer_jobs() if producer is None else producer,
        run_jobs=(jobs or Jobs().acceptance()).values,
        artifacts=evidence_artifacts() if artifacts is None else artifacts,
        documents=documents(selection) if evidence is None else evidence,
        workflow=workflow or installer_workflow(), run=RUN, attempt=4, environment=environment,
        needs=NEEDS if needs is None else needs,
    )


@pytest.fixture
def selection(tmp_path):
    return write_candidate(tmp_path)


def status(document, scenario):
    return next(row for row in document["scenarios"] if row["scenario"] == scenario)


def test_complete_evidence_passes_with_a_closed_bound_receipt(selection):
    document = run_aggregate(selection)
    assert document["status"] == "passed"
    assert set(document) == {
        "apiVersion", "kind", "candidate", "acceptance", "environment", "transport", "publicRelease",
        "status", "scenarios", "workloadFunctionality", "secretMaterialization",
    }
    assert document["candidate"] == {
        "repository": SOURCE["repository"], "sourceCommit": SOURCE["commit"], "sourceRef": SOURCE["ref"],
        "caller": ".github/workflows/release.yaml", "producerRun": 42, "producerAttempt": 3, "preview": False,
        "admissionSha256": selection["artifacts"]["admission"]["sha256"],
        "planSha256": selection["artifacts"]["plan"]["sha256"],
        "inventorySha256": selection["artifacts"]["inventory"]["sha256"],
    }
    assert document["acceptance"] == {"run": RUN, "attempt": 4}
    assert (document["environment"], document["transport"], document["publicRelease"]) == (
        "dev", "prepublication", "not-observed")
    assert [row["scenario"] for row in document["scenarios"]] == list(SCENARIOS)
    for row in document["scenarios"]:
        assert row["status"] == "passed"
        assert row["assertions"] == list(ASSERTIONS[row["scenario"]])
        assert set(row) == {"scenario", "status", "groups", "assertions", "cleanup", "vaultPurge"}
        assert row["groups"] == ("not-applicable" if row["scenario"] == "installer" else "ephemeral")


@pytest.mark.parametrize(("site_groups", "fleet_groups"), [
    ("persistent", "persistent"), ("persistent", "ephemeral"), ("ephemeral", "persistent"),
])
def test_each_scenario_records_the_group_mode_its_own_configuration_selected(selection, site_groups, fleet_groups):
    evidence = documents(selection, site_groups=site_groups, fleet_groups=fleet_groups)
    document = run_aggregate(selection, evidence=evidence)
    assert document["status"] == "passed"
    assert [row["groups"] for row in document["scenarios"]] == [
        "not-applicable", site_groups, site_groups, site_groups, fleet_groups]
    evidence["enabled"]["groups"] = "operator"
    evidence["fleet"]["groups"] = None
    rejected = run_aggregate(selection, evidence=evidence)
    assert status(rejected, "site-combined")["status"] == "ambiguous"
    assert status(rejected, "fleet")["status"] == "ambiguous"
    assert {status(rejected, name)["groups"] for name in ("site-combined", "fleet")} == {"unknown"}


def test_older_passing_attempt_cannot_hide_a_later_failing_attempt(selection):
    jobs = Jobs().acceptance().add("Site case (site-existing-secretsync)", 2, "failure")
    evidence = documents(selection)
    evidence["existing"] = site_receipt(selection, "existing", 2, status="failed", assertions=[],
                                        cleanup="confirmed-absent")
    artifacts = [*evidence_artifacts(), artifact(f"site-outcome-{RUN}-2-existing", 700)]
    document = run_aggregate(selection, jobs=jobs, artifacts=artifacts, evidence=evidence)
    assert status(document, "site-existing-secretsync")["status"] == "failed"
    assert document["status"] == "failed"
    chosen = select_evidence(jobs.values, artifacts, run=RUN, commit=SOURCE["commit"])
    assert chosen["existing"]["attempt"] == 2 and chosen["existing"]["artifact"]["id"] == 700


def test_a_case_rerun_alone_is_governed_by_its_latest_passing_attempt(selection):
    jobs = Jobs().acceptance(enabled="failure").add("Site case (site-combined)", 2)
    artifacts = evidence_artifacts({"enabled": 2})
    evidence = documents(selection, {"enabled": 2})
    document = run_aggregate(selection, jobs=jobs, artifacts=artifacts, evidence=evidence)
    assert document["status"] == "passed"


@pytest.mark.parametrize(("fault", "expected"), [
    ("receipt-missing", "missing"),
    ("duplicate-job", "ambiguous"),
    ("duplicate-artifact", "ambiguous"),
    ("expired-artifact", "ambiguous"),
    ("skipped", "skipped"),
    ("cancelled", "cancelled"),
    ("other-candidate", "wrong-candidate"),
    ("other-attempt", "ambiguous"),
    ("residual-cleanup", "failed"),
    ("passed-without-assertion", "ambiguous"),
    ("unknown-field", "ambiguous"),
])
def test_site_rows_fail_closed(selection, fault, expected):
    jobs, artifacts, evidence = Jobs().acceptance(), evidence_artifacts(), documents(selection)
    receipt = evidence["enabled"]
    if fault == "receipt-missing":
        evidence["enabled"] = None
    elif fault == "duplicate-job":
        jobs.add("Site case (site-combined)", 1)
    elif fault == "duplicate-artifact":
        artifacts.append(artifact(f"site-outcome-{RUN}-1-enabled", 701))
    elif fault == "expired-artifact":
        artifacts[1]["expired"] = True
    elif fault in {"skipped", "cancelled"}:
        jobs = Jobs().acceptance(enabled=fault)
        evidence["enabled"] = None
    elif fault == "other-candidate":
        receipt["context"]["admissionSha256"] = "f" * 64
    elif fault == "other-attempt":
        receipt["context"]["attempt"] = 2
    elif fault == "residual-cleanup":
        receipt.update(status="failed", cleanup="residual")
    elif fault == "passed-without-assertion":
        receipt["assertions"] = receipt["assertions"][1:]
    else:
        receipt["release"] = "private-marker"
    document = run_aggregate(selection, jobs=jobs, artifacts=artifacts, evidence=evidence)
    assert status(document, "site-combined")["status"] == expected
    assert document["status"] == "failed"
    assert "private-marker" not in json.dumps(document)


def test_vault_purge_failure_is_recorded_without_failing_the_gate(selection):
    evidence = documents(selection)
    evidence["existing"]["vaultPurge"] = "failed"
    document = run_aggregate(selection, evidence=evidence)
    assert document["status"] == "passed"
    assert status(document, "site-existing-secretsync")["vaultPurge"] == "failed"


@pytest.mark.parametrize("partial", [False, True])
def test_fleet_must_pass_within_one_attempt(selection, partial):
    jobs = Jobs().acceptance(fleet_attempt=2 if not partial else 1)
    if partial:
        jobs.add("Fleet controller", 2, prefix="Fleet / ").add("Require complete fleet acceptance", 2,
                                                                 prefix="Fleet / ")
    artifacts = evidence_artifacts(fleet_attempt=2) + ([artifact(f"fleet-acceptance-{RUN}-1", 601)] if partial else [])
    evidence = documents(selection, fleet_attempt=2)
    document = run_aggregate(selection, jobs=jobs, artifacts=artifacts, evidence=evidence)
    assert status(document, "fleet")["status"] == ("ambiguous" if partial else "passed")
    assert select_evidence(jobs.values, artifacts, run=RUN, commit=SOURCE["commit"])["fleet"]["partial"] is partial


@pytest.mark.parametrize(("change", "expected"), [
    ({"status": "failed"}, "failed"),
    ({"cleanup": "residual"}, "failed"),
    ({"context": "other"}, "wrong-candidate"),
])
def test_fleet_receipt_requires_the_exact_passing_shape(selection, change, expected):
    evidence = documents(selection)
    if change.get("context"):
        evidence["fleet"]["context"]["inventorySha256"] = "f" * 64
    else:
        evidence["fleet"].update(change)
    assert status(run_aggregate(selection, evidence=evidence), "fleet")["status"] == expected


def test_missing_needs_or_a_failed_wrapper_job_fails_the_run(selection):
    for needs in ({**NEEDS, "e2e": {"result": "failure"}}, {k: v for k, v in NEEDS.items() if k != "fleet"}, None):
        document = run_aggregate(selection, needs=needs if needs is not None else "invalid")
        assert document["status"] == "failed"


def test_receipts_record_preview_and_environment_without_authorizing_them(tmp_path):
    selection = write_candidate(tmp_path, preview=True)
    document = run_aggregate(selection, environment="sandbox")
    assert document["candidate"]["preview"] is True
    assert document["environment"] == "sandbox"
    with pytest.raises(ValueError):
        run_aggregate(selection, environment="dev\nprivate")


def test_plan_selects_the_required_set_and_fails_closed_without_the_workspace(tmp_path):
    selection = write_candidate(tmp_path)
    plan = {"kind": "ReleaseCandidate", "active": True, "source": SOURCE, "dryRun": False,
            "siteops": {"bundle": False}, "workspaces": [{"id": "azure.iot-operations",
                                                          "workspace": "workspaces/iot-operations"}]}
    document = run_aggregate(selection, plan=plan, producer=[])
    assert status(document, "installer")["status"] == "not-applicable"
    assert document["status"] == "passed"
    plan["workspaces"] = []
    document = run_aggregate(selection, plan=plan, producer=[])
    assert all(status(document, name)["status"] == "missing" for name in SCENARIOS[1:])
    assert document["status"] == "failed"
    plan["source"] = {**SOURCE, "commit": "c" * 40}
    assert {row["status"] for row in run_aggregate(selection, plan=plan)["scenarios"]} == {"wrong-candidate"}


def test_github_matrix_include_rules_match_the_documented_example():
    cells = matrix_cells({
        "fruit": ["apple", "pear"], "animal": ["cat", "dog"],
        "include": [{"color": "green"}, {"color": "pink", "animal": "cat"}, {"fruit": "apple", "shape": "circle"},
                    {"fruit": "banana"}, {"fruit": "banana", "animal": "cat"}],
    })
    assert cells == [
        {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
        {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
        {"fruit": "pear", "animal": "cat", "color": "pink"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
        {"fruit": "banana"}, {"fruit": "banana", "animal": "cat"},
    ]
    assert matrix_cells({"os": ["a", "b"], "exclude": [{"os": "a"}]}) == [{"os": "b"}]
    for invalid in ({"os": "${{ fromJSON(inputs.cells) }}"}, {"os": []}, {"os": ["a"], "include": [{"x": 1}]}):
        with pytest.raises(ValueError):
            matrix_cells(invalid)


def test_installer_contract_names_every_required_cell():
    assert installer_cells(installer_workflow()) == list(INSTALLER_CELLS)
    # Without a base account key, the standard user entry merges into the existing cell.
    merged = installer_cells(installer_workflow(account_key=False))
    assert "Qualify bundle (windows-2025, Python 3.11)" not in merged
    assert installer_row(producer_jobs(), installer_workflow(account_key=False))["status"] == "missing"
    assert installer_row(producer_jobs(), installer_workflow(cells=False))["status"] == "missing"
    with pytest.raises(ValueError):
        installer_cells({"jobs": {"qualify": {"strategy": {"matrix": {"os": ["x"], "python": ["3.11"],
                                                                       "shell": ["bash"]}}}}})


def test_candidate_distribution_matrix_is_the_production_source():
    workflow = yaml.safe_load((WORKFLOWS / "_siteops-distribution.yaml").read_text(encoding="utf-8"))
    names = installer_cells(workflow)
    assert set(INSTALLER_CELLS[:10]) <= set(names)
    step = next(step for step in yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8"))[
        "jobs"]["acceptance"]["steps"] if step.get("name") == "Require every scenario for this candidate")
    assert "--workflow .github/workflows/_siteops-distribution.yaml" in step["run"]


@pytest.mark.parametrize(("cell", "conclusion", "expected"), [
    (None, None, "passed"),
    ("Qualify bundle (windows-2025, Python 3.11, standard user)", "failure", "failed"),
    ("Qualify bundle (ubuntu-26.04, Python 3.11)", "skipped", "skipped"),
    ("Attest bundle", "cancelled", "cancelled"),
    ("Qualify bundle (ubuntu-24.04, Python 3.14)", "missing", "missing"),
    ("Build bundle", "duplicate", "ambiguous"),
])
def test_installer_row_requires_every_cell_build_attest_and_review(cell, conclusion, expected):
    values = producer_jobs(**({cell: conclusion} if conclusion not in {None, "missing", "duplicate"} else {}))
    if conclusion == "missing":
        values = [job for job in values if not job["name"].endswith(cell)]
    elif conclusion == "duplicate":
        values.append({**values[0], "id": 999})
    result = installer_row(values, installer_workflow())
    assert result["status"] == expected
    assert ("windows-standard-user" in result["assertions"]) is (cell != INSTALLER_CELLS[-1])


@pytest.mark.parametrize("groups", ["ephemeral", "persistent"])
@pytest.mark.parametrize("slot", ["disabled", "enabled", "existing"])
def test_site_outcome_records_only_assertions_from_successful_steps(selection, slot, groups):
    outcomes = {step: "success" for step in ("engine", "guided", "routes", "deploy", "existing-vault",
                                             "existing-enable", "readiness")}
    expected = context(selection, 1)
    qualification = {"kind": "WorkspaceEngineQualification", "engineVersion": "1.0.0b7",
                     "project": {"workspace": "workspaces/iot-operations", "sourceReleaseObservation": "not-performed"}}
    readiness = {"kind": "PublishedPackageReadiness", "aioInstances": 1, "activePods": 3, "readyActivePods": 3,
                 "instanceCustomResources": 1, "secretSyncEnabled": slot != "disabled"}
    if slot != "disabled":
        readiness.update(spcBoundToInstance=True, vaultBinding="existing" if slot == "existing" else "created")
    cleanup = {"apiVersion": "siteops.release.fleet/v1", "kind": "SiteCleanup", "groups": groups,
               "context": expected, "scopeKey": "d" * 64,
               "slots": {slot: {"state": "absent", "reason": "confirmed-absent"}},
               "status": "complete", "operationExit": 0, "vaultPurge": "failed"}

    def record(**changes):
        values = {"outcomes": outcomes, "qualification": qualification, "readiness": readiness,
                  "cleanup": cleanup, **changes}
        return site_outcome(expected, "d" * 64, slot, values["outcomes"], qualification=values["qualification"],
                            readiness=values["readiness"], cleanup=values["cleanup"], preflight="success",
                            groups=groups)

    passed = record()
    assert passed["status"] == "passed" and passed["vaultPurge"] == "failed"
    assert passed["groups"] == groups
    other_mode = copy.deepcopy(cleanup)
    other_mode["groups"] = "ephemeral" if groups == "persistent" else "persistent"
    assert record(cleanup=other_mode)["cleanup"] == "unknown"
    assert passed["assertions"] == list(ASSERTIONS[passed["scenario"]])
    assert record(outcomes={**outcomes, "deploy": "failure"})["status"] == "failed"
    assert "deploy-succeeded" not in record(outcomes={**outcomes, "deploy": "failure"})["assertions"]
    assert record(readiness=None)["status"] == "failed"
    for key in ("aioInstances", "activePods", "readyActivePods", "instanceCustomResources"):
        for value in (None, True, "3", 0, -1):
            assert "aio-readiness" not in record(readiness={**readiness, key: value})["assertions"]
    for count in (1, 2, 4):
        # A Ready neighbor cannot stand in for another active pod.
        unready = record(readiness={**readiness, "readyActivePods": count})
        assert unready["status"] == "failed" and "aio-readiness" not in unready["assertions"]
    missing_count = {key: value for key, value in readiness.items() if key != "readyActivePods"}
    assert "aio-readiness" not in record(readiness=missing_count)["assertions"]
    assert record(cleanup=None)["cleanup"] == "unknown"
    residual = copy.deepcopy(cleanup)
    residual.update(status="incomplete", slots={slot: {"state": "residual", "reason": "deletion-pending"}})
    assert record(cleanup=residual)["cleanup"] == "residual"
    other = copy.deepcopy(cleanup)
    other["context"] = {**expected, "attempt": 2}
    assert record(cleanup=other)["cleanup"] == "unknown"
    assert record(qualification={**qualification, "project": {}})["status"] == "failed"
    if slot != "disabled":
        assert record(readiness={**readiness, "vaultBinding": "other"})["status"] == "failed"
    assert site_outcome(expected, "d" * 64, slot, {}, qualification=None, readiness=None, cleanup=None,
                        preflight="failure", groups=groups)["cleanup"] == "not-attempted"


@pytest.mark.parametrize(("active", "ready", "expected"), [
    pytest.param(2, 2, True, id="all-active-ready"),
    pytest.param(2, 1, False, id="crash-loop-with-ready-neighbor"),
    pytest.param(2, 3, False, id="excess-ready-count"),
    pytest.param(0, 0, False, id="no-active-pods"),
])
def test_site_readiness_requires_matching_active_counts(active, ready, expected):
    readiness = {"kind": "PublishedPackageReadiness", "aioInstances": 1,
                 "activePods": active, "readyActivePods": ready, "instanceCustomResources": 1}
    result = site_outcome({}, "d" * 64, "disabled", {"readiness": "success"},
                          qualification=None, readiness=readiness, cleanup=None,
                          preflight="failure", groups="persistent")
    assert ("aio-readiness" in result["assertions"]) is expected


def _cli(tmp_path, selection, *, downloads="success", evidence=None, jobs=None, artifacts=None):
    root = tmp_path
    (root / "producer-jobs.json").write_text(json.dumps([{"total_count": len(producer_jobs()),
                                                          "jobs": producer_jobs()}]))
    run_jobs = jobs or Jobs().acceptance()
    (root / "run-jobs.json").write_text(json.dumps(run_jobs.pages()))
    (root / "run-artifacts.json").write_text(json.dumps([{"artifacts": artifacts or evidence_artifacts()}]))
    for key, document in (evidence or documents(selection)).items():
        directory = root / "evidence" / key
        directory.mkdir(parents=True)
        (directory / ("fleet-acceptance.json" if key == "fleet" else "outcome.json")).write_text(json.dumps(document))
    workflow = root / "distribution.yaml"
    workflow.write_text(yaml.safe_dump(installer_workflow()))
    environment = {
        **os.environ, "FLEET_CANDIDATE": json.dumps(selection), "GITHUB_REPOSITORY": SOURCE["repository"],
        "GITHUB_SHA": SOURCE["commit"], "GITHUB_REF": SOURCE["ref"], "GITHUB_RUN_ID": str(RUN),
        "GITHUB_RUN_ATTEMPT": "4", "ACCEPTANCE_ENVIRONMENT": "dev", "ACCEPTANCE_NEEDS": json.dumps(NEEDS),
        "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
        **{f"DOWNLOAD_{key.upper()}": downloads for key in ("disabled", "enabled", "existing", "fleet")},
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "aggregate-release-acceptance.py"), "aggregate", "--root", str(root),
         "--workflow", str(workflow), "--output", str(root / "release-acceptance.json")],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    output = root / "release-acceptance.json"
    return result, json.loads(output.read_text()) if output.exists() else None


def test_aggregate_entrypoint_writes_a_receipt_for_passing_and_failing_runs(tmp_path, selection):
    result, document = _cli(tmp_path, selection)
    assert result.returncode == 0, result.stderr
    assert document["status"] == "passed"
    assert "| fleet | passed | ephemeral | confirmed-absent | not-applicable |" in (
        tmp_path / "summary.md").read_text()


def test_aggregate_entrypoint_ignores_evidence_from_a_failed_download(tmp_path, selection):
    result, document = _cli(tmp_path, selection, downloads="failure")
    assert result.returncode == 1
    assert {row["status"] for row in document["scenarios"][1:]} == {"missing"}


@pytest.mark.parametrize("unavailable", ["admission", "plan"])
def test_aggregate_entrypoint_binds_a_missing_result_when_selection_did_not_complete(
    tmp_path, selection, unavailable,
):
    # A failed selection skips both downloads. The run still records a failing receipt for this candidate.
    shutil.rmtree(tmp_path / unavailable)
    result, document = _cli(tmp_path, selection)
    assert result.returncode == 1
    assert document["status"] == "failed"
    assert document["candidate"]["admissionSha256"] == selection["artifacts"]["admission"]["sha256"]
    assert {row["status"] for row in document["scenarios"]} == {"missing"}
    assert [(row["groups"], row["vaultPurge"]) for row in document["scenarios"]] == [
        ("not-applicable", "not-applicable"), *[("unknown", "not-attempted")] * 3, ("unknown", "not-applicable"),
    ]


def test_aggregate_records_a_mismatched_admission_as_another_candidate(tmp_path, selection):
    (tmp_path / "admission" / "receipt.json").write_text("{}")
    result, document = _cli(tmp_path, selection)
    assert result.returncode == 1
    assert {row["status"] for row in document["scenarios"]} == {"wrong-candidate"}


def test_aggregate_entrypoint_names_a_partial_fleet_rerun(tmp_path, selection):
    jobs = Jobs().acceptance().add("Fleet controller", 2, prefix="Fleet / ")
    result, document = _cli(tmp_path, selection, jobs=jobs)
    assert result.returncode == 1
    assert document["status"] == "failed"
    assert "Use Re-run all jobs" in result.stderr


def test_aggregate_entrypoint_writes_nothing_for_another_source(tmp_path, selection):
    selection = copy.deepcopy(selection)
    selection["source"]["commit"] = "c" * 40
    result, document = _cli(tmp_path, selection)
    assert result.returncode == 1 and document is None


def test_acceptance_run_is_bound_named_and_always_aggregated():
    workflow = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8"))
    assert workflow["run-name"] == (
        "${{ inputs.scenario == 'release-acceptance' && 'Release acceptance' || 'E2E Tests' }}"
    )
    job = workflow["jobs"]["acceptance"]
    assert job["needs"] == ["fleet-request", "fleet", "prep", "site-groups", "e2e"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert "environment" not in job
    steps = job["steps"]
    names = [step.get("name") or step.get("id") or step.get("uses") for step in steps]
    bind = steps[0]
    # Binding comes before checkout and tool setup, so neither can stop a run from recording its result.
    assert bind["id"] == "bind" and bind["shell"] == "python"
    assert "if" not in bind and "env" not in bind and "uses" not in bind
    assert "scripts/" not in bind["run"] and "import json, os, re" in bind["run"]
    aggregate_step = next(step for step in steps if step.get("name") == "Require every scenario for this candidate")
    assert aggregate_step["env"]["ACCEPTANCE_NEEDS"] == "${{ toJSON(needs) }}"
    fallback = next(step for step in steps if step.get("name") == "Keep a failing result when aggregation wrote none")
    upload = steps[-1]
    assert names.index("Require every scenario for this candidate") < steps.index(fallback) < len(steps) - 1
    assert upload["with"]["name"] == (
        "release-acceptance-${{ github.run_id }}-${{ github.run_attempt }}-${{ steps.bind.outputs.admission-sha }}"
    )
    for key in ("disabled", "enabled", "existing", "fleet"):
        download = next(step for step in steps if step.get("id") == f"download-{key}")
        assert download["with"]["artifact-ids"] == f"${{{{ steps.evidence.outputs.{key}-id }}}}"
        assert download["with"]["path"] == f"${{{{ runner.temp }}}}/acceptance/evidence/{key}"
        assert download["with"]["run-id"] == "${{ github.run_id }}"
        assert "pattern" not in download["with"] and "merge-multiple" not in download["with"]
        assert aggregate_step["env"][f"DOWNLOAD_{key.upper()}"] == f"${{{{ steps.download-{key}.outcome }}}}"
        assert names.index(f"download-{key}") < names.index("Require every scenario for this candidate")


@pytest.mark.parametrize(("selected", "cancelled", "expected"), [
    pytest.param("success", False, True, id="candidate-selected"),
    pytest.param("failure", False, False, id="selection-failed"),
    pytest.param("skipped", False, False, id="selection-skipped"),
    pytest.param("cancelled", False, False, id="selection-cancelled"),
    pytest.param("success", True, True, id="retain-evidence-on-cancelled-run"),
    pytest.param("failure", True, False, id="cancelled-without-selection"),
])
def test_acceptance_selects_evidence_only_after_candidate_selection(selected, cancelled, expected):
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["acceptance"]
    step = next(step for step in job["steps"] if step.get("id") == "evidence")
    contexts = {"steps": {"select": {"outcome": selected}}, "needs": {"previous": {"result": "failure"}}}
    assert truthy(evaluate(step["if"], contexts, cancelled=cancelled)) is expected


@pytest.mark.parametrize("slot", [
    pytest.param("disabled", id="disabled-evidence"),
    pytest.param("enabled", id="enabled-evidence"),
    pytest.param("existing", id="existing-evidence"),
    pytest.param("fleet", id="fleet-evidence"),
])
@pytest.mark.parametrize(("artifact_id", "cancelled", "expected"), [
    pytest.param("123", False, True, id="selected-artifact"),
    pytest.param("", False, False, id="no-artifact"),
    pytest.param(None, False, False, id="missing-output"),
    pytest.param("123", True, True, id="selected-artifact-after-cancellation"),
    pytest.param("", True, False, id="cancelled-without-artifact"),
])
def test_acceptance_downloads_only_the_selected_scenario_artifact(slot, artifact_id, cancelled, expected):
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["acceptance"]
    step = next(step for step in job["steps"] if step.get("id") == f"download-{slot}")
    outputs = {f"{key}-id": "other-artifact" for key in SLOT_SCENARIOS if key != slot}
    if artifact_id is not None:
        outputs[f"{slot}-id"] = artifact_id
    contexts = {"steps": {"evidence": {"outputs": outputs}}, "needs": {"previous": {"result": "failure"}}}
    assert truthy(evaluate(step["if"], contexts, cancelled=cancelled)) is expected


@pytest.mark.parametrize(("bound", "selected", "cancelled", "expected"), [
    pytest.param("success", "success", False, True, id="bound-and-selected"),
    pytest.param("success", "failure", False, True, id="selection-failed-after-binding"),
    pytest.param("success", "skipped", False, True, id="selection-skipped-after-binding"),
    pytest.param("failure", "success", False, False, id="binding-failed"),
    pytest.param("skipped", "success", False, False, id="binding-skipped"),
    pytest.param("cancelled", "success", False, False, id="binding-cancelled"),
    pytest.param("success", "failure", True, True, id="cancelled-after-binding"),
    pytest.param("success", "skipped", True, True, id="cancelled-before-selection"),
    pytest.param("failure", "success", True, False, id="cancelled-without-binding"),
])
def test_acceptance_aggregates_and_retains_every_bound_candidate(bound, selected, cancelled, expected):
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["acceptance"]
    aggregate_step = next(step for step in job["steps"] if step.get("name") == "Require every scenario for this candidate")
    fallback = next(step for step in job["steps"] if step.get("name") == "Keep a failing result when aggregation wrote none")
    contexts = {
        "steps": {"bind": {"outcome": bound}, "select": {"outcome": selected}},
        "needs": {"previous": {"result": "failure"}},
    }
    for step in (aggregate_step, fallback, job["steps"][-1]):
        assert truthy(evaluate(step["if"], contexts, cancelled=cancelled)) is expected


def _bind(tmp_path, candidate, **environment):
    step = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["acceptance"]["steps"][0]
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, "-c", step["run"]], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "FLEET_CANDIDATE": candidate, "GITHUB_OUTPUT": str(output),
             "GITHUB_REPOSITORY": SOURCE["repository"], "GITHUB_SHA": SOURCE["commit"], **environment},
    )
    return result, output.read_text() if output.exists() else None


def test_acceptance_binds_a_candidate_for_this_commit_with_only_the_standard_library(tmp_path, selection):
    result, output = _bind(tmp_path, json.dumps(selection))
    assert result.returncode == 0, result.stderr
    assert output == f"admission-sha={selection['artifacts']['admission']['sha256']}\n"


@pytest.mark.parametrize("fault", [
    "other-commit", "other-repository", "other-kind", "short-admission", "upper-admission", "list",
    "not-json", "oversized", "empty",
])
def test_acceptance_records_no_result_for_a_candidate_that_names_another_source(tmp_path, selection, fault):
    value = copy.deepcopy(selection)
    raw = None
    if fault == "other-commit":
        value["source"]["commit"] = "c" * 40
    elif fault == "other-repository":
        value["source"]["repository"] = "fork/content"
    elif fault == "other-kind":
        value["kind"] = "ReleaseCandidate"
    elif fault == "short-admission":
        value["artifacts"]["admission"]["sha256"] = "a" * 63
    elif fault == "upper-admission":
        value["artifacts"]["admission"]["sha256"] = "A" * 64
    elif fault == "list":
        raw = json.dumps([value])
    elif fault == "not-json":
        raw = "{"
    elif fault == "oversized":
        raw = json.dumps({**value, "padding": "x" * 16384})
    elif fault == "empty":
        raw = ""
    result, output = _bind(tmp_path, raw if raw is not None else json.dumps(value))
    assert result.returncode != 0
    assert output is None
    assert "records no result" in result.stderr


@pytest.mark.parametrize("written", [None, "", '{"kind": "ReleaseAcceptance", "status": "passed"}'])
def test_acceptance_keeps_a_failing_result_only_when_aggregation_wrote_none(tmp_path, written):
    step = next(step for step in yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["acceptance"]["steps"]
                if step.get("name") == "Keep a failing result when aggregation wrote none")
    runner = tmp_path / "runner"
    receipt = runner / "acceptance" / "release-acceptance.json"
    if written is not None:
        receipt.parent.mkdir(parents=True)
        receipt.write_text(written)
    result = run_script(step["run"], tmp_path, {"RUNNER_TEMP": bash_path(runner)})
    assert result.returncode == 0, result.stderr
    document = json.loads(receipt.read_text())
    if written:
        assert document["status"] == "passed"
        assert "::error::" not in result.stdout
    else:
        assert document == {"kind": "ReleaseAcceptance", "status": "failed"}
        assert "::error::Aggregation wrote no receipt" in result.stdout


@pytest.mark.parametrize("slot", SLOT_SCENARIOS)
def test_workflow_site_names_match_receipt_and_ownership_matchers(slot):
    caller = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8"))
    reconcile = yaml.safe_load((WORKFLOWS / "_fleet-reconcile.yaml").read_text(encoding="utf-8"))
    name = evaluate(caller["jobs"]["e2e"]["name"], {
        "needs": {"prep": {"outputs": {"candidate-mode": "true"}}},
        "matrix": {"secret-sync-mode": slot},
    })
    assert name == SITE_JOB[slot]
    values = Jobs().add(name, attempt=2, prefix="Acceptance / ").values
    assert named_job(values, SITE_JOB[slot]) is values[0]
    outcome = artifact(f"site-outcome-{RUN}-2-{slot}", 500)
    assert select_evidence(values, [outcome], run=RUN, commit=SOURCE["commit"])[slot] == {
        "attempt": 2, "status": "passed", "artifact": outcome,
    }
    assert evaluate(reconcile["jobs"]["reconcile"]["name"], {
        "inputs": {"kind": "site"}, "matrix": {"slot": slot},
    }) == f"Reconcile original {SITE_JOB[slot]}"


@pytest.mark.parametrize("slot", SLOT_SCENARIOS)
@pytest.mark.parametrize("release", ["2607", "2608"])
def test_ordinary_e2e_job_names_keep_the_release_and_internal_mode(slot, release):
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8"))["jobs"]["e2e"]
    assert evaluate(job["name"], {
        "needs": {"prep": {"outputs": {"candidate-mode": "false"}}},
        "matrix": {"secret-sync-mode": slot, "aio-release": release},
    }) == f"e2e ({release}, {slot})"


def test_fleet_reconciliation_job_name_is_unchanged():
    job = yaml.safe_load((WORKFLOWS / "_fleet-reconcile.yaml").read_text(encoding="utf-8"))["jobs"]["reconcile"]
    assert evaluate(job["name"], {
        "inputs": {"kind": "fleet"}, "matrix": {"slot": "fleet"},
    }) == "Reconcile original fleet"


def test_site_cases_do_not_cancel_each_other_and_always_clean_up_before_the_receipt():
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8"))["jobs"]["e2e"]
    assert job["strategy"]["fail-fast"] is False
    assert job["name"].startswith("${{ needs.prep.outputs.candidate-mode == 'true' && format('Site case ({0})'")
    names = [step.get("name") or step.get("uses") for step in job["steps"]]
    cleanup = "Remove Site resources created by this attempt"
    for earlier, later in (
        ("Preflight the Site resource group", "Retain Site ownership"),
        ("Retain Site ownership", "Prepare the Site resource group"),
        ("Prepare the Site resource group", "Snapshot RG resources (persistent mode)"),
        ("Observe bounded AIO readiness", cleanup),
        (cleanup, "Record the Site case outcome"),
        ("Record the Site case outcome", "Retain the Site case outcome"),
    ):
        assert names.index(earlier) < names.index(later)
    steps = {step.get("name"): step for step in job["steps"]}
    assert "cleanup --execute --kind site" in steps[cleanup]["run"]
    assert "Teardown" not in json.dumps(steps[cleanup])
    assert steps["Retain Site ownership"]["with"]["name"] == (
        "site-ownership-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.secret-sync-mode }}")
    assert steps["Retain the Site case outcome"]["with"]["name"] == (
        "site-outcome-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.secret-sync-mode }}")


@pytest.mark.parametrize(("candidate", "preflight", "cancelled", "expected"), [
    pytest.param("true", "success", False, True, id="admitted-candidate"),
    pytest.param("true", "failure", False, False, id="preflight-failed"),
    pytest.param("true", "skipped", False, False, id="preflight-skipped"),
    pytest.param("true", "cancelled", False, False, id="preflight-cancelled"),
    pytest.param("false", "success", False, False, id="ordinary-run"),
    pytest.param("true", "success", True, True, id="cleanup-after-cancellation"),
    pytest.param("true", "failure", True, False, id="cancelled-without-admission"),
    pytest.param("false", "success", True, False, id="no-site-cleanup-for-ordinary-cancellation"),
])
def test_site_cleanup_requires_candidate_ownership_even_after_cancellation(candidate, preflight, cancelled, expected):
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["e2e"]
    step = next(step for step in job["steps"] if step.get("id") == "site-cleanup")
    contexts = {
        "env": {"CANDIDATE_MODE": candidate},
        "steps": {"site-preflight": {"outcome": preflight}},
        "needs": {"previous": {"result": "failure"}},
    }
    assert truthy(evaluate(step["if"], contexts, cancelled=cancelled)) is expected


@pytest.mark.parametrize(("candidate", "selected", "cancelled", "expected"), [
    pytest.param("true", "success", False, True, id="selected-candidate"),
    pytest.param("true", "failure", False, False, id="selection-failed"),
    pytest.param("true", "skipped", False, False, id="selection-skipped"),
    pytest.param("true", "cancelled", False, False, id="selection-cancelled"),
    pytest.param("false", "success", False, False, id="ordinary-run"),
    pytest.param("true", "success", True, True, id="receipt-after-cancellation"),
    pytest.param("false", "success", True, False, id="no-site-receipt-for-ordinary-cancellation"),
])
def test_site_outcomes_are_recorded_and_retained_only_for_selected_candidates(candidate, selected, cancelled, expected):
    job = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text())["jobs"]["e2e"]
    contexts = {
        "env": {"CANDIDATE_MODE": candidate},
        "steps": {"select": {"outcome": selected}},
        "needs": {"previous": {"result": "failure"}},
    }
    for name in ("Record the Site case outcome", "Retain the Site case outcome"):
        step = next(step for step in job["steps"] if step.get("name") == name)
        assert truthy(evaluate(step["if"], contexts, cancelled=cancelled)) is expected


@pytest.mark.parametrize(("selected", "expected"), [("2", 0), ("1", 1), ("", 1)])
def test_fleet_jobs_refuse_a_partial_rerun_before_any_work(tmp_path, selected, expected):
    workflow = yaml.safe_load((WORKFLOWS / "_fleet-acceptance.yaml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert jobs["select"]["outputs"]["attempt"] == "${{ github.run_attempt }}"
    for name in ("prepare", "hosts", "controller", "cleanup"):
        guard = jobs[name]["steps"][0]
        assert guard["name"] == "Require one complete fleet attempt"
        assert guard["env"] == {"SELECTED_ATTEMPT": "${{ needs.select.outputs.attempt }}"}
        result = run_script(guard["run"], tmp_path, {"SELECTED_ATTEMPT": selected, "GITHUB_RUN_ATTEMPT": "2"})
        assert result.returncode == expected
        assert ("Use Re-run all jobs" in result.stdout) is bool(expected)
    boundary = jobs["result"]["steps"][0]
    assert boundary["env"]["SELECTED_ATTEMPT"] == "${{ needs.select.outputs.attempt }}"
    assert "Use Re-run all jobs" in boundary["run"]


def test_pull_request_and_push_workflows_cannot_reach_azure_authority():
    def reachable(name, seen):
        seen.add(name)
        document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
        for job in document["jobs"].values():
            uses = job.get("uses", "")
            if uses.startswith("./.github/workflows/") and uses.rsplit("/", 1)[1] not in seen:
                reachable(uses.rsplit("/", 1)[1], seen)
        return seen

    for path in WORKFLOWS.glob("*.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = document.get("on", document.get(True))
        if set(triggers) & {"push", "pull_request", "pull_request_target", "schedule"}:
            for name in reachable(path.name, set()):
                text = (WORKFLOWS / name).read_text(encoding="utf-8")
                assert "azure/login" not in text and "secrets.AZURE_" not in text, (path.name, name)
    e2e = yaml.safe_load((WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8"))
    assert set(e2e.get("on", e2e.get(True))) == {"workflow_dispatch"}


def test_guide_names_the_group_secrets_the_workflows_read():
    guide = (ROOT / "docs" / "e2e-testing.md").read_text(encoding="utf-8")
    e2e = (WORKFLOWS / "e2e-test.yaml").read_text(encoding="utf-8")
    for secret, workflows in (
        ("E2E_SITE_RESOURCE_GROUP", ("e2e-test.yaml", "_fleet-reconcile.yaml")),
        ("E2E_FLEET_RESOURCE_GROUPS", ("e2e-test.yaml", "_fleet-acceptance.yaml", "_fleet-reconcile.yaml")),
    ):
        assert f"`{secret}`" in guide
        assert all(f"secrets.{secret}" in (WORKFLOWS / name).read_text(encoding="utf-8") for name in workflows)
    for value in ("scenario=release-acceptance", "**Re-run failed jobs**", "**Re-run all jobs**",
                  "scenario=site-cleanup", "`existingVault`"):
        assert value in guide
    assert "fleet-candidate" not in guide and "fleet-candidate" not in e2e.replace("outputs.fleet-candidate", "")
