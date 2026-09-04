"""Tests for concurrent prepared-target execution using in-process fakes.

These cover worker-count clamping, per-target failure isolation, and result
accumulation under contention. Never mock a process handle without also
patching the kernel calls in its cleanup path.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from siteops.models import DeploymentStep, Manifest, Site
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    DataReference,
    DeploymentOperation,
    DeploymentPlan,
    InputStatus,
    MappingValue,
    OperationIdentity,
    OperationKind,
    OperationScope,
    PlanBuildResult,
    PlanDisposition,
    PlanExecutionMode,
    PlanIntent,
    PlanSkipReason,
    PlanStatus,
    PlanStep,
    PreparedOperation,
    PreparedTarget,
    SkipReasonCode,
    TargetKind,
)

TIMESTAMP = "20260728T000000"


def _make_manifest(step_count: int = 2) -> Manifest:
    return Manifest(
        name="parallel-test",
        description="",
        sites=[],
        steps=[
            DeploymentStep(name=f"step{i}", template=f"templates/step{i}.bicep")
            for i in range(step_count)
        ],
    )


def _make_sites(count: int, *, subscription: str = "sub-a") -> list[Site]:
    return [
        Site(
            name=f"site-{i}",
            subscription=subscription,
            resource_group=f"rg-{i}",
            location="eastus",
            labels={},
        )
        for i in range(count)
    ]


def _ok_result(site: Site, manifest: Manifest) -> dict:
    return {
        "site": site.name,
        "status": "success",
        "steps_completed": len(manifest.steps),
        "steps_skipped": 0,
        "steps_total": len(manifest.steps),
        "elapsed": 0.0,
        "steps": [],
    }


def _prepared_plan(
    sites: list[Site],
    *,
    parallel_sites: int,
) -> DeploymentPlan:
    details = DeploymentOperation(
        template=Path("template.json"),
        input_status=InputStatus.PREPARED,
        parameters=MappingValue(()),
    )
    step = PlanStep(
        name="deploy",
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=details,
    )
    targets = tuple(
        PreparedTarget(
            name=site.name,
            kind=TargetKind.RESOURCE_GROUP,
            subscription=site.subscription,
            resource_group=site.resource_group,
            location=site.location,
            operations=(
                PreparedOperation(
                    identity=OperationIdentity(
                        target=site.name,
                        step=step.name,
                    ),
                    step=step,
                    disposition=PlanDisposition.EXECUTE,
                    details=details,
                ),
            ),
        )
        for site in sites
    )
    return DeploymentPlan(
        manifest_name="parallel-test",
        source_path=Path("manifests/test.yaml"),
        intent=PlanIntent.EXECUTABLE,
        description=None,
        max_parallel_sites=parallel_sites,
        steps=(step,),
        targets=targets,
    )


def _prepared_ok_result(
    target: PreparedTarget,
    plan: DeploymentPlan,
) -> dict:
    return {
        "site": target.name,
        "status": "success",
        "error": None,
        "steps_completed": len(plan.steps),
        "steps_skipped": 0,
        "steps_total": len(plan.steps),
        "elapsed": 0.0,
        "steps": [],
    }


class TestPreparedTargetFanOut:
    """Prepared fan-out covers clamping, isolation, and accumulation."""

    def test_every_target_produces_exactly_one_result(self, tmp_workspace):
        sites = _make_sites(5)
        plan = _prepared_plan(sites, parallel_sites=3)
        orchestrator = Orchestrator(tmp_workspace)

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=lambda plan, target, *args, **kwargs: (
                _prepared_ok_result(target, plan),
                {},
            ),
        ):
            results, _ = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
            )

        assert len(results) == len(sites)
        assert {result["site"] for result in results} == {
            site.name for site in sites
        }

    @pytest.mark.parametrize(
        ("site_count", "parallel_sites", "expected_workers"),
        [
            (5, 3, 3),
            (2, 5, 2),
            (4, 0, 4),
        ],
    )
    def test_worker_count_is_clamped(
        self,
        tmp_workspace,
        site_count,
        parallel_sites,
        expected_workers,
    ):
        sites = _make_sites(site_count)
        plan = _prepared_plan(
            sites,
            parallel_sites=parallel_sites,
        )
        orchestrator = Orchestrator(tmp_workspace)
        observed = {}
        from concurrent.futures import ThreadPoolExecutor as RealPool

        def recording_pool(max_workers=None, **kwargs):
            observed["max_workers"] = max_workers
            return RealPool(max_workers=max_workers, **kwargs)

        with (
            patch.object(
                orchestrator,
                "_execute_prepared_target",
                side_effect=lambda plan, target, *args, **kwargs: (
                    _prepared_ok_result(target, plan),
                    {},
                ),
            ),
            patch(
                "siteops.orchestrator.ThreadPoolExecutor",
                side_effect=recording_pool,
            ),
        ):
            orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
            )

        assert observed["max_workers"] == expected_workers

    @pytest.mark.parametrize(
        "parallel_sites",
        [1, 4],
        ids=["sequential", "parallel"],
    )
    def test_one_failure_does_not_stop_other_targets(
        self,
        tmp_workspace,
        parallel_sites,
    ):
        sites = _make_sites(4)
        plan = _prepared_plan(
            sites,
            parallel_sites=parallel_sites,
        )
        orchestrator = Orchestrator(tmp_workspace)

        def execute(plan, target, *args, **kwargs):
            if target.name == "site-2":
                raise RuntimeError("boom")
            return _prepared_ok_result(target, plan), {}

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=execute,
        ):
            results, _ = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
            )

        assert len(results) == len(sites)
        by_site = {result["site"]: result for result in results}
        assert by_site["site-2"]["status"] == "failed"
        assert "boom" in by_site["site-2"]["error"]
        assert all(
            by_site[name]["status"] == "success"
            for name in ("site-0", "site-1", "site-3")
        )

    def test_results_are_not_lost_under_contention(self, tmp_workspace):
        site_count = 12
        sites = _make_sites(site_count)
        plan = _prepared_plan(
            sites,
            parallel_sites=site_count,
        )
        orchestrator = Orchestrator(tmp_workspace)
        barrier = threading.Barrier(site_count, timeout=30)

        def execute(plan, target, *args, **kwargs):
            barrier.wait()
            return _prepared_ok_result(target, plan), {}

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=execute,
        ):
            results, _ = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
            )

        assert len(results) == site_count
        assert {result["site"] for result in results} == {
            site.name for site in sites
        }

    def test_parallel_mode_reaches_each_target(self, tmp_workspace):
        sites = _make_sites(3)
        plan = _prepared_plan(sites, parallel_sites=3)
        orchestrator = Orchestrator(tmp_workspace)
        seen = []

        def execute(
            plan,
            target,
            timestamp,
            inherited_outputs,
            *,
            parallel_mode,
            execution_mode,
        ):
            seen.append(parallel_mode)
            return _prepared_ok_result(target, plan), {}

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=execute,
        ):
            orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
            )

        assert seen == [True, True, True]


class TestSubscriptionFailureBlastRadius:
    """Which resource-group sites proceed after a subscription-scoped failure.

    A failed subscription phase must stop only the sites that consume its
    outputs. Blocking more than that halts a fleet unnecessarily. Blocking
    less sends sites into a deploy whose inputs never resolved.
    """

    def _manifest_with_subscription_step(self) -> Manifest:
        return Manifest(
            name="two-phase",
            description="",
            sites=[],
            steps=[
                DeploymentStep(
                    name="edge-site",
                    template="templates/edge-site.bicep",
                    scope="subscription",
                ),
                DeploymentStep(name="aio", template="templates/aio.bicep"),
            ],
        )

    def _run_deploy(self, tmp_workspace, sites, *, depends: bool):
        manifest = self._manifest_with_subscription_step()
        orchestrator = Orchestrator(tmp_workspace)
        sub_site = next(s for s in sites if s.is_subscription_level)
        deployment_details = DeploymentOperation(
            template=Path("template.json"),
            input_status=InputStatus.PREPARED,
            parameters=MappingValue(()),
        )
        subscription_step = PlanStep(
            name="edge-site",
            sequence=1,
            kind=OperationKind.DEPLOYMENT,
            scope=OperationScope.SUBSCRIPTION,
            details=deployment_details,
        )
        resource_group_step = PlanStep(
            name="aio",
            sequence=2,
            kind=OperationKind.DEPLOYMENT,
            scope=OperationScope.RESOURCE_GROUP,
            details=deployment_details,
        )
        plan_targets: list[PreparedTarget] = []
        for site in sites:
            if site.is_subscription_level:
                operations = (
                    PreparedOperation(
                        identity=OperationIdentity(
                            target=site.name,
                            step="edge-site",
                        ),
                        step=subscription_step,
                        disposition=PlanDisposition.EXECUTE,
                        details=deployment_details,
                    ),
                    PreparedOperation(
                        identity=OperationIdentity(
                            target=site.name,
                            step="aio",
                        ),
                        step=resource_group_step,
                        disposition=PlanDisposition.SKIP,
                        details=deployment_details,
                        skip_reason=PlanSkipReason(
                            code=SkipReasonCode.SCOPE_MISMATCH,
                            detail=(
                                "resourceGroup-scoped step, site has no "
                                "resource group"
                            ),
                        ),
                    ),
                )
                kind = TargetKind.SUBSCRIPTION
                resource_group = None
            else:
                data_references = ()
                if depends and site.subscription == sub_site.subscription:
                    data_references = (
                        DataReference(
                            source=OperationIdentity(
                                target=sub_site.name,
                                step="edge-site",
                            ),
                            output_path=("value",),
                        ),
                    )
                operations = (
                    PreparedOperation(
                        identity=OperationIdentity(
                            target=site.name,
                            step="edge-site",
                        ),
                        step=subscription_step,
                        disposition=PlanDisposition.SKIP,
                        details=deployment_details,
                        skip_reason=PlanSkipReason(
                            code=SkipReasonCode.SCOPE_MISMATCH,
                            detail=(
                                "subscription-scoped step, site has resource "
                                "group"
                            ),
                        ),
                    ),
                    PreparedOperation(
                        identity=OperationIdentity(
                            target=site.name,
                            step="aio",
                        ),
                        step=resource_group_step,
                        disposition=PlanDisposition.EXECUTE,
                        details=deployment_details,
                        data_references=data_references,
                    ),
                )
                kind = TargetKind.RESOURCE_GROUP
                resource_group = site.resource_group
            plan_targets.append(
                PreparedTarget(
                    name=site.name,
                    kind=kind,
                    subscription=site.subscription,
                    resource_group=resource_group,
                    location=site.location,
                    operations=operations,
                )
            )
        plan = DeploymentPlan(
            manifest_name=manifest.name,
            source_path=Path("manifests/two-phase.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=manifest.parallel.sites,
            steps=(subscription_step, resource_group_step),
            targets=tuple(plan_targets),
        )
        plan_result = PlanBuildResult(
            status=PlanStatus.PLANNED,
            executable=True,
            plan=plan,
        )
        failed_phase_one = [
            {
                "site": sub_site.name,
                "status": "failed",
                "error": "subscription step failed",
                "steps_completed": 0,
                "steps_skipped": 0,
                "steps_total": len(manifest.steps),
                "elapsed": 0.0,
                "steps": [],
            }
        ]
        deployed: list[list[str]] = []

        def _run_targets(plan, phase_targets, *args, **kwargs):
            if phase_targets[0].kind is TargetKind.SUBSCRIPTION:
                return failed_phase_one, {}
            deployed.append([target.name for target in phase_targets])
            site_lookup = {site.name: site for site in sites}
            return (
                [
                    _ok_result(site_lookup[target.name], manifest)
                    for target in phase_targets
                ],
                {},
            )

        with patch.object(
            orchestrator,
            "_run_prepared_targets",
            side_effect=_run_targets,
        ):
            summary = orchestrator.execute_plan(plan_result)

        phase_two = deployed[0] if deployed else []
        return summary, phase_two

    def test_dependent_site_is_blocked(self, tmp_workspace):
        sites = [
            Site(name="global-a", subscription="sub-a", resource_group="", location="eastus", labels={}),
            Site(
                name="edge-a",
                subscription="sub-a",
                resource_group="rg-a",
                location="eastus",
                labels={},
            ),
        ]

        summary, phase_two = self._run_deploy(tmp_workspace, sites, depends=True)

        assert "edge-a" not in phase_two
        blocked = summary["sites"]["edge-a"]
        assert blocked["status"] == "blocked"
        assert blocked["steps_completed"] == 0
        assert blocked["steps_skipped"] == 2

    def test_redacted_block_notice_omits_the_site(
        self,
        tmp_workspace,
        capsys,
        monkeypatch,
    ):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
        sites = [
            Site(
                name="private-global",
                subscription="sub-a",
                resource_group="",
                location="eastus",
                labels={},
            ),
            Site(
                name="private-edge",
                subscription="sub-a",
                resource_group="rg-a",
                location="eastus",
                labels={},
            ),
        ]

        summary, _ = self._run_deploy(tmp_workspace, sites, depends=True)

        output = capsys.readouterr().out
        assert "private-edge" not in output
        assert "[<site>] - blocked" in output
        assert summary["sites"]["private-edge"]["status"] == "blocked"

    def test_independent_site_in_failed_subscription_proceeds(self, tmp_workspace):
        sites = [
            Site(name="global-a", subscription="sub-a", resource_group="", location="eastus", labels={}),
            Site(
                name="edge-a",
                subscription="sub-a",
                resource_group="rg-a",
                location="eastus",
                labels={},
            ),
        ]

        summary, phase_two = self._run_deploy(tmp_workspace, sites, depends=False)

        assert phase_two == ["edge-a"]
        assert summary["sites"]["edge-a"]["status"] == "success"

    def test_site_in_healthy_subscription_is_unaffected(self, tmp_workspace):
        sites = [
            Site(name="global-a", subscription="sub-a", resource_group="", location="eastus", labels={}),
            Site(
                name="edge-b",
                subscription="sub-b",
                resource_group="rg-b",
                location="eastus",
                labels={},
            ),
        ]

        # depends=True proves the healthy subscription is exempted by subscription
        # identity, not by the dependency scan.
        summary, phase_two = self._run_deploy(tmp_workspace, sites, depends=True)

        assert phase_two == ["edge-b"]
        assert summary["sites"]["edge-b"]["status"] == "success"


class TestAnInterruptedFleetStops:
    """An operator who interrupts a rollout has decided to stop it.

    Leaving the thread pool waits for everything already queued, so a fleet
    that had eight sites submitted still deployed to all eight after the
    interrupt. The sequential path stopped where it was told to.
    """

    def _sites(self, count):
        return [
            Site(
                name=f"plant-{index:02d}",
                subscription="00000000-0000-0000-0000-000000000000",
                resource_group=f"rg-plant-{index:02d}",
                location="eastus",
                labels={},
            )
            for index in range(count)
        ]

    def test_the_sites_not_yet_started_are_not_deployed(self, tmp_workspace):
        started: list[str] = []
        gate = threading.Event()

        def execute_target(
            self_,
            plan,
            target,
            *args,
            **kwargs,
        ):
            started.append(target.name)
            if len(started) == 2:
                gate.set()
            # Hold the two running workers so the rest stay queued, which is
            # the state the interrupt has to stop.
            gate.wait(timeout=5)
            if len(started) <= 2:
                raise KeyboardInterrupt("operator stopped the rollout")
            return _prepared_ok_result(target, plan), {}

        orchestrator = Orchestrator(tmp_workspace)
        sites = self._sites(8)
        plan = _prepared_plan(sites, parallel_sites=2)

        with (
            patch.object(
                Orchestrator,
                "_execute_prepared_target",
                execute_target,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
            )

        assert len(started) < len(sites), (
            f"every site deployed despite the interrupt: {started}"
        )
