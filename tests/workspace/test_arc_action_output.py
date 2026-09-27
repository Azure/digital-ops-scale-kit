"""Public-output boundaries for the Arc composite action."""

import os
import shlex
import stat
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

REPO_ROOT = Path(__file__).parent.parent.parent
ACTION = REPO_ROOT / ".github" / "actions" / "connect-arc" / "action.yaml"
WAIT_SCRIPT = ACTION.parent / "wait-connected.sh"
PRIVATE_SENTINEL = "PRIVATE-CLUSTER-RG-ISSUER-SENTINEL"


def _run_bash(
    script: str,
    tmp_path: Path,
    *,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    temp_dir = tmp_path / "private-temp"
    temp_dir.mkdir(exist_ok=True)
    preamble = [
        f"export PATH={shlex.quote(_bash_path(bin_dir))}:\"$PATH\"",
        f"export TMPDIR={shlex.quote(_bash_path(temp_dir))}",
        f"export HOME={shlex.quote(_bash_path(tmp_path))}",
        f"export AZURE_CONFIG_DIR={shlex.quote(_bash_path(tmp_path / 'azure'))}",
    ]
    preamble.extend(
        f"export {name}={shlex.quote(value)}"
        for name, value in env.items()
    )
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith("AZURE_")
    }
    return subprocess.run(
        [
            str(_required_bash()),
            "--noprofile",
            "--norc",
            "-c",
            "\n".join((*preamble, script)),
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "jq",
        (
            "#!/usr/bin/env bash\n"
            f"exec {shlex.quote(_bash_path(Path(sys.executable)))} "
            "-c 'import json,sys; "
            "assert sys.argv[1:] == [\"-er\", "
            "\".issuer | select(type == \\\"string\\\" and length > 0)\"]; "
            "value=json.load(sys.stdin).get(\"issuer\"); "
            "sys.exit(1) if not isinstance(value,str) or not value else "
            "print(value)' \"$@\"\n"
        ),
    )
    return bin_dir


def _action_step(name: str) -> dict:
    data = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    return next(step for step in data["runs"]["steps"] if step.get("name") == name)


@pytest.mark.parametrize(
    ("step_name", "failed_command", "diagnostic"),
    [
        ("Connect cluster to Arc + enable features", "connect", "connect-3.err"),
        (
            "Connect cluster to Arc + enable features",
            "enable-features",
            "enable-features-3.err",
        ),
        ("Enable OIDC issuer + workload identity", "update", "update.err"),
        ("Capture OIDC issuer URL", "show", "issuer.err"),
    ],
)
def test_published_arc_provider_errors_stay_on_runner(
    tmp_path, step_name, failed_command, diagnostic,
):
    bin_dir = _fake_bin(tmp_path)
    _write_executable(
        bin_dir / "az",
        """#!/usr/bin/env bash
printf '%s\\n' "$2" >> "$FAKE_AZ_LOG"
if [[ "$2" == "$FAKE_AZ_FAIL" ]]; then
  printf 'provider details for %s\\n' "$PRIVATE_SENTINEL" >&2
  exit 23
fi
if [[ "$2" == "show" ]]; then
  printf '%s\\n' 'https://example.test/issuer'
fi
""",
    )
    script = _action_step(step_name)["run"].replace("RETRY_DELAY=60", "RETRY_DELAY=0")
    result = _run_bash(
        script,
        tmp_path,
        env={
            "PRIVATE_PROVIDER_ERRORS": "true",
            "PRIVATE_SENTINEL": PRIVATE_SENTINEL,
            "FAKE_AZ_FAIL": failed_command,
            "FAKE_AZ_LOG": _bash_path(tmp_path / "az-commands.log"),
            "RUNNER_TEMP": _bash_path(tmp_path / "private-temp"),
            "GITHUB_OUTPUT": _bash_path(tmp_path / "outputs.txt"),
            "CLUSTER_NAME": "arc-private",
            "RESOURCE_GROUP": "rg-private",
            "LOCATION": "eastus2",
            "CUSTOM_LOCATIONS_OID": "",
        },
    )

    public = result.stdout + result.stderr
    private = tmp_path / "private-temp" / "connectedk8s-diagnostics" / diagnostic
    assert result.returncode != 0
    assert PRIVATE_SENTINEL not in public
    assert "::error::" in public
    assert PRIVATE_SENTINEL in private.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
    calls = (tmp_path / "az-commands.log").read_text(encoding="utf-8").splitlines()
    assert calls.count(failed_command) == (
        3 if failed_command in {"connect", "enable-features"} else 1
    )
    if failed_command == "enable-features":
        assert calls[0] == "connect"
    assert not (tmp_path / "outputs.txt").exists()


def test_source_arc_provider_errors_remain_visible(tmp_path):
    bin_dir = _fake_bin(tmp_path)
    _write_executable(
        bin_dir / "az",
        """#!/usr/bin/env bash
printf 'provider details for %s\\n' "$PRIVATE_SENTINEL" >&2
exit 23
""",
    )
    result = _run_bash(
        _action_step("Connect cluster to Arc + enable features")["run"].replace(
            "RETRY_DELAY=60", "RETRY_DELAY=0"
        ),
        tmp_path,
        env={
            "PRIVATE_PROVIDER_ERRORS": "false",
            "PRIVATE_SENTINEL": PRIVATE_SENTINEL,
            "CLUSTER_NAME": "arc-private",
            "RESOURCE_GROUP": "rg-private",
            "LOCATION": "eastus2",
            "CUSTOM_LOCATIONS_OID": "",
        },
    )
    assert result.returncode != 0
    assert PRIVATE_SENTINEL in result.stderr
    assert not list((tmp_path / "private-temp").iterdir())


@pytest.mark.parametrize("private", ["true", "false"])
def test_arc_provider_success_keeps_connect_and_issuer_outputs(tmp_path, private):
    bin_dir = _fake_bin(tmp_path)
    _write_executable(
        bin_dir / "az",
        """#!/usr/bin/env bash
printf '%s\\n' "$2" >> "$FAKE_AZ_LOG"
if [[ "$2" == "show" ]]; then
  printf '%s\\n' 'https://example.test/issuer'
fi
""",
    )
    env = {
        "PRIVATE_PROVIDER_ERRORS": private,
        "FAKE_AZ_LOG": _bash_path(tmp_path / "az-commands.log"),
        "RUNNER_TEMP": _bash_path(tmp_path / "private-temp"),
        "GITHUB_OUTPUT": _bash_path(tmp_path / "outputs.txt"),
        "CLUSTER_NAME": "arc-private",
        "RESOURCE_GROUP": "rg-private",
        "LOCATION": "eastus2",
        "CUSTOM_LOCATIONS_OID": "",
    }
    for step_name in (
        "Connect cluster to Arc + enable features",
        "Enable OIDC issuer + workload identity",
        "Capture OIDC issuer URL",
    ):
        result = _run_bash(_action_step(step_name)["run"], tmp_path, env=env)
        assert result.returncode == 0, result.stderr
    assert (tmp_path / "az-commands.log").read_text(encoding="utf-8").splitlines() == [
        "connect", "enable-features", "update", "show",
    ]
    assert (tmp_path / "outputs.txt").read_text(encoding="utf-8").strip() == (
        "url=https://example.test/issuer"
    )


@pytest.mark.parametrize(
    ("mode", "expected_exit", "expected_message"),
    [
        ("connected", 0, "Arc cluster is Connected"),
        ("pending", 1, "Arc cluster did not reach Connected"),
        ("failed", 1, "Arc connectivity could not be queried"),
    ],
)
@pytest.mark.parametrize("private", ["true", "false"])
def test_wait_connected_omits_private_status_errors_and_arguments(
    tmp_path,
    mode,
    expected_exit,
    expected_message,
    private,
):
    bin_dir = _fake_bin(tmp_path)
    _write_executable(
        bin_dir / "sleep",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_SLEEP_LOG\"\n",
    )
    _write_executable(
        bin_dir / "az",
        """#!/usr/bin/env bash
printf 'query\\n' >> "$FAKE_AZ_LOG"
case "$FAKE_AZ_MODE" in
  connected)
    printf 'Connected\n'
    exit 0
    ;;
  pending)
    printf '%s\n' "Pending-$PRIVATE_SENTINEL"
    printf '%s\n' "$PRIVATE_SENTINEL" >&2
    exit 0
    ;;
  failed)
    printf '%s\n' "$PRIVATE_SENTINEL"
    printf '%s\n' "$PRIVATE_SENTINEL" >&2
    exit 42
    ;;
esac
""",
    )

    # Use the LF source text a Linux Git checkout provides, without changing its logic.
    script = (
        f"set -- {shlex.quote(PRIVATE_SENTINEL)} {shlex.quote(PRIVATE_SENTINEL)}\n"
        + WAIT_SCRIPT.read_text(encoding="utf-8")
    )
    result = _run_bash(
        script,
        tmp_path,
        env={
            "FAKE_AZ_MODE": mode,
            "PRIVATE_SENTINEL": PRIVATE_SENTINEL,
            "FAKE_AZ_LOG": _bash_path(tmp_path / "queries.log"),
            "FAKE_SLEEP_LOG": _bash_path(tmp_path / "sleeps.log"),
            "PRIVATE_PROVIDER_ERRORS": private,
            "RUNNER_TEMP": _bash_path(tmp_path / "private-temp"),
        },
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == expected_exit
    assert expected_message in combined
    assert PRIVATE_SENTINEL not in combined
    queries = (tmp_path / "queries.log").read_text(encoding="utf-8").splitlines()
    assert len(queries) == (1 if mode == "connected" else 20)
    if mode == "connected":
        assert not (tmp_path / "sleeps.log").exists()
    else:
        assert (tmp_path / "sleeps.log").read_text(encoding="utf-8").splitlines() == (
            ["15"] * 19
        )
    if private == "true":
        diagnostic = tmp_path / "private-temp" / "connectedk8s-diagnostics" / (
            "wait-connected.err"
        )
        assert diagnostic.is_file()
        if mode != "connected":
            assert PRIVATE_SENTINEL in diagnostic.read_text(encoding="utf-8")
        if os.name != "nt":
            assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    else:
        assert not list((tmp_path / "private-temp").iterdir())


def _oidc_verification_script() -> str:
    return _action_step("Verify OIDC discovery endpoint")["run"]


@pytest.mark.parametrize(
    ("mode", "expected_exit", "expected_message"),
    [
        ("match", 0, "OIDC discovery issuer verified."),
        (
            "mismatch",
            1,
            "OIDC discovery issuer does not match the configured issuer.",
        ),
        ("tool-failure", 1, "OIDC discovery endpoint could not be read."),
        (
            "malformed",
            1,
            "OIDC discovery endpoint returned an invalid issuer document.",
        ),
    ],
)
def test_oidc_verification_omits_private_values_and_tool_diagnostics(
    tmp_path,
    mode,
    expected_exit,
    expected_message,
):
    bin_dir = _fake_bin(tmp_path)
    _write_executable(
        bin_dir / "kubectl",
        """#!/usr/bin/env bash
case "$FAKE_KUBECTL_MODE" in
  match)
    printf '{"issuer":"%s"}\n' "$EXPECTED_ISSUER"
    ;;
  mismatch)
    printf '{"issuer":"%s"}\n' "$PRIVATE_SENTINEL"
    ;;
  malformed)
    printf '%s\n' "$PRIVATE_SENTINEL"
    ;;
  tool-failure)
    printf '%s\n' '{"issuer":"unused"}'
    printf '%s\n' "$PRIVATE_SENTINEL"
    printf '%s\n' "$PRIVATE_SENTINEL" >&2
    exit 31
    ;;
esac
""",
    )

    result = _run_bash(
        _oidc_verification_script(),
        tmp_path,
        env={
            "EXPECTED_ISSUER": (
                f"https://expected-{PRIVATE_SENTINEL}.example/"
            ),
            "FAKE_KUBECTL_MODE": mode,
            "PRIVATE_SENTINEL": PRIVATE_SENTINEL,
        },
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == expected_exit
    assert expected_message in combined
    assert PRIVATE_SENTINEL not in combined
    assert not list((tmp_path / "private-temp").iterdir())
