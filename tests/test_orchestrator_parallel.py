"""Tests for concurrent site execution.

Manifests opt into concurrent site execution with `parallel:`, which routes
deployment through `_deploy_parallel` and the parallel branch of
`_collect_subscription_outputs`. These tests exercise the fan-out itself:
worker-count clamping, per-site failure isolation, result accumulation under
contention, and subscription-output collection.

`_deploy_site` is the injection seam. It is replaced with an in-process fake, so
nothing here spawns a process. Note the standing repository trap: never mock a
process handle without also patching the kernel calls in its cleanup path.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from siteops.models import DeploymentStep, Manifest, ParallelConfig, Site
from siteops.orchestrator import Orchestrator

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


class TestDeployParallel:
    """Fan-out, clamping, and failure isolation in `_deploy_parallel`."""

    def test_every_site_produces_exactly_one_result(self, tmp_workspace):
        manifest = _make_manifest()
        sites = _make_sites(5)
        orchestrator = Orchestrator(tmp_workspace)

        with patch.object(
            orchestrator,
            "_deploy_site",
            side_effect=lambda m, s, *a, **k: _ok_result(s, m),
        ):
            results = orchestrator._deploy_parallel(
                manifest, sites, TIMESTAMP, ParallelConfig(sites=3)
            )

        assert len(results) == len(sites)
        assert {r["site"] for r in results} == {s.name for s in sites}
        assert all(r["status"] == "success" for r in results)

    @pytest.mark.parametrize(
        ("site_count", "parallel_sites", "expected_workers"),
        [
            (5, 3, 3),  # capped by config
            (2, 5, 2),  # capped by site count
            (4, 0, 4),  # unlimited means one worker per site
        ],
    )
    def test_worker_count_is_clamped(
        self, tmp_workspace, site_count, parallel_sites, expected_workers
    ):
        """Workers are min(sites, max_workers); unlimited caps at the site count.

        An unclamped pool would open a thread, and an Arc proxy slot, per site
        on a large fleet.
        """
        manifest = _make_manifest()
        sites = _make_sites(site_count)
        orchestrator = Orchestrator(tmp_workspace)
        observed: dict[str, int] = {}

        from concurrent.futures import ThreadPoolExecutor as _RealPool

        def _recording_pool(max_workers=None, **kwargs):
            observed["max_workers"] = max_workers
            return _RealPool(max_workers=max_workers, **kwargs)

        with (
            patch.object(
                orchestrator,
                "_deploy_site",
                side_effect=lambda m, s, *a, **k: _ok_result(s, m),
            ),
            patch("siteops.orchestrator.ThreadPoolExecutor", side_effect=_recording_pool),
        ):
            orchestrator._deploy_parallel(
                manifest, sites, TIMESTAMP, ParallelConfig(sites=parallel_sites)
            )

        assert observed["max_workers"] == expected_workers

    def test_one_failing_site_does_not_stop_the_others(self, tmp_workspace):
        """Failure isolation: one site raising must not lose the other results.

        This is the blast-radius guarantee for a fleet deployment.
        """
        manifest = _make_manifest()
        sites = _make_sites(4)
        orchestrator = Orchestrator(tmp_workspace)

        def _fake(m, s, *args, **kwargs):
            if s.name == "site-2":
                raise RuntimeError("boom")
            return _ok_result(s, m)

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            results = orchestrator._deploy_parallel(
                manifest, sites, TIMESTAMP, ParallelConfig(sites=4)
            )

        assert len(results) == 4
        by_site = {r["site"]: r for r in results}
        assert [by_site[n]["status"] for n in ("site-0", "site-1", "site-3")] == [
            "success",
            "success",
            "success",
        ]

        failed = by_site["site-2"]
        assert failed["status"] == "failed"
        assert "boom" in failed["error"]
        assert failed["steps_completed"] == 0
        assert failed["steps_total"] == len(manifest.steps)
        assert failed["steps"] == []

    def test_results_are_not_lost_under_contention(self, tmp_workspace):
        """All results survive concurrent accumulation.

        Every worker blocks on a barrier so the appends genuinely overlap rather
        than serializing by luck of scheduling.
        """
        manifest = _make_manifest()
        site_count = 12
        sites = _make_sites(site_count)
        orchestrator = Orchestrator(tmp_workspace)
        barrier = threading.Barrier(site_count, timeout=30)

        def _fake(m, s, *args, **kwargs):
            barrier.wait()
            return _ok_result(s, m)

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            results = orchestrator._deploy_parallel(
                manifest, sites, TIMESTAMP, ParallelConfig(sites=site_count)
            )

        assert len(results) == site_count
        assert {r["site"] for r in results} == {s.name for s in sites}

    def test_parallel_mode_flag_is_passed_to_site_deploy(self, tmp_workspace):
        """Sites deployed concurrently must run in parallel mode.

        Parallel mode is what drives per-site output isolation, so a regression
        here would interleave site output rather than fail loudly.
        """
        manifest = _make_manifest()
        sites = _make_sites(3)
        orchestrator = Orchestrator(tmp_workspace)
        seen: list[bool] = []

        def _fake(m, s, timestamp, parallel_mode=False, subscription_outputs=None):
            seen.append(parallel_mode)
            return _ok_result(s, m)

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            orchestrator._deploy_parallel(
                manifest, sites, TIMESTAMP, ParallelConfig(sites=3)
            )

        assert seen == [True, True, True]


class TestSubscriptionPhaseFanOut:
    """Phase 1 subscription-scoped execution and output collection."""

    def _sub_site(self, name: str, subscription: str) -> Site:
        return Site(
            name=name,
            subscription=subscription,
            resource_group=f"rg-{name}",
            location="eastus",
            labels={},
        )

    def test_outputs_are_collected_per_subscription_in_parallel(self, tmp_workspace):
        manifest = _make_manifest()
        orchestrator = Orchestrator(tmp_workspace)
        subscription_sites = {
            "sub-a": self._sub_site("global-a", "sub-a"),
            "sub-b": self._sub_site("global-b", "sub-b"),
        }

        def _fake(m, s, timestamp, parallel_mode=False, subscription_outputs=None):
            return {
                **_ok_result(s, m),
                "steps": [
                    {
                        "step": "edge-site",
                        "status": "success",
                        "outputs": {"siteId": f"id-{s.subscription}"},
                    }
                ],
            }

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            outputs, results = orchestrator._collect_subscription_outputs(
                manifest, subscription_sites, TIMESTAMP, ParallelConfig(sites=2)
            )

        assert len(results) == 2
        assert set(outputs) == {"sub-a", "sub-b"}
        assert outputs["sub-a"]["edge-site"]["siteId"] == "id-sub-a"
        assert outputs["sub-b"]["edge-site"]["siteId"] == "id-sub-b"

    def test_failure_in_one_subscription_is_isolated(self, tmp_workspace):
        manifest = _make_manifest()
        orchestrator = Orchestrator(tmp_workspace)
        subscription_sites = {
            "sub-a": self._sub_site("global-a", "sub-a"),
            "sub-b": self._sub_site("global-b", "sub-b"),
        }

        def _fake(m, s, timestamp, parallel_mode=False, subscription_outputs=None):
            if s.subscription == "sub-b":
                raise RuntimeError("subscription boom")
            return _ok_result(s, m)

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            outputs, results = orchestrator._collect_subscription_outputs(
                manifest, subscription_sites, TIMESTAMP, ParallelConfig(sites=2)
            )

        by_site = {r["site"]: r for r in results}
        assert by_site["global-a"]["status"] == "success"
        assert by_site["global-b"]["status"] == "failed"
        assert "subscription boom" in by_site["global-b"]["error"]

    def test_extraction_failure_records_one_result_per_site(self, tmp_workspace):
        """A site is recorded exactly once even if output extraction fails.

        Output extraction runs inside the same guarded block as the deploy, so
        appending the result before extracting would report one site twice, as
        both a success and a failure, and inflate the summary counts.
        """
        manifest = _make_manifest()
        orchestrator = Orchestrator(tmp_workspace)
        subscription_sites = {
            "sub-a": self._sub_site("global-a", "sub-a"),
            "sub-b": self._sub_site("global-b", "sub-b"),
        }

        def _fake(m, s, timestamp, parallel_mode=False, subscription_outputs=None):
            # A step result missing the "step" key breaks extraction.
            return {
                **_ok_result(s, m),
                "steps": [{"status": "success", "outputs": {"siteId": "x"}}],
            }

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            _, results = orchestrator._collect_subscription_outputs(
                manifest, subscription_sites, TIMESTAMP, ParallelConfig(sites=2)
            )

        assert len(results) == len(subscription_sites)
        assert sorted(r["site"] for r in results) == ["global-a", "global-b"]
        assert all(r["status"] == "failed" for r in results)

    def test_no_subscription_sites_is_a_noop(self, tmp_workspace):
        manifest = _make_manifest()
        orchestrator = Orchestrator(tmp_workspace)

        with patch.object(orchestrator, "_deploy_site") as mock_deploy:
            outputs, results = orchestrator._collect_subscription_outputs(
                manifest, {}, TIMESTAMP, ParallelConfig(sites=2)
            )

        assert outputs == {}
        assert results == []
        mock_deploy.assert_not_called()

    def test_single_subscription_runs_sequentially(self, tmp_workspace):
        """One subscription-level site takes the sequential branch even when the
        manifest asks for parallelism, so it is never deployed in parallel mode.
        """
        manifest = _make_manifest()
        orchestrator = Orchestrator(tmp_workspace)
        subscription_sites = {"sub-a": self._sub_site("global-a", "sub-a")}
        seen: list[bool] = []

        def _fake(m, s, timestamp, parallel_mode=False, subscription_outputs=None):
            seen.append(parallel_mode)
            return _ok_result(s, m)

        with patch.object(orchestrator, "_deploy_site", side_effect=_fake):
            orchestrator._collect_subscription_outputs(
                manifest, subscription_sites, TIMESTAMP, ParallelConfig(sites=4)
            )

        assert seen == [False]


class TestSubscriptionFailureBlastRadius:
    """Which resource-group sites proceed after a subscription-scoped failure.

    A failed subscription phase must stop only the sites that consume its
    outputs. Blocking more than that halts a fleet unnecessarily; blocking less
    sends sites into a deploy whose inputs never resolved.
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
        manifest_path = tmp_workspace / "manifests" / "two-phase.yaml"
        manifest_path.write_text("name: two-phase\n", encoding="utf-8")

        orchestrator = Orchestrator(tmp_workspace)
        sub_site = next(s for s in sites if s.is_subscription_level)
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

        def _record(m, phase_sites, *args, **kwargs):
            deployed.append([s.name for s in phase_sites])
            return [_ok_result(s, m) for s in phase_sites]

        with (
            patch.object(
                orchestrator,
                "_collect_subscription_outputs",
                return_value=({}, failed_phase_one),
            ),
            patch.object(
                orchestrator, "_site_depends_on_subscription_outputs", return_value=depends
            ),
            patch.object(orchestrator, "_deploy_parallel", side_effect=_record),
            patch.object(orchestrator, "_deploy_sequential", side_effect=_record),
        ):
            summary = orchestrator.deploy(manifest_path, manifest=manifest, sites=sites)

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
