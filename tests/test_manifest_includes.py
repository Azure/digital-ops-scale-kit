# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for the manifest `include:` step directive.

Covers the include resolution surface in `siteops/models.py`:
- happy paths (simple include, nested, mixed inline+include, deep chains)
- diagnostics (cycles, depth cap, traversal, name collisions, empty targets)
- semantics (when propagation, when conflicts, parameter merge)
- standalone-vs-fragment dual purpose contract
"""

from pathlib import Path

import pytest
import yaml

from siteops.models import (
    MAX_INCLUDE_DEPTH,
    DeploymentStep,
    IncludeError,
    KubectlStep,
    Manifest,
)


def _write_manifest(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(body, f, sort_keys=False)
    return path


def _step(name: str, template: str = "templates/x.bicep", **extra) -> dict:
    return {"name": name, "template": template, **extra}


def _kubectl_step(name: str, **extra) -> dict:
    return {
        "name": name,
        "type": "kubectl",
        "operation": "apply",
        "arc": {"name": "c", "resourceGroup": "rg"},
        "files": ["x.yaml"],
        **extra,
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace root with a manifests/ subdir."""
    (tmp_path / "manifests").mkdir()
    return tmp_path


class TestSimpleInclude:
    """Basic include happy paths."""

    def test_one_include_two_steps_each_preserves_order(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "frag.yaml",
            {
                "name": "frag",
                "steps": [_step("frag-a"), _step("frag-b")],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "parent.yaml",
            {
                "name": "parent",
                "steps": [
                    _step("parent-a"),
                    {"include": "frag.yaml"},
                    _step("parent-b"),
                ],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)

        assert [s.name for s in m.steps] == ["parent-a", "frag-a", "frag-b", "parent-b"]
        assert all(isinstance(s, DeploymentStep) for s in m.steps)

    def test_include_only_parent(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "frag.yaml",
            {"name": "frag", "steps": [_step("only-step")]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "parent.yaml",
            {"name": "parent", "steps": [{"include": "frag.yaml"}]},
        )

        m = Manifest.from_file(parent, workspace_root=workspace)

        assert [s.name for s in m.steps] == ["only-step"]

    def test_kubectl_step_in_fragment(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "frag.yaml",
            {"name": "frag", "steps": [_kubectl_step("apply-yaml")]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "parent.yaml",
            {"name": "parent", "steps": [{"include": "frag.yaml"}]},
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert isinstance(m.steps[0], KubectlStep)


class TestNestedIncludes:
    """Recursive include behavior."""

    def test_nested_include_ordering(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "c.yaml",
            {"name": "c", "steps": [_step("c-1")]},
        )
        _write_manifest(
            workspace / "manifests" / "b.yaml",
            {
                "name": "b",
                "steps": [
                    _step("b-1"),
                    {"include": "c.yaml"},
                    _step("b-2"),
                ],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "a.yaml",
            {
                "name": "a",
                "steps": [
                    _step("a-1"),
                    {"include": "b.yaml"},
                    _step("a-2"),
                ],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert [s.name for s in m.steps] == ["a-1", "b-1", "c-1", "b-2", "a-2"]

    def test_nested_include_path_relative_to_parent(self, workspace: Path):
        # B in dir2/, C also in dir2/. B's include of C uses a path relative to dir2.
        (workspace / "dir1").mkdir()
        (workspace / "dir2").mkdir()
        _write_manifest(
            workspace / "dir2" / "c.yaml",
            {"name": "c", "steps": [_step("c-1")]},
        )
        _write_manifest(
            workspace / "dir2" / "b.yaml",
            {"name": "b", "steps": [{"include": "c.yaml"}]},
        )
        parent = _write_manifest(
            workspace / "dir1" / "a.yaml",
            {"name": "a", "steps": [{"include": "../dir2/b.yaml"}]},
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert [s.name for s in m.steps] == ["c-1"]

    def test_shared_subfragment_not_a_cycle(self, workspace: Path):
        # A includes B and C; B includes D as "d-from-b"; C includes D as "d-from-c".
        # D contributes one step but its name varies via a copy file so no collision.
        # This case proves: visited globally would flag a false cycle; recursion-stack does not.
        _write_manifest(
            workspace / "manifests" / "d-from-b.yaml",
            {"name": "d", "steps": [_step("d-via-b")]},
        )
        _write_manifest(
            workspace / "manifests" / "d-from-c.yaml",
            {"name": "d", "steps": [_step("d-via-c")]},
        )
        _write_manifest(
            workspace / "manifests" / "b.yaml",
            {"name": "b", "steps": [{"include": "d-from-b.yaml"}]},
        )
        _write_manifest(
            workspace / "manifests" / "c.yaml",
            {"name": "c", "steps": [{"include": "d-from-c.yaml"}]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "a.yaml",
            {
                "name": "a",
                "steps": [{"include": "b.yaml"}, {"include": "c.yaml"}],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert [s.name for s in m.steps] == ["d-via-b", "d-via-c"]

    def test_truly_shared_fragment_distinct_steps(self, workspace: Path):
        # A includes B and C; both B and C include the SAME D file with one step.
        # Should fail with a step-name collision (NOT a cycle).
        _write_manifest(
            workspace / "manifests" / "d.yaml",
            {"name": "d", "steps": [_step("d-only")]},
        )
        _write_manifest(
            workspace / "manifests" / "b.yaml",
            {"name": "b", "steps": [{"include": "d.yaml"}]},
        )
        _write_manifest(
            workspace / "manifests" / "c.yaml",
            {"name": "c", "steps": [{"include": "d.yaml"}]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "a.yaml",
            {
                "name": "a",
                "steps": [{"include": "b.yaml"}, {"include": "c.yaml"}],
            },
        )

        with pytest.raises(ValueError, match="Duplicate step name 'd-only'"):
            Manifest.from_file(parent, workspace_root=workspace)


class TestCycleDetection:
    def test_direct_cycle(self, workspace: Path):
        a = workspace / "manifests" / "a.yaml"
        b = workspace / "manifests" / "b.yaml"
        _write_manifest(a, {"name": "a", "steps": [{"include": "b.yaml"}]})
        _write_manifest(b, {"name": "b", "steps": [{"include": "a.yaml"}]})

        with pytest.raises(IncludeError, match="cycle detected"):
            Manifest.from_file(a, workspace_root=workspace)

    def test_self_cycle(self, workspace: Path):
        a = _write_manifest(
            workspace / "manifests" / "a.yaml",
            {"name": "a", "steps": [{"include": "a.yaml"}]},
        )
        with pytest.raises(IncludeError, match="cycle"):
            Manifest.from_file(a, workspace_root=workspace)

    def test_indirect_cycle_three_hops(self, workspace: Path):
        a = workspace / "manifests" / "a.yaml"
        b = workspace / "manifests" / "b.yaml"
        c = workspace / "manifests" / "c.yaml"
        _write_manifest(a, {"name": "a", "steps": [{"include": "b.yaml"}]})
        _write_manifest(b, {"name": "b", "steps": [{"include": "c.yaml"}]})
        _write_manifest(c, {"name": "c", "steps": [{"include": "a.yaml"}]})

        with pytest.raises(IncludeError, match="cycle"):
            Manifest.from_file(a, workspace_root=workspace)


class TestDepthCap:
    def test_depth_cap_exceeded(self, workspace: Path):
        # Build a chain of MAX_INCLUDE_DEPTH + 2 manifests to exceed the cap.
        chain_len = MAX_INCLUDE_DEPTH + 2
        for i in range(chain_len):
            steps = [_step(f"leaf-{i}")] if i == chain_len - 1 else [{"include": f"m{i+1}.yaml"}]
            _write_manifest(
                workspace / "manifests" / f"m{i}.yaml",
                {"name": f"m{i}", "steps": steps},
            )

        with pytest.raises(IncludeError, match="Include depth exceeded"):
            Manifest.from_file(workspace / "manifests" / "m0.yaml", workspace_root=workspace)


class TestStepNameCollision:
    def test_collision_across_two_includes(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f1.yaml",
            {"name": "f1", "steps": [_step("dup")]},
        )
        _write_manifest(
            workspace / "manifests" / "f2.yaml",
            {"name": "f2", "steps": [_step("dup")]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "steps": [{"include": "f1.yaml"}, {"include": "f2.yaml"}],
            },
        )

        with pytest.raises(ValueError, match="Duplicate step name 'dup'"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_collision_parent_vs_fragment(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {"name": "f", "steps": [_step("shared")]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [_step("shared"), {"include": "f.yaml"}]},
        )

        with pytest.raises(ValueError, match="Duplicate step name 'shared'"):
            Manifest.from_file(parent, workspace_root=workspace)


class TestPathTraversal:
    def test_traversal_outside_workspace_rejected(self, tmp_path: Path):
        # Workspace is tmp_path/ws; fragment exists at tmp_path/outside.yaml.
        ws = tmp_path / "ws"
        (ws / "manifests").mkdir(parents=True)
        outside = _write_manifest(
            tmp_path / "outside.yaml",
            {"name": "outside", "steps": [_step("x")]},
        )
        parent = _write_manifest(
            ws / "manifests" / "p.yaml",
            {"name": "p", "steps": [{"include": "../../outside.yaml"}]},
        )
        # Sanity check the file actually exists at the traversed location.
        assert outside.exists()

        with pytest.raises(IncludeError, match="outside the workspace root"):
            Manifest.from_file(parent, workspace_root=ws)

    def test_missing_include_target(self, workspace: Path):
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [{"include": "nonexistent.yaml"}]},
        )
        with pytest.raises(IncludeError, match="does not exist"):
            Manifest.from_file(parent, workspace_root=workspace)


class TestParameterMerge:
    def test_parent_wins_on_duplicate_path(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {
                "name": "f",
                "parameters": ["params/shared.yaml", "params/frag.yaml"],
                "steps": [_step("f-1")],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "parameters": ["params/shared.yaml", "params/parent.yaml"],
                "steps": [{"include": "f.yaml"}],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        # Parent params first; fragment-only params appended; duplicate dropped.
        assert m.parameters == ["params/shared.yaml", "params/parent.yaml", "params/frag.yaml"]

    def test_path_normalization_dedupes(self, workspace: Path):
        # 'params/x.yaml' and './params/x.yaml' should be considered the same.
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {
                "name": "f",
                "parameters": ["./params/x.yaml"],
                "steps": [_step("f-1")],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "parameters": ["params/x.yaml"],
                "steps": [{"include": "f.yaml"}],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert m.parameters == ["params/x.yaml"]


class TestWhenPropagation:
    def test_when_inherited_by_spliced_steps(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {"name": "f", "steps": [_step("f-1"), _step("f-2")]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "steps": [
                    {
                        "include": "f.yaml",
                        "when": "{{ site.properties.gate == 'on' }}",
                    }
                ],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert all(s.when == "{{ site.properties.gate == 'on' }}" for s in m.steps)

    def test_when_inheritance_through_nested_include(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "c.yaml",
            {"name": "c", "steps": [_step("c-1")]},
        )
        _write_manifest(
            workspace / "manifests" / "b.yaml",
            {"name": "b", "steps": [{"include": "c.yaml"}]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "a.yaml",
            {
                "name": "a",
                "steps": [
                    {
                        "include": "b.yaml",
                        "when": "{{ site.properties.deep == 'true' }}",
                    }
                ],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert m.steps[0].when == "{{ site.properties.deep == 'true' }}"

    def test_when_conflict_raises(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {
                "name": "f",
                "steps": [_step("f-1", when="{{ site.properties.inner == 'on' }}")],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "steps": [
                    {
                        "include": "f.yaml",
                        "when": "{{ site.properties.outer == 'on' }}",
                    }
                ],
            },
        )

        with pytest.raises(IncludeError, match="already has a `when:`"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_when_with_fragment_manifest_params_raises(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {
                "name": "f",
                "parameters": ["params/frag.yaml"],
                "steps": [_step("f-1")],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "steps": [
                    {
                        "include": "f.yaml",
                        "when": "{{ site.properties.gate == 'on' }}",
                    }
                ],
            },
        )

        with pytest.raises(IncludeError, match="manifest-level `parameters:`"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_when_with_transitive_fragment_params_raises(self, workspace: Path):
        # B has no parameters of its own but includes C, which does. The guard
        # must check the post-recursion parameter set, not just B's spec.
        _write_manifest(
            workspace / "manifests" / "c.yaml",
            {
                "name": "c",
                "parameters": ["params/c.yaml"],
                "steps": [_step("c-1")],
            },
        )
        _write_manifest(
            workspace / "manifests" / "b.yaml",
            {"name": "b", "steps": [{"include": "c.yaml"}]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "a.yaml",
            {
                "name": "a",
                "steps": [
                    {
                        "include": "b.yaml",
                        "when": "{{ site.properties.gate == 'on' }}",
                    }
                ],
            },
        )

        with pytest.raises(IncludeError, match="manifest-level `parameters:`"):
            Manifest.from_file(parent, workspace_root=workspace)


class TestEmptyAndInvalidIncludes:
    def test_empty_include_target(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {"name": "f", "steps": []},
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [{"include": "f.yaml"}]},
        )

        with pytest.raises(IncludeError, match="contributed zero steps"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_include_target_is_not_a_manifest(self, workspace: Path):
        _write_manifest(
            workspace / "sites" / "fake-site.yaml",
            {"apiVersion": "siteops/v1", "kind": "Site", "name": "site-x"},
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [{"include": "../sites/fake-site.yaml"}]},
        )

        with pytest.raises(IncludeError, match="could not be loaded as a Manifest"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_include_extra_keys_rejected(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {"name": "f", "steps": [_step("f-1")]},
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "steps": [
                    {
                        "include": "f.yaml",
                        "name": "stray",
                        "template": "stray.bicep",
                    }
                ],
            },
        )

        with pytest.raises(IncludeError, match="unexpected keys alongside"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_include_with_empty_string_rejected(self, workspace: Path):
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [{"include": ""}]},
        )

        with pytest.raises(IncludeError, match="non-empty string path"):
            Manifest.from_file(parent, workspace_root=workspace)

    def test_include_with_non_string_rejected(self, workspace: Path):
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [{"include": 42}]},
        )

        with pytest.raises(IncludeError, match="non-empty string path"):
            Manifest.from_file(parent, workspace_root=workspace)


class TestStandaloneFragmentDualPurpose:
    """A manifest used as a fragment ignores its top-level standalone fields."""

    def test_fragment_site_selector_ignored(self, workspace: Path):
        _write_manifest(
            workspace / "manifests" / "f.yaml",
            {
                "name": "f",
                "siteSelector": "ignored=true",
                "sites": ["ignored-site"],
                "parallel": 5,
                "steps": [_step("f-1")],
            },
        )
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {
                "name": "p",
                "siteSelector": "real=true",
                "steps": [{"include": "f.yaml"}],
            },
        )

        m = Manifest.from_file(parent, workspace_root=workspace)
        assert m.site_selector == "real=true"
        assert m.sites == []
        assert m.parallel.sites == 1  # parent default, not 5


class TestBackwardCompatibility:
    """Manifests with no includes still load with workspace_root supplied."""

    def test_no_includes_still_loads(self, workspace: Path):
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [_step("only")]},
        )
        m = Manifest.from_file(parent, workspace_root=workspace)
        assert [s.name for s in m.steps] == ["only"]

    def test_workspace_root_is_required(self, workspace: Path):
        """workspace_root is keyword-only and required (no silent default)."""
        parent = _write_manifest(
            workspace / "manifests" / "p.yaml",
            {"name": "p", "steps": [_step("only")]},
        )
        with pytest.raises(TypeError, match="workspace_root"):
            Manifest.from_file(parent)  # type: ignore[call-arg]
