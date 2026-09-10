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
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import (
    bash_path as _bash_path,
)
from tests.shell_helpers import (
    required_bash as _required_bash,
)
from tests.shell_helpers import (
    write_executable as _write_executable,
)
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


def _ado_deployment_steps() -> tuple[dict, list[dict]]:
    data = yaml.safe_load(REUSABLE_ADO_DEPLOY.read_text(encoding="utf-8"))
    stage = data["stages"][0]
    steps = stage["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]
    return stage, steps


def _install_fake_delivery_tools(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "siteops-invocations.log"
    _write_executable(
        bin_dir / "siteops",
        """#!/usr/bin/env bash
command_name=""
for argument in "$@"; do
  case "$argument" in
    plan|deploy)
      command_name="$argument"
      break
      ;;
  esac
done
printf '%s %s\n' "$command_name" "$*" >> "$FAKE_SITEOPS_LOG"

if [[ "$command_name" == "plan" ]]; then
  if [[ "${SITEOPS_REDACT_OUTPUT:-}" != "1" ]]; then
    exit 97
  fi
  if [[ "$(umask)" != "0077" ]]; then
    exit 96
  fi
  printf 'PRIVATE PLAN STDERR SENTINEL\n' >&2
  plan_exit="${FAKE_PLAN_EXIT:-0}"
  if [[ "${FAKE_PLAN_DOCUMENT_VALID:-1}" == "1" ]]; then
    status="planned"
    executable=true
    [[ "$plan_exit" == "0" ]] || status="invalid"
    [[ "$plan_exit" == "0" ]] || executable=false
    projection="publishable"
    intent="executable"
    case "${FAKE_PLAN_DOCUMENT_MODE:-valid}" in
      private) projection="local-private" ;;
      describe) intent="describe"; executable=false ;;
      not-executable) executable=false ;;
    esac
    printf '{"apiVersion":"siteops/v1alpha1","kind":"DeploymentPlan","projection":"%s","status":"%s","intent":"%s","executable":%s}\n' "$projection" "$status" "$intent" "$executable"
  else
    printf 'not-json\n'
  fi
  exit "$plan_exit"
fi

if [[ "$command_name" == "deploy" ]]; then
  [[ "${SITEOPS_REDACT_OUTPUT:-}" == "1" ]] || exit 97
  [[ "$(umask)" == "0077" ]] || exit 96
  printf 'PRIVATE RUN STDERR SENTINEL\n' >&2
  run_exit="${FAKE_DEPLOY_EXIT:-0}"
  api_version="siteops/v1alpha1"
  kind="DeploymentRun"
  projection="publishable"
  document_exit="$run_exit"
  site_total=1
  operation_total=2
  interrupted=false
  status="succeeded"
  [[ "$run_exit" == "0" ]] || status="failed"
  if [[ "$run_exit" == "130" ]]; then
    status="cancelled"
    interrupted=true
  fi
  case "${FAKE_RUN_DOCUMENT_MODE:-valid}" in
    invalid-json) printf 'PRIVATE INVALID RUN SENTINEL\n'; exit "$run_exit" ;;
    private) projection="local-private" ;;
    plan-kind) kind="DeploymentPlan" ;;
    skipped) status="skipped"; operation_total=0 ;;
    unknown) status="unknown" ;;
    bad-status) status="partly-succeeded" ;;
    interrupted-success) status="succeeded" ;;
    interrupted-unknown) status="unknown" ;;
    interrupted-without-stop) interrupted=true ;;
    text-interrupted) interrupted='"true"' ;;
    text-counts) site_total='"1"'; operation_total='"2"' ;;
    false-success) status="succeeded"; interrupted=false ;;
    false-failure) status="failed" ;;
    mismatched-exit) document_exit=99 ;;
  esac
  summary_json=""
  if [[ "${FAKE_RUN_DOCUMENT_MODE:-valid}" != "no-summary" ]]; then
    summary_json=$(printf '"summary":{"interrupted":%s,"operations":{"total":%s},"sites":{"total":%s}},' "$interrupted" "$operation_total" "$site_total")
  fi
  printf '{"apiVersion":"%s","kind":"%s","projection":"%s","status":"%s","exitCode":%s,%s"diagnostics":[]}\n' "$api_version" "$kind" "$projection" "$status" "$document_exit" "$summary_json"
  exit "$run_exit"
fi

exit 98
""",
    )
    _write_executable(
        bin_dir / "python3",
        (
            "#!/usr/bin/env bash\n"
            f"exec {shlex.quote(_bash_path(Path(sys.executable)))} \"$@\"\n"
        ),
    )
    return bin_dir, invocation_log


def _delivery_plan_case(platform: str, *, run_step: bool = False) -> tuple[str, str]:
    if platform == "github":
        data = yaml.safe_load(
            REUSABLE_GITHUB_DEPLOY.read_text(encoding="utf-8")
        )
        step = next(
            step
            for step in data["jobs"]["deploy"]["steps"]
            if step.get("name") == (
                "Deploy" if run_step else "Prepare executable deployment plan"
            )
        )
        return step["run"], str(data.get("env", {}).get("SITEOPS_REDACT_OUTPUT", ""))

    stage, steps = _ado_deployment_steps()
    task = next(
        step
        for step in steps
        if step.get("task", "").startswith("AzureCLI@2")
        and "plan \"$MANIFEST\"" in step["inputs"].get("inlineScript", "")
    )
    return (
        task["inputs"]["inlineScript"],
        str(stage.get("variables", {}).get("SITEOPS_REDACT_OUTPUT", "")),
    )


def _run_delivery_plan_script(
    platform: str,
    tmp_path: Path,
    *,
    plan_exit: int,
    valid_document: bool,
    document_mode: str = "valid",
    dry_run: bool = False,
    run_step: bool = False,
    deploy_exit: int = 0,
    run_document_mode: str = "valid",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    script, redaction = _delivery_plan_case(platform, run_step=run_step)
    bin_dir, invocation_log = _install_fake_delivery_tools(tmp_path)
    temp_dir = tmp_path / "runner-temp"
    summary_dir = tmp_path / "summaries"
    temp_dir.mkdir()
    summary_dir.mkdir()
    github_summary = summary_dir / "github-summary.md"

    exports = {
        "PATH_PREFIX": _bash_path(bin_dir),
        "FAKE_SITEOPS_LOG": _bash_path(invocation_log),
        "FAKE_PLAN_EXIT": str(plan_exit),
        "FAKE_PLAN_DOCUMENT_VALID": "1" if valid_document else "0",
        "FAKE_PLAN_DOCUMENT_MODE": document_mode,
        "FAKE_DEPLOY_EXIT": str(deploy_exit),
        "FAKE_RUN_DOCUMENT_MODE": run_document_mode,
        "SITEOPS_REDACT_OUTPUT": redaction,
        "INPUT_WORKSPACE": "workspace",
        "INPUT_MANIFEST": "manifests/install.yaml",
        "INPUT_SELECTOR": "",
        "INPUT_DRY_RUN": "true" if dry_run else "false",
        "RUNNER_TEMP": _bash_path(temp_dir),
        "GITHUB_STEP_SUMMARY": _bash_path(github_summary),
        "WORKSPACE": "workspace",
        "MANIFEST": "manifests/install.yaml",
        "SELECTOR": "",
        "DRY_RUN": "True" if dry_run else "False",
        "PLAN_TEMP_DIRECTORY": _bash_path(temp_dir),
        "PLAN_SUMMARY_DIRECTORY": _bash_path(summary_dir),
    }
    preamble = [
        f"export PATH={shlex.quote(exports.pop('PATH_PREFIX'))}:\"$PATH\""
    ]
    preamble.extend(
        f"export {name}={shlex.quote(value)}"
        for name, value in exports.items()
    )
    command = "\n".join((*preamble, script))

    result = subprocess.run(
        [
            str(_required_bash()),
            "--noprofile",
            "--norc",
            *(("-e", "-o", "pipefail") if platform == "github" else ()),
            "-c",
            command,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    summary_path = (
        github_summary
        if platform == "github"
        else summary_dir / (
            "deployment-result.md" if run_step else "deployment-plan.md"
        )
    )
    return result, summary_path, temp_dir, invocation_log


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

    def test_github_authenticates_and_refreshes_before_planning(self):
        data = yaml.safe_load(
            REUSABLE_GITHUB_DEPLOY.read_text(encoding="utf-8")
        )
        steps = data["jobs"]["deploy"]["steps"]
        names = [step.get("name") for step in steps]

        assert data["env"]["SITEOPS_REDACT_OUTPUT"] == "1"
        assert names.index("Azure Login (OIDC)") < names.index(
            "Start OIDC token refresh service"
        ) < names.index("Prepare executable deployment plan")

    def test_github_dry_run_skips_deploy_after_planning(self):
        data = yaml.safe_load(
            REUSABLE_GITHUB_DEPLOY.read_text(encoding="utf-8")
        )
        deploy = next(
            step
            for step in data["jobs"]["deploy"]["steps"]
            if step.get("name") == "Deploy"
        )

        assert deploy["if"] == "inputs.dry-run != true"
        assert "--dry-run" not in deploy["run"]

    def test_ado_plans_and_deploys_in_one_authenticated_task(self):
        stage, steps = _ado_deployment_steps()
        azure_tasks = [
            step
            for step in steps
            if step.get("task", "").startswith("AzureCLI@2")
        ]

        assert stage["variables"]["SITEOPS_REDACT_OUTPUT"] == "1"
        assert len(azure_tasks) == 1
        task = azure_tasks[0]
        assert task["inputs"]["azureSubscription"] == (
            "${{ parameters.serviceConnection }}"
        )
        script = task["inputs"]["inlineScript"]
        assert 'plan "$MANIFEST"' in script
        assert 'deploy "$MANIFEST"' in script

    @pytest.mark.parametrize("platform", ["github", "azure-pipelines"])
    @pytest.mark.parametrize(
        ("plan_exit", "valid_document", "document_mode", "expected_exit"),
        [
            pytest.param(0, True, "valid", 0, id="planned"),
            pytest.param(23, True, "valid", 23, id="invalid-plan"),
            pytest.param(0, False, "valid", 1, id="unsupported-document"),
            pytest.param(23, False, "valid", 23, id="failed-without-document"),
            pytest.param(0, True, "private", 1, id="private-document"),
            pytest.param(0, True, "describe", 1, id="describe-document"),
            pytest.param(0, True, "not-executable", 1, id="not-executable"),
        ],
    )
    def test_executable_plan_delivery_executes_real_shell_semantics(
        self,
        tmp_path,
        platform,
        plan_exit,
        valid_document,
        document_mode,
        expected_exit,
    ):
        result, summary_path, temp_dir, invocation_log = (
            _run_delivery_plan_script(
                "github" if platform == "github" else "azure-pipelines",
                tmp_path,
                plan_exit=plan_exit,
                valid_document=valid_document,
                document_mode=document_mode,
            )
        )

        assert result.returncode == expected_exit, (
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        summary = summary_path.read_text(encoding="utf-8")
        combined_output = f"{result.stdout}\n{result.stderr}\n{summary}"
        assert "PRIVATE PLAN STDERR SENTINEL" not in combined_output
        assert "not-json" not in summary
        publishable = valid_document and document_mode == "valid"
        if publishable:
            assert '"projection":"publishable"' in summary
            expected_status = "planned" if plan_exit == 0 else "invalid"
            assert f'"status":"{expected_status}"' in summary
        else:
            assert "Publishable plan output was unavailable." in summary

        invocations = invocation_log.read_text(encoding="utf-8").splitlines()
        assert " plan " in f" {invocations[0]} "
        assert "--output json" in invocations[0]
        assert "--projection publishable" in invocations[0]
        if platform == "azure-pipelines" and plan_exit == 0 and publishable:
            assert len(invocations) == 2
            assert " deploy " in f" {invocations[1]} "
        else:
            assert len(invocations) == 1

        assert not (temp_dir / "siteops-plan.json").exists()
        assert not (temp_dir / "siteops-plan.stderr").exists()

    @pytest.mark.parametrize("plan_exit", [0, 23])
    def test_ado_dry_run_plans_once_and_preserves_plan_exit(
        self,
        tmp_path,
        plan_exit,
    ):
        result, summary_path, temp_dir, invocation_log = (
            _run_delivery_plan_script(
                "azure-pipelines",
                tmp_path,
                plan_exit=plan_exit,
                valid_document=True,
                dry_run=True,
            )
        )

        assert result.returncode == plan_exit
        invocations = invocation_log.read_text(encoding="utf-8").splitlines()
        assert len(invocations) == 1
        assert " plan " in f" {invocations[0]} "
        assert " deploy " not in f" {invocations[0]} "
        assert not (summary_path.parent / "deployment-result.md").exists()
        assert "Deployment Result" not in summary_path.read_text(
            encoding="utf-8"
        )
        assert not (temp_dir / "siteops-plan.json").exists()
        assert not (temp_dir / "siteops-plan.stderr").exists()

    @pytest.mark.parametrize("platform", ["github", "azure-pipelines"])
    @pytest.mark.parametrize(
        ("deploy_exit", "mode", "expected_exit", "published"),
        [
            (0, "valid", 0, True),
            (0, "skipped", 0, True),
            (1, "valid", 1, True),
            (1, "unknown", 1, True),
            (130, "valid", 130, True),
            (130, "interrupted-success", 130, True),
            (130, "interrupted-unknown", 130, True),
            (0, "invalid-json", 1, False),
            (23, "invalid-json", 23, False),
            (0, "private", 1, False),
            (0, "plan-kind", 1, False),
            (0, "bad-status", 1, False),
            (0, "no-summary", 1, False),
            (0, "text-interrupted", 1, False),
            (0, "text-counts", 1, False),
            (0, "interrupted-without-stop", 1, False),
            (130, "false-success", 130, False),
            (0, "mismatched-exit", 1, False),
            (0, "false-failure", 1, False),
            (1, "false-success", 1, False),
        ],
    )
    def test_deployment_result_publication_preserves_outcome_and_privacy(
        self, tmp_path, platform, deploy_exit, mode, expected_exit, published
    ):
        result, summary_path, temp_dir, invocation_log = _run_delivery_plan_script(
            platform,
            tmp_path,
            plan_exit=0,
            valid_document=True,
            run_step=True,
            deploy_exit=deploy_exit,
            run_document_mode=mode,
        )

        assert result.returncode == expected_exit, (result.stdout, result.stderr)
        summary = summary_path.read_text(encoding="utf-8")
        assert "PRIVATE" not in result.stdout + result.stderr + summary
        if published:
            assert '"kind":"DeploymentRun"' in summary
            assert '"projection":"publishable"' in summary
            assert "Sites 1. Operations" in summary
            assert (
                "Interrupted yes." if deploy_exit == 130 else "Interrupted no."
            ) in summary
            if mode == "skipped":
                assert '"status":"skipped"' in summary
                assert "Operations 0." in summary
                assert "Success" not in summary
        else:
            assert "Publishable deployment result was unavailable." in summary
            assert "```json" not in summary
        invocations = invocation_log.read_text(encoding="utf-8").splitlines()
        assert len(invocations) == (1 if platform == "github" else 2)
        assert " deploy " in f" {invocations[-1]} "
        assert "--output json" in invocations[-1]
        assert "--projection publishable" in invocations[-1]
        assert not (temp_dir / "siteops-run.json").exists()
        assert not (temp_dir / "siteops-run.stderr").exists()
