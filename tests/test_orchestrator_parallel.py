"""Tests for concurrent prepared-target execution using in-process fakes.

These cover worker-count clamping, per-target failure isolation, and result
accumulation under contention. Never mock a process handle without also
patching the kernel calls in its cleanup path.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from siteops.compilation import (
    CompilationKey,
    DependencyCoverage,
    DependencyIdentity,
    PreparedTemplateUnit,
    SourceIdentity,
    TemplateCompilationIdentity,
    TemplateKind,
    VersionProvenance,
)
from siteops.models import DeploymentStep, Manifest, Site
from siteops.orchestrator import (
    Orchestrator,
    _ProgressOwner,
    _TargetExecution,
)
from siteops.planning import (
    CapabilityKind,
    CapabilityProviderIdentity,
    CapabilityStatus,
    DataReference,
    DeploymentOperation,
    DeploymentPlan,
    InputStatus,
    MappingValue,
    OperationIdentity,
    OperationKind,
    OperationScope,
    PlanBuildResult,
    PlanCapability,
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
from siteops.reporting import TextProgressReporter
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    OutcomeReasonCode,
    SiteResult,
    SiteStatus,
)

TIMESTAMP = "20260728T000000"


def _template_unit() -> PreparedTemplateUnit:
    source = SourceIdentity(
        path=Path("template.json"),
        content_digest="source",
        size_bytes=1,
    )
    key = CompilationKey(
        source_path=source.path,
        source_content_digest=source.content_digest,
        template_kind=TemplateKind.ARM_JSON,
        compiler_fingerprint="arm-json",
        configuration_digest="none",
        invocation=("read-arm-json",),
    )
    return PreparedTemplateUnit(
        key=key,
        identity=TemplateCompilationIdentity(
            source=source,
            compiler_driver=None,
            compiler=None,
            configuration=None,
            dependencies=DependencyIdentity(
                coverage=DependencyCoverage.NOT_APPLICABLE,
            ),
            compiled_output_digest=source.content_digest,
        ),
        parameters=(),
    )


def _arm_capability(
    identities: tuple[OperationIdentity, ...],
) -> PlanCapability:
    return PlanCapability(
        kind=CapabilityKind.ARM_CONTROL_PLANE,
        status=CapabilityStatus.AVAILABLE,
        required_by=identities,
        provider=CapabilityProviderIdentity(
            name="azure-cli",
            executable_path=Path("C:/tools/az.exe"),
            version=None,
            version_provenance=VersionProvenance.UNKNOWN,
        ),
    )


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


def _prepared_plan(
    sites: list[Site],
    *,
    parallel_sites: int,
) -> DeploymentPlan:
    unit = _template_unit()
    described_details = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.DESCRIBED,
    )
    prepared_details = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.PREPARED,
        parameters=MappingValue(()),
        template_unit_key=unit.key,
    )
    step = PlanStep(
        name="deploy",
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=described_details,
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
                    details=prepared_details,
                ),
            ),
        )
        for site in sites
    )
    required_by = tuple(
        target.operations[0].identity
        for target in targets
    )
    return DeploymentPlan(
        manifest_name="parallel-test",
        source_path=Path("manifests/test.yaml"),
        intent=PlanIntent.EXECUTABLE,
        description=None,
        max_parallel_sites=parallel_sites,
        steps=(step,),
        targets=targets,
        template_units=(unit,),
        capabilities=(_arm_capability(required_by),),
    )


def _prepared_ok_result(target: PreparedTarget) -> SiteResult:
    operations = tuple(
        OperationResult(
            identity=operation.identity,
            kind=operation.kind,
            status=(
                OperationStatus.SUCCEEDED
                if operation.disposition is PlanDisposition.EXECUTE
                else OperationStatus.SKIPPED
            ),
            elapsed=0.0,
            reason=(
                None
                if operation.disposition is PlanDisposition.EXECUTE
                else OutcomeReason(
                    OutcomeReasonCode.SCOPE_MISMATCH,
                    private_detail=operation.skip_reason.detail,
                )
            ),
        )
        for operation in target.operations
    )
    return SiteResult.from_operations(
        target=target.name,
        kind=target.kind,
        operations=operations,
        elapsed=0.0,
    )


def _failed_result(target: PreparedTarget) -> SiteResult:
    reason = OutcomeReason(
        OutcomeReasonCode.OPERATION_FAILED,
        private_detail="subscription step failed",
    )
    operations = []
    failed = False
    for operation in target.operations:
        if (
            not failed
            and operation.disposition is PlanDisposition.EXECUTE
        ):
            operations.append(
                OperationResult(
                    identity=operation.identity,
                    kind=operation.kind,
                    status=OperationStatus.FAILED,
                    elapsed=0.0,
                    reason=reason,
                )
            )
            failed = True
        elif operation.disposition is PlanDisposition.EXECUTE:
            operations.append(
                OperationResult(
                    identity=operation.identity,
                    kind=operation.kind,
                    status=OperationStatus.NOT_RUN,
                    elapsed=0.0,
                    reason=OutcomeReason(
                        OutcomeReasonCode.EARLIER_OPERATION_FAILED
                    ),
                )
            )
        else:
            operations.append(
                OperationResult(
                    identity=operation.identity,
                    kind=operation.kind,
                    status=OperationStatus.SKIPPED,
                    elapsed=0.0,
                    reason=OutcomeReason(
                        OutcomeReasonCode.SCOPE_MISMATCH,
                        private_detail=operation.skip_reason.detail,
                    ),
                )
            )
    return SiteResult.from_operations(
        target=target.name,
        kind=target.kind,
        operations=tuple(operations),
        elapsed=0.0,
        reason=reason,
    )


class TestPreparedTargetFanOut:
    """Prepared fan-out covers clamping, isolation, and accumulation."""

    def test_every_target_produces_exactly_one_result(self, tmp_workspace):
        sites = _make_sites(5)
        plan = _prepared_plan(sites, parallel_sites=3)
        orchestrator = Orchestrator(tmp_workspace)

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=lambda plan, target, *args, **kwargs: _TargetExecution(
                site=_prepared_ok_result(target),
                outputs={},
            ),
        ):
            results, _, interrupted = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
                progress=_ProgressOwner(None),
            )

        assert interrupted is False
        assert len(results) == len(sites)
        assert [result.target for result in results] == [
            site.name for site in sites
        ]

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
                side_effect=lambda plan, target, *args, **kwargs: _TargetExecution(
                    site=_prepared_ok_result(target),
                    outputs={},
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
                progress=_ProgressOwner(None),
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
            return _TargetExecution(
                site=_prepared_ok_result(target),
                outputs={},
            )

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=execute,
        ):
            results, _, interrupted = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
                progress=_ProgressOwner(None),
            )

        assert interrupted is False
        assert len(results) == len(sites)
        by_site = {result.target: result for result in results}
        assert by_site["site-2"].status is SiteStatus.FAILED
        assert "boom" in by_site["site-2"].reason.private_detail
        assert all(
            by_site[name].status is SiteStatus.SUCCEEDED
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
            return _TargetExecution(
                site=_prepared_ok_result(target),
                outputs={},
            )

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=execute,
        ):
            results, _, interrupted = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
                progress=_ProgressOwner(None),
            )

        assert interrupted is False
        assert len(results) == site_count
        assert [result.target for result in results] == [
            site.name for site in sites
        ]

    def test_parallel_completion_keeps_plan_order(self, tmp_workspace):
        sites = _make_sites(3)
        plan = _prepared_plan(sites, parallel_sites=3)
        orchestrator = Orchestrator(tmp_workspace)

        def execute(
            plan,
            target,
            timestamp,
            inherited_outputs,
            *,
            execution_mode,
            progress,
            state,
        ):
            time.sleep((2 - int(target.name[-1])) * 0.01)
            return _TargetExecution(
                site=_prepared_ok_result(target),
                outputs={},
            )

        with patch.object(
            orchestrator,
            "_execute_prepared_target",
            side_effect=execute,
        ):
            results, _, _ = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
                progress=_ProgressOwner(None),
            )

        assert [result.target for result in results] == [
            "site-0",
            "site-1",
            "site-2",
        ]


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

    def _run_deploy(
        self,
        tmp_workspace,
        sites,
        *,
        depends: bool,
        progress=None,
    ):
        manifest = self._manifest_with_subscription_step()
        orchestrator = Orchestrator(tmp_workspace)
        sub_site = next(s for s in sites if s.is_subscription_level)
        unit = _template_unit()
        described_details = DeploymentOperation(
            template=unit.identity.source.path,
            input_status=InputStatus.DESCRIBED,
        )
        prepared_details = DeploymentOperation(
            template=unit.identity.source.path,
            input_status=InputStatus.PREPARED,
            parameters=MappingValue(()),
            template_unit_key=unit.key,
        )
        subscription_step = PlanStep(
            name="edge-site",
            sequence=1,
            kind=OperationKind.DEPLOYMENT,
            scope=OperationScope.SUBSCRIPTION,
            details=described_details,
        )
        resource_group_step = PlanStep(
            name="aio",
            sequence=2,
            kind=OperationKind.DEPLOYMENT,
            scope=OperationScope.RESOURCE_GROUP,
            details=described_details,
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
                        details=prepared_details,
                    ),
                    PreparedOperation(
                        identity=OperationIdentity(
                            target=site.name,
                            step="aio",
                        ),
                        step=resource_group_step,
                        disposition=PlanDisposition.SKIP,
                        details=described_details,
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
                        details=described_details,
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
                        details=prepared_details,
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
        required_by = tuple(
            operation.identity
            for target in plan_targets
            for operation in target.operations
            if operation.disposition is PlanDisposition.EXECUTE
        )
        plan = DeploymentPlan(
            manifest_name=manifest.name,
            source_path=Path("manifests/two-phase.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=manifest.parallel.sites,
            steps=(subscription_step, resource_group_step),
            targets=tuple(plan_targets),
            template_units=(unit,),
            capabilities=(_arm_capability(required_by),),
        )
        plan_result = PlanBuildResult(
            status=PlanStatus.PLANNED,
            executable=True,
            plan=plan,
        )
        subscription_target = next(
            target
            for target in plan.targets
            if target.name == sub_site.name
        )
        failed_phase_one = [_failed_result(subscription_target)]
        deployed: list[list[str]] = []

        def _run_targets(plan, phase_targets, *args, **kwargs):
            if phase_targets[0].kind is TargetKind.SUBSCRIPTION:
                return failed_phase_one, {}, False
            deployed.append([target.name for target in phase_targets])
            return (
                [
                    _prepared_ok_result(target)
                    for target in phase_targets
                ],
                {},
                False,
            )

        with patch.object(
            orchestrator,
            "_run_prepared_targets",
            side_effect=_run_targets,
        ):
            summary = orchestrator.execute_plan(
                plan_result,
                progress=progress,
            )

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
        blocked = next(
            site for site in summary.sites if site.target == "edge-a"
        )
        assert blocked.status is SiteStatus.NOT_RUN
        assert [operation.status for operation in blocked.operations] == [
            OperationStatus.SKIPPED,
            OperationStatus.NOT_RUN,
        ]
        assert blocked.failure_reason() is not None
        assert blocked.failure_reason().code is (
            OutcomeReasonCode.DEPENDENCY_UNAVAILABLE
        )

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

        summary, _ = self._run_deploy(
            tmp_workspace,
            sites,
            depends=True,
            progress=TextProgressReporter(sys.stdout, redacted=True),
        )

        output = capsys.readouterr().out
        assert "private-edge" not in output
        assert "[<site>] - blocked" in output
        blocked = next(
            site
            for site in summary.sites
            if site.target == "private-edge"
        )
        assert blocked.status is SiteStatus.NOT_RUN

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
        edge = next(
            site for site in summary.sites if site.target == "edge-a"
        )
        assert edge.status is SiteStatus.SUCCEEDED

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
        edge = next(
            site for site in summary.sites if site.target == "edge-b"
        )
        assert edge.status is SiteStatus.SUCCEEDED


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
            state = kwargs["state"]
            state.start()
            assert state.begin(target.operations[0])
            started.append(target.name)
            if len(started) == 2:
                gate.set()
            # Hold the two running workers so the rest stay queued, which is
            # the state the interrupt has to stop.
            gate.wait(timeout=5)
            if len(started) <= 2:
                raise KeyboardInterrupt("operator stopped the rollout")
            return _TargetExecution(
                site=_prepared_ok_result(target),
                outputs={},
            )

        orchestrator = Orchestrator(tmp_workspace)
        sites = self._sites(8)
        plan = _prepared_plan(sites, parallel_sites=2)

        with patch.object(
            Orchestrator,
            "_execute_prepared_target",
            execute_target,
        ):
            results, _, interrupted = orchestrator._run_prepared_targets(
                plan,
                list(plan.targets),
                TIMESTAMP,
                {},
                PlanExecutionMode.APPLY,
                progress=_ProgressOwner(None),
            )

        assert interrupted is True
        assert len(started) < len(sites), (
            f"every site deployed despite the interrupt: {started}"
        )
        assert len(results) == len(sites)
        statuses = {
            operation.status
            for result in results
            for operation in result.operations
        }
        assert OperationStatus.UNKNOWN in statuses
        assert OperationStatus.CANCELLED in statuses
