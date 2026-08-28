"""Fast contracts for the integration Azure CLI helper."""

import subprocess

import pytest

from tests.integration.helpers.azure import run_az

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/rg-private/providers/Microsoft.Example/widgets/private"
)


def test_timeout_does_not_expose_the_argument_vector(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "az.cmd")

    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(RuntimeError) as excinfo:
        run_az(["az", "resource", "show", "--ids", RESOURCE_ID])

    assert RESOURCE_ID not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_failure_redacts_resource_ids_and_explicit_values(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "az.cmd")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr=f"request for {RESOURCE_ID} carried sensitive-value",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        run_az(
            ["az", "resource", "show", "--ids", RESOURCE_ID],
            redact=("sensitive-value",),
        )

    message = str(excinfo.value)
    assert RESOURCE_ID not in message
    assert "sensitive-value" not in message
    assert "<Microsoft.Example/widgets>" in message
    assert "***" in message


def test_missing_azure_cli_fails_clearly(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match=r"Azure CLI \(`az`\) is required"):
        run_az(["az", "resource", "show"])


def test_success_returns_raw_stdout_to_the_parsing_caller(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "az.cmd")
    seen = {}

    def succeed(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"value": "raw"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", succeed)

    result = run_az(["az", "resource", "show"])

    assert seen["command"][0] == "az.cmd"
    assert result.stdout == '{"value": "raw"}'


def test_os_error_does_not_expose_the_argument_vector(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "az.cmd")

    def fail_to_start(*args, **kwargs):
        raise OSError("launch failed")

    monkeypatch.setattr(subprocess, "run", fail_to_start)

    with pytest.raises(RuntimeError) as excinfo:
        run_az(["az", "resource", "show", "--ids", RESOURCE_ID])

    assert RESOURCE_ID not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
