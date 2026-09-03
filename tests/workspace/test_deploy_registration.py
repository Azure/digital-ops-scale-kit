"""Tests that every deployable manifest is registered on both CI platforms.

The GitHub Actions workflow and the Azure Pipelines definition each carry a
hand-maintained dropdown of deployable manifests. They have drifted before: a
manifest registered on one platform and not the other makes that surface
undeployable from the missing one, which is how the AKS Edge Essentials
manifests were unreachable from Azure Pipelines until a later repair.

These tests close that by deriving the expected set from the workspace itself,
so adding a manifest fails CI until it is registered on both, and removing one
fails until it is de-registered.
"""

import re
from pathlib import Path

import yaml

from tests.workspace.test_manifest_validation import _all_manifest_files

REPO_ROOT = Path(__file__).parent.parent.parent
GITHUB_DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy.yaml"
ADO_DEPLOY = REPO_ROOT / ".pipelines" / "deploy.yaml"
REUSABLE_GITHUB_DEPLOY = REPO_ROOT / ".github" / "workflows" / "_siteops-deploy.yaml"
REUSABLE_ADO_DEPLOY = REPO_ROOT / ".pipelines" / "templates" / "siteops-deploy.yaml"
GITHUB_INTEGRATION = REPO_ROOT / ".github" / "workflows" / "integration-test.yaml"

_RESOURCE_SET_SAMPLES = (
    "samples/resource-set-basic/manifest.yaml",
    "samples/resource-set-composition/manifest.yaml",
)


def _github_manifest_options() -> list[str]:
    """Read the manifest choice list from the GitHub Actions deploy workflow.

    `on` parses as the boolean True, since YAML 1.1 treats it as a keyword.
    """
    data = yaml.safe_load(GITHUB_DEPLOY.read_text(encoding="utf-8"))
    trigger = data.get("on", data.get(True))
    return list(trigger["workflow_dispatch"]["inputs"]["manifest"]["options"])


def _ado_manifest_options() -> list[str]:
    """Read the manifest value list from the Azure Pipelines deploy definition."""
    data = yaml.safe_load(ADO_DEPLOY.read_text(encoding="utf-8"))
    for parameter in data["parameters"]:
        if parameter.get("name") == "manifest":
            return list(parameter["values"])
    raise AssertionError("No `manifest` parameter found in .pipelines/deploy.yaml")


def _deployable_manifests(workspace: Path) -> set[str]:
    """Workspace-relative paths of every manifest meant to be deployed directly.

    A partial (filename prefixed `_`) is composed rather than deployed, so it is
    excluded. Everything else discovered by the shared sweep is an entry point an
    operator can select.
    """
    deployable: set[str] = set()
    for path in _all_manifest_files(workspace):
        if path.name.startswith("_"):
            continue
        deployable.add(path.relative_to(workspace).as_posix())
    return deployable


def _github_sample_selectors() -> dict[str, str]:
    text = GITHUB_DEPLOY.read_text(encoding="utf-8")
    return {
        manifest: selector
        for manifest, selector in re.findall(
            r"^\s+(samples/resource-set-[^)]+)\)\s*$"
            r".*?^\s+SELECTOR=\"([^\"]+)\"",
            text,
            re.MULTILINE | re.DOTALL,
        )
    }


def _ado_sample_selectors() -> dict[str, tuple[str, str]]:
    lines = ADO_DEPLOY.read_text(encoding="utf-8").splitlines()
    result: dict[str, tuple[str, str]] = {}
    for index, line in enumerate(lines):
        match = re.search(
            r"if eq\(parameters\.manifest, '([^']+)'\)",
            line,
        )
        if not match or not match.group(1).startswith("samples/resource-set-"):
            continue
        selectors: list[str] = []
        for candidate in lines[index + 1:]:
            if "${{ elseif" in candidate:
                break
            stripped = candidate.strip()
            if stripped.startswith("selector: "):
                selectors.append(stripped.removeprefix("selector: "))
        assert len(selectors) == 2, (
            f"{match.group(1)} should have one selector with the optional "
            f"filter and one without it. Found: {selectors}"
        )
        result[match.group(1)] = (selectors[0], selectors[1])
    return result


class TestDeployDropdownRegistration:
    """Both platforms offer exactly the deployable manifests the workspace has."""

    def test_dropdowns_are_identical_across_platforms(self):
        """Same manifests, same order, so both UIs present the same default.

        List equality implies set equality, so this subsumes a separate
        membership check. The message carries the set difference, which is what
        a reader needs when it fails.
        """
        github = _github_manifest_options()
        ado = _ado_manifest_options()
        assert github == ado, (
            "The deploy manifest dropdowns have drifted between platforms. A "
            "manifest listed on one and not the other is undeployable from the "
            "missing platform.\n"
            f"  Only in .github/workflows/deploy.yaml: {sorted(set(github) - set(ado))}\n"
            f"  Only in .pipelines/deploy.yaml:        {sorted(set(ado) - set(github))}\n"
            f"  GitHub order: {github}\n"
            f"  ADO order:    {ado}"
        )

    def test_dropdowns_match_the_workspace(self, workspace):
        registered = set(_github_manifest_options())
        deployable = _deployable_manifests(workspace)

        assert registered == deployable, (
            "The deploy dropdowns do not match the deployable manifests in the "
            "workspace. Add a new manifest to both platform dropdowns, or remove "
            "a retired one from both.\n"
            f"  Deployable but not registered: {sorted(deployable - registered)}\n"
            f"  Registered but not deployable: {sorted(registered - deployable)}"
        )

    def test_sample_dropdown_entries_are_alphabetical(self):
        options = _github_manifest_options()
        samples = [
            option
            for option in options
            if option.startswith("samples/")
        ]

        assert samples == sorted(samples)

    def test_resource_set_samples_keep_their_site_selector(self, workspace):
        """The environment selector alone would target every development site."""
        expected = {}
        for manifest_path in _RESOURCE_SET_SAMPLES:
            raw = yaml.safe_load(
                (workspace / manifest_path).read_text(encoding="utf-8")
            )
            expected[manifest_path] = raw["selector"]

        assert _github_sample_selectors() == expected
        assert _ado_sample_selectors() == {
            manifest: (
                f"{selector},${{{{ parameters.selector }}}}",
                selector,
            )
            for manifest, selector in expected.items()
        }

    def test_published_delivery_output_omits_selector_identity(self):
        github = GITHUB_DEPLOY.read_text(encoding="utf-8")
        ado = ADO_DEPLOY.read_text(encoding="utf-8")
        reusable = REUSABLE_GITHUB_DEPLOY.read_text(encoding="utf-8")
        reusable_ado = REUSABLE_ADO_DEPLOY.read_text(encoding="utf-8")
        integration = GITHUB_INTEGRATION.read_text(encoding="utf-8")

        assert "| Selector | \\`<redacted>\\` |" in github
        assert "| Selector | \\`<redacted>\\` |" in ado
        assert "INPUT_SELECTOR: ${{ inputs.selector }}" not in github
        assert "GITHUB_EVENT_PATH" in github
        validation = (
            '[[ -n "$INPUT_SELECTOR" && ! "$INPUT_SELECTOR" =~ '
            "^[a-zA-Z0-9_=,./:-]+$ ]]"
        )
        assert validation in github
        assert github.index(validation) < github.index(
            'echo "selector=$SELECTOR" >> $GITHUB_OUTPUT'
        )
        integration_validation = (
            '[[ -n "$SELECTOR" && ! "$SELECTOR" =~ '
            "^[a-zA-Z0-9_=,./:-]+$ ]]"
        )
        assert integration_validation in integration
        assert integration.index(integration_validation) < integration.index(
            'echo "INTEGRATION_SELECTOR=$SELECTOR" >> "$GITHUB_ENV"'
        )
        assert "SITE_SELECTOR: ${{ needs.prepare.outputs.selector }}" in github
        assert "SITE_SELECTOR:" in reusable
        assert "INPUT_SELECTOR: ${{ secrets.SITE_SELECTOR }}" in reusable
        assert "INPUT_SELECTOR: ${{ inputs.selector }}" not in reusable
        assert "Executing: siteops ${CMD_ARGS[*]}" not in reusable
        assert "Executing: siteops ${CMD_ARGS[*]}" not in reusable_ado
        assert github.count(
            "Resource-set samples use the dev deployment environment"
        ) == 2
        assert (
            "Resource-set samples use the dev deployment environment"
            in ado
        )
