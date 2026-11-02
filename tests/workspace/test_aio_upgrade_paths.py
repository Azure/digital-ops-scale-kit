"""AIO upgrade path check: the table, the decision and where aio-upgrade applies it.

The decision runs offline through `az bicep snapshot`, which evaluates the
Bicep functions and `fail()` without Azure.
"""

import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml

from tests.workspace.conftest import WORKSPACE_PATH, az_path

UPGRADE = Path("templates") / "aio" / "upgrade"
LIBRARY = UPGRADE / "upgrade-path.bicep"
TABLE = UPGRADE / "upgrade-paths.yaml"
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[A-Za-z]+\.(\d+))?$")

UNREADABLE = "aio-upgrade could not read the installed AIO version, so it did not check the upgrade path."


def _version_key(version: str) -> tuple[int, ...]:
    match = VERSION.match(version)
    assert match, version
    major, minor, patch, label = match.groups()
    return int(major), int(minor), int(patch), 0 if label else 1, int(label or 0)


def _table() -> dict:
    return yaml.safe_load((WORKSPACE_PATH / TABLE).read_text(encoding="utf-8"))


def _releases() -> dict[str, dict]:
    folder = WORKSPACE_PATH / "parameters" / "aio-releases"
    return {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.yaml"))
    }


def _hops(rows: dict, target: str) -> dict[str, int]:
    """Listed hops from each release to the target, as an independent oracle."""
    hops = {target: 0}
    frontier = [target]
    while frontier:
        reached = []
        for name in frontier:
            for other, row in rows.items():
                if other not in hops and row["version"] in rows[name]["upgradeFrom"]:
                    hops[other] = hops[name] + 1
                    reached.append(other)
        frontier = reached
    return hops


def _bicep(value) -> str:
    """Write a JSON value as a Bicep literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("${", "\\${") + "'"
    if isinstance(value, list):
        return "[" + ", ".join(_bicep(item) for item in value) + "]"
    assert isinstance(value, dict), value
    return "{" + ", ".join(f"{_bicep(key)}: {_bicep(item)}" for key, item in value.items()) + "}"


def _snapshot(folder: Path, name: str, template: str, parameters: dict) -> subprocess.CompletedProcess:
    lines = [f"using '{template}'"]
    lines += [f"param {key} = {_bicep(value)}" for key, value in parameters.items()]
    (folder / f"{name}.bicepparam").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return subprocess.run(
        [az_path(), "bicep", "snapshot", "--file", str(folder / f"{name}.bicepparam")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, cwd=folder,
    )


def _snapshot_document(folder: Path, name: str, template: str, parameters: dict) -> dict:
    result = _snapshot(folder, name, template, parameters)
    assert result.returncode == 0, result.stderr
    return json.loads((folder / f"{name}.snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> Path:
    """A copy of the upgrade templates, so snapshots write nothing into the workspace."""
    root = tmp_path_factory.mktemp("upgrade-path")
    for relative in (UPGRADE, Path("templates") / "common"):
        shutil.copytree(WORKSPACE_PATH / relative, root / relative)
    probe = (
        "import { upgradeDecision, upgradePaths } from 'templates/aio/upgrade/upgrade-path.bicep'\n"
        "param cases array\n"
        "param paths object = {}\n"
        "var table = empty(paths) ? upgradePaths : paths\n"
        "output decisions array = map(cases, case => upgradeDecision("
        "case[0], case[1], case[2], case[3], table))\n"
    )
    (root / "probe.bicep").write_text(probe, encoding="utf-8")
    return root


def _decide(templates: Path, name: str, cases: list[tuple[str, str, str, str]], paths: dict | None = None) -> list[dict]:
    parameters = {"cases": [list(case) for case in cases]}
    if paths is not None:
        parameters["paths"] = paths
    return _snapshot_document(templates, name, "probe.bicep", parameters)["outputs"]["decisions"]


class TestTable:
    def test_each_release_file_has_its_row(self):
        rows = _table()["releases"]
        releases = _releases()
        assert sorted(rows) == sorted(releases)
        for name, release in releases.items():
            assert rows[name]["version"] == release["aioVersion"], name
            assert rows[name]["train"] == release["aioTrain"], name

    def test_the_source_is_pinned_and_dated(self):
        source = _table()["source"]
        assert source["url"] == "https://github.com/Azure/azure-iot-ops-cli-extension/wiki/IoT-Operations-versions"
        assert re.fullmatch(r"[0-9a-f]{40}", source["revision"])
        assert date.fromisoformat(source["retrieved"])

    def test_targets_are_unambiguous_and_lists_hold_older_versions_once(self):
        rows = _table()["releases"]
        identities = [(row["version"], row["train"]) for row in rows.values()]
        assert len(set(identities)) == len(identities)
        for name, row in rows.items():
            assert set(row) == {"version", "train", "upgradeFrom"}, name
            assert len(set(row["upgradeFrom"])) == len(row["upgradeFrom"]), name
            for source in row["upgradeFrom"]:
                assert _version_key(source) < _version_key(row["version"]), (name, source)

    def test_the_newest_release_is_reachable_from_every_shipped_release(self):
        rows = _table()["releases"]
        newest = max(rows, key=lambda name: _version_key(rows[name]["version"]))
        assert set(_hops(rows, newest)) == set(rows)

    def test_2610_keeps_microsofts_list(self):
        assert _table()["releases"]["2610"]["upgradeFrom"] == [
            "1.4.112", "1.4.73", "1.4.41", "1.3.137", "1.3.105", "1.3.70", "1.3.38",
        ]


class TestDecision:
    def test_every_status_and_message(self, templates):
        cases = {
            ("1.2.154", "stable", "1.5.41", "stable"): ("refused", "2609", (
                "Microsoft's supported upgrade list for AIO release 2610 (1.5.41) does not include "
                "AIO 1.2.154. Upgrade to AIO release 2609 first."
            )),
            ("1.0.34", "stable", "1.5.41", "stable"): ("refused", "", (
                "Microsoft's supported upgrade list for AIO release 2610 (1.5.41) does not include "
                "AIO 1.0.34, and the AIO releases in this content offer no path from it. "
                "Choose a path at https://aka.ms/aio-versions."
            )),
            ("1.5.41", "stable", "1.4.112", "stable"): ("refused", "", (
                "AIO release 2609 (1.4.112) is older than the installed AIO 1.5.41. Azure IoT "
                "Operations does not support a downgrade. Uninstall and reinstall to use an older version."
            )),
            ("1.5.41", "stable", "1.4.99", "stable"): ("refused", "", (
                "AIO 1.4.99 is older than the installed AIO 1.5.41. Azure IoT Operations does not "
                "support a downgrade. Uninstall and reinstall to use an older version."
            )),
            ("1.6.0-preview.57", "preview", "1.5.41", "stable"): ("refused", "", (
                "The instance runs AIO 1.6.0-preview.57 on the preview train, and AIO release 2610 "
                "(1.5.41) uses the stable train. Moving between preview and GA needs an uninstall and reinstall."
            )),
            ("1.5.41", "stable", "1.6.0-preview.57", "preview"): ("refused", "", (
                "The instance runs AIO 1.5.41 on the stable train, and AIO 1.6.0-preview.57 uses the "
                "preview train. Moving between preview and GA needs an uninstall and reinstall."
            )),
            ("1.5.41", "stable", "1.5.50", "stable"): ("unverified", "", (
                "AIO 1.5.50 on the stable train is not an AIO release this content lists, so "
                "aio-upgrade did not check the path from AIO 1.5.41."
            )),
            ("", "", "1.5.41", "stable"): ("unverified", "", UNREADABLE),
            ("1.5", "stable", "1.5.41", "stable"): ("unverified", "", UNREADABLE),
            ("1.4.112", "", "1.5.41", "stable"): ("unverified", "", UNREADABLE),
            ("1.5.41", "Stable", "1.5.41", "stable"): ("unchanged", "", ""),
            ("1.4.112", "stable", "1.5.41", "stable"): ("supported", "", ""),
            ("1.3.38", "stable", "1.5.41", "stable"): ("supported", "", ""),
        }
        decisions = _decide(templates, "statuses", list(cases))
        for case, decision in zip(cases, decisions, strict=True):
            status, hop, message = cases[case]
            assert decision == {"status": status, "message": message, "nextHop": hop}, case

    def test_every_shipped_pair_matches_the_listed_graph(self, templates):
        rows = _table()["releases"]
        sources = sorted({row["version"] for row in rows.values()} | {
            version for row in rows.values() for version in row["upgradeFrom"]
        }, key=_version_key)
        cases = [(source, "stable", row["version"], "stable") for row in rows.values() for source in sources]
        decisions = _decide(templates, "pairs", cases)
        for (source, _, target, _), decision in zip(cases, decisions, strict=True):
            name = next(key for key, row in rows.items() if row["version"] == target)
            if source == target:
                expected, hop = "unchanged", ""
            elif _version_key(source) > _version_key(target):
                expected, hop = "refused", ""
            elif source in rows[name]["upgradeFrom"]:
                expected, hop = "supported", ""
            else:
                hops = _hops(rows, name)
                firsts = [key for key, row in rows.items() if source in row["upgradeFrom"] and hops.get(key, 0) > 0]
                expected = "refused"
                hop = min(firsts, key=lambda key: (hops[key], [-part for part in _version_key(rows[key]["version"])]),
                          default="")
            assert (decision["status"], decision["nextHop"]) == (expected, hop), (source, target)

    def test_the_next_hop_follows_a_path_that_reaches_the_target(self, templates):
        paths = {"releases": {
            "old": {"version": "1.0.0", "train": "stable", "upgradeFrom": ["0.9.0"]},
            "viable": {"version": "1.1.0", "train": "stable", "upgradeFrom": ["1.0.0"]},
            "newer": {"version": "1.1.5", "train": "stable", "upgradeFrom": ["1.0.0"]},
            "dead-end": {"version": "1.2.0", "train": "stable", "upgradeFrom": ["1.0.0"]},
            "target": {"version": "2.0.0", "train": "stable", "upgradeFrom": ["1.1.0", "1.1.5"]},
            "island": {"version": "3.0.0", "train": "stable", "upgradeFrom": []},
            "narrow": {"version": "4.0.0", "train": "stable", "upgradeFrom": ["1.1.0"]},
        }}
        decisions = _decide(templates, "fixture", [
            ("1.0.0", "stable", "2.0.0", "stable"),
            ("0.9.0", "stable", "2.0.0", "stable"),
            ("2.0.0", "stable", "3.0.0", "stable"),
            ("1.0.0", "stable", "4.0.0", "stable"),
            ("0.5.0", "stable", "2.0.0", "stable"),
        ], paths)
        assert [decision["nextHop"] for decision in decisions] == ["newer", "old", "", "viable", ""]
        assert {decision["status"] for decision in decisions} == {"refused"}


class TestPlacement:
    def test_the_check_runs_first_and_reports_warnings(self, workspace):
        manifest = yaml.safe_load((workspace / "manifests" / "aio-upgrade" / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["requiredFeatures"] == ["step-reports/v1"]
        assert manifest["steps"][0] == {
            "name": "check-upgrade-path",
            "template": "templates/aio/upgrade/check-upgrade-path.bicep",
            "scope": "resourceGroup",
            "reports": {"warnings": "warnings"},
        }

    def test_the_check_only_reads_the_aio_extension(self, workspace):
        result = subprocess.run(
            [az_path(), "bicep", "build", "--file", str(workspace / UPGRADE / "check-upgrade-path.bicep"), "--stdout"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        assert result.returncode == 0, result.stderr
        template = json.loads(result.stdout)
        resources = template["resources"]
        resources = list(resources.values()) if isinstance(resources, dict) else resources
        assert [resource["type"] for resource in resources] == [
            "Microsoft.Kubernetes/connectedClusters", "Microsoft.KubernetesConfiguration/extensions",
        ]
        assert all(resource.get("existing") is True for resource in resources)
        assert "cert-manager" not in result.stdout
        assert template["outputs"]["warnings"]["type"] == "array"

    def test_a_site_without_a_cluster_name_gets_a_warning(self, templates):
        document = _snapshot_document(templates, "no-cluster", str(UPGRADE / "check-upgrade-path.bicep").replace("\\", "/"), {
            "aioVersion": "1.5.41", "aioTrain": "stable",
        })
        assert document["predictedResources"] == []
        assert document["outputs"]["status"] == "unverified"
        assert document["outputs"]["warnings"] == [{"message": UNREADABLE}]


def _writer(aio_version: str, **target) -> dict:
    snapshot = {"configurationSettings": {}, "identity": {"type": "None"}, "releaseTrain": "stable"}
    return {
        "connectedClusterName": "plant-arc",
        "aio": {**snapshot, "name": "azure-iot-operations-abcde", "extensionType": "microsoft.iotoperations",
                "version": aio_version, "releaseNamespace": "azure-iot-operations", "extensionSuffix": "abcde"},
        "secretStore": {**snapshot, "name": "azure-secret-store", "extensionType": "microsoft.azure.secretstore",
                        "version": "1.6.0"},
        "certManager": {**snapshot, "name": "cert-manager", "extensionType": "microsoft.certmanagement",
                        "version": "1.2.1"},
        "enableCertManager": True,
        "aioApiVersion": "2026-10-01",
        **target,
    }


class TestWriter:
    TEMPLATE = str(UPGRADE / "update-extensions.bicep").replace("\\", "/")

    @pytest.mark.parametrize(("installed", "target"), [
        pytest.param("1.4.112", {"aioVersion": "1.5.41", "aioTrain": "stable"}, id="supported"),
        pytest.param("1.5.41", {"aioVersion": "1.5.41", "aioTrain": "stable"}, id="rerun"),
        pytest.param("1.5.41", {}, id="preserve-installed"),
        pytest.param("1.5.41", {"aioVersion": "1.5.50", "aioTrain": "stable"}, id="unlisted-override"),
    ])
    def test_an_allowed_target_writes_every_extension(self, templates, installed, target):
        name = f"writer-{installed}-{target.get('aioVersion', 'kept')}"
        document = _snapshot_document(templates, name, self.TEMPLATE, _writer(installed, **target))
        versions = {resource["name"]: resource["properties"]["version"] for resource in document["predictedResources"]}
        assert versions == {"azure-iot-operations-abcde": target.get("aioVersion", installed),
                            "azure-secret-store": "1.6.0", "cert-manager": "1.2.1"}

    def test_a_refused_target_writes_nothing(self, templates):
        result = _snapshot(templates, "writer-downgrade", self.TEMPLATE, _writer(
            "1.5.41", aioVersion="1.4.112", aioTrain="stable", aioApiVersion="2026-07-01",
        ))
        assert result.returncode != 0
        assert "AIO release 2609 (1.4.112) is older than the installed AIO 1.5.41." in result.stderr
        assert not (templates / "writer-downgrade.snapshot.json").exists()
