"""Secret Sync prerequisite guard and issuer constraint.

The guard decision runs offline through `az bicep snapshot`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.workspace.conftest import WORKSPACE_PATH, az_path
from tests.workspace.test_aio_upgrade_paths import _snapshot_document

GUARD = Path("templates") / "secretsync" / "check-prerequisites.bicep"
PREPARATION = (
    "Enable the OIDC issuer and workload identity on the cluster, and configure its API server to issue "
    "tokens with that issuer, then run Secret Sync again. "
    "See https://learn.microsoft.com/azure/iot-operations/secure-iot-ops/howto-enable-secure-settings."
)
ISSUER = "https://oidc.example.test/00000000-0000-0000-0000-000000000000/"


@pytest.fixture(scope="module")
def guard(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("secretsync-guard")
    (root / GUARD.parent).mkdir(parents=True)
    shutil.copy(WORKSPACE_PATH / GUARD, root / GUARD)
    (root / "probe.bicep").write_text(
        f"import {{ prerequisiteProblem }} from '{GUARD.as_posix()}'\n"
        "param cases array\n"
        "output problems array = map(cases, case => prerequisiteProblem(case[0], case[1], case[2], case[3]))\n",
        encoding="utf-8",
    )
    return root


def test_each_missing_prerequisite_has_guidance(guard):
    cases = {
        (False, True, ISSUER, False): (
            "Secret Sync needs the OIDC issuer on the Arc cluster, and oidcIssuerProfile.enabled is not true. "
        ),
        (True, False, ISSUER, False): (
            "Secret Sync needs workload identity on the Arc cluster, and "
            "securityProfile.workloadIdentity.enabled is not true. "
        ),
        (True, True, "", False): (
            "Secret Sync needs an OIDC issuer URL, and the Arc cluster's oidcIssuerProfile.issuerUrl is empty. "
        ),
        (True, True, "", True): (
            "Secret Sync needs an OIDC issuer URL, and the Arc cluster's "
            "oidcIssuerProfile.selfHostedIssuerUrl is empty. "
        ),
        (True, True, "http://oidc.example.test/cluster", False): (
            "Secret Sync needs an HTTPS OIDC issuer URL, and the Arc cluster's oidcIssuerProfile.issuerUrl "
            "does not start with https://. "
        ),
    }
    problems = _snapshot_document(guard, "problems", "probe.bicep", {"cases": [list(case) for case in cases]})
    for case, problem in zip(cases, problems["outputs"]["problems"], strict=True):
        assert problem == cases[case] + PREPARATION, case


@pytest.mark.parametrize("issuer", [ISSUER, ISSUER.rstrip("/"), "HTTPS://oidc.example.test/cluster"])
def test_a_ready_cluster_passes_with_its_issuer_unchanged(guard, issuer):
    problems = _snapshot_document(guard, f"ready-{len(issuer)}", "probe.bicep", {"cases": [[True, True, issuer, False]]})
    assert problems["outputs"]["problems"] == [""]


def test_the_guard_runs_before_enablement_on_every_route(workspace):
    partial = yaml.safe_load((workspace / "manifests" / "_partials" / "_secretsync.yaml").read_text(encoding="utf-8"))
    assert [step["name"] for step in partial["steps"]] == ["check-secretsync-prerequisites", "secretsync"]
    assert partial["steps"][0]["template"] == GUARD.as_posix()
    inputs = yaml.safe_load(
        (workspace / "parameters" / "inputs" / "check-secretsync-prerequisites.yaml").read_text(encoding="utf-8")
    )
    assert inputs == {
        "connectedClusterName": "{{ steps.resolve-aio.outputs.connectedClusterName }}",
        "oidcIssuerUrl": "{{ steps.resolve-aio.outputs.oidcIssuerUrl }}",
    }
    for manifest in ("aio-install", "secretsync"):
        text = (workspace / "manifests" / manifest / "manifest.yaml").read_text(encoding="utf-8")
        assert "include: ../_partials/_secretsync.yaml" in text, manifest
    resolve = (workspace / "templates" / "aio" / "resolve-aio.bicep").read_text(encoding="utf-8")
    assert "fail(" not in resolve and "workloadIdentity" not in resolve


def test_the_guard_only_reads_the_cluster(workspace):
    result = subprocess.run(
        [az_path(), "bicep", "build", "--file", str(workspace / GUARD), "--stdout"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
    resources = json.loads(result.stdout)["resources"]
    resources = list(resources.values()) if isinstance(resources, dict) else resources
    assert [(resource["type"], resource.get("existing")) for resource in resources] == [
        ("Microsoft.Kubernetes/connectedClusters", True),
    ]


def test_enablement_rejects_an_empty_issuer(workspace):
    result = subprocess.run(
        [az_path(), "bicep", "build", "--file", str(workspace / "templates" / "secretsync" / "enable-secretsync.bicep"),
         "--stdout"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["parameters"]["oidcIssuerUrl"]["minLength"] == 1
