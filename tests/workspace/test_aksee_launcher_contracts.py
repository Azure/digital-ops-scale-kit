"""Static contracts for AKS Edge Essentials host launchers."""

from pathlib import Path

import pytest

WORKSPACE = Path(__file__).parents[2] / "workspaces" / "iot-operations"
BOOTSTRAP = WORKSPACE / "templates" / "host-bootstrap" / "aksee"
UPGRADE = WORKSPACE / "templates" / "host-ops" / "aksee-upgrade"
MAX_INLINE_SCRIPT_BYTES = 38_000


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_bootstrap_bicep_passes_operation_identity() -> None:
    template = _read(BOOTSTRAP / "template.bicep")

    assert "param runId string = utcNow()" in template
    assert "{ name: 'MachineName',        value: machineName }" in template
    assert "{ name: 'RunId',              value: runId }" in template
    assert "output runId string = runId" in template


def test_bootstrap_launcher_revalidates_completed_state() -> None:
    launcher = _read(BOOTSTRAP / "scripts" / "launcher-template.ps1")

    assert "[Parameter(Mandatory)] [string]$MachineName" in launcher
    assert "[Parameter(Mandatory)] [string]$RunId" in launcher
    assert "$initialPhase = 3" in launcher
    assert "Set-RunningTag -Required:($initialPhase -eq 3)" in launcher
    assert "ALREADY-BOOTSTRAPPED" not in launcher
    assert launcher.index("Set-RunningTag -Required:") < launcher.index(
        "Start-ScheduledTask -TaskName"
    )


def test_bootstrap_worker_targets_configured_machine() -> None:
    worker = _read(BOOTSTRAP / "scripts" / "worker.ps1")

    assert "$name = [string](Get-Prop $config 'machineName' '')" in worker
    assert '"siteops.bootstrap.runId=$runId"' in worker
    assert "for ($attempt = 1; $attempt -le 3; $attempt++)" in worker
    assert "$name = $env:COMPUTERNAME" not in worker


@pytest.mark.parametrize(
    "launcher",
    [
        BOOTSTRAP / "scripts" / "Install-AksEeBootstrap.min.ps1",
        UPGRADE / "scripts" / "Install-AksEeUpgrade.min.ps1",
    ],
)
def test_minified_launcher_fits_inline_delivery(launcher: Path) -> None:
    assert launcher.stat().st_size <= MAX_INLINE_SCRIPT_BYTES
