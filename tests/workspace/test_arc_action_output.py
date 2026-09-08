"""Public-output boundaries for the Arc composite action."""

import os
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
    temp_dir.mkdir()
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


@pytest.mark.parametrize(
    ("mode", "expected_exit", "expected_message"),
    [
        ("connected", 0, "Arc cluster is Connected"),
        ("pending", 1, "Arc cluster did not reach Connected"),
        ("failed", 1, "Arc connectivity could not be queried"),
    ],
)
def test_wait_connected_omits_private_status_errors_and_arguments(
    tmp_path,
    mode,
    expected_exit,
    expected_message,
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
    assert not list((tmp_path / "private-temp").iterdir())


def _oidc_verification_script() -> str:
    data = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    step = next(
        step
        for step in data["runs"]["steps"]
        if step.get("name") == "Verify OIDC discovery endpoint"
    )
    return step["run"]


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
