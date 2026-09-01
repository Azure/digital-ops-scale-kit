"""The resource catalog cannot change what a base AIO install deploys.

The install, upgrade, Secret Sync, and host manifests are the heavily exercised
paths. The catalog is additive: it ships its own entry point, its own partials,
and its own templates, and a site opts in by resource area. Nothing here is
about the catalog working. It is about the catalog staying out of the way.

These properties make that true, and each is checked below.

- A base-path manifest never deploys a catalog template, so adding a family
  cannot add a step to an install.
- A declaration key is never a parameter name a base-path template accepts.
  Parameters are filtered per template by name, so even if a declaration
  reached an install step's merge, the step would not receive it. Disjoint
  names are what guarantee the drop rather than relying on it.
- The base site omits `resourceSets`, so a site that has never heard of the
  catalog deploys exactly what it did before.
"""

import re
from pathlib import Path

import yaml

from siteops.models import DeploymentStep, Manifest, ParameterSource
from tests.workspace import catalog_harness as harness
from tests.workspace.catalog_harness import CATALOG_FAMILIES

# `param <name> <type>`, matching the declaration form used across the workspace.
_PARAM_DECL = re.compile(r"^\s*param\s+(\w+)\s+", re.MULTILINE)

# The catalog's own entry point. Every other manifest is a base path.
_CATALOG_ENTRY_POINT = "aio-resources.yaml"


def _catalog_template_dirs(workspace: Path) -> set[Path]:
    """The template directory each registered family composes from."""
    return {harness.family_dir(workspace, spec) for spec in CATALOG_FAMILIES}


def _declaration_keys(workspace: Path) -> set[str]:
    """Every top-level key a family's declarations use.

    Taken from the specs and from the committed sets together. The specs cover a
    kind that ships before a set declares it, and the sets cover a key an
    author added ahead of the spec, so a name reaching a base-path template is
    caught either way.
    """
    keys: set[str] = {key for spec in CATALOG_FAMILIES for key in spec.kind_keys}
    for spec in CATALOG_FAMILIES:
        for directory in harness.parameter_dirs(workspace, spec):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.yaml"):
                data = harness.load_yaml(path) or {}
                if isinstance(data, dict):
                    keys.update(key for key in data if key != "_siteops")
    return keys


def _declaration_dirs(workspace: Path) -> set[Path]:
    """Directories holding a family's declaration sets."""
    return {
        directory.resolve()
        for spec in CATALOG_FAMILIES
        for directory in harness.parameter_dirs(workspace, spec)
        if directory.is_dir()
    }


def _catalog_sibling_parameter_files(workspace: Path) -> list[Path]:
    """Manifest-level files the catalog loads alongside a declaration.

    A path carrying a variable lets the site pick one of a directory's files,
    so every candidate in that directory counts. The declaration directories
    themselves are excluded, since those are what the siblings are compared
    against.
    """
    manifest = Manifest.from_file(
        workspace / "manifests" / _CATALOG_ENTRY_POINT, workspace_root=workspace
    )
    declaration_dirs = _declaration_dirs(workspace)
    files: list[Path] = []
    for source in manifest.parameters:
        raw = source.path if isinstance(source, ParameterSource) else source
        directory = (workspace / raw).parent.resolve()
        if directory in declaration_dirs:
            continue
        if "{{" in raw:
            files.extend(sorted(directory.glob("*.yaml")))
        elif (workspace / raw).is_file():
            files.append(workspace / raw)
    return files


def _base_path_manifests(workspace: Path) -> list[Path]:
    """Entry-point manifests that are not the catalog.

    Scoped to `manifests/*.yaml` with the partials excluded, which is the set an
    operator deploys directly. Discovered rather than listed, so a new entry
    point is covered the moment it lands and would have to be excluded here
    deliberately rather than by omission.

    Family partials and the samples that compose them are catalog surfaces and
    are meant to deploy catalog templates, so they are not base paths. A sample
    is opt-in by nature.
    """
    return sorted(
        p
        for p in (workspace / "manifests").glob("*.yaml")
        if not p.name.startswith("_") and p.name != _CATALOG_ENTRY_POINT
    )


class TestCatalogAddsNoStepToABasePath:
    """Adding a family cannot add work to an install."""

    def test_no_base_path_manifest_deploys_a_catalog_template(self, workspace):
        """Includes are flattened before this runs, so a nested include counts."""
        catalog_dirs = _catalog_template_dirs(workspace)
        assert catalog_dirs, "No catalog family found, so this check covers nothing."

        manifests = _base_path_manifests(workspace)
        assert len(manifests) >= 5, (
            f"Only {len(manifests)} base-path entry points found, which is fewer "
            f"than the install, upgrade, Secret Sync, and two host manifests "
            f"that ship. Discovery is matching the wrong thing, so this check "
            f"is covering less than it appears to."
        )

        offenders: list[str] = []
        for manifest_path in manifests:
            manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            for step in manifest.steps:
                if not isinstance(step, DeploymentStep) or not step.template:
                    continue
                resolved = (workspace / step.template).resolve()
                if any(resolved.is_relative_to(d.resolve()) for d in catalog_dirs):
                    offenders.append(
                        f"{manifest_path.name}: step '{step.name}' deploys "
                        f"'{step.template}', which belongs to a catalog family. "
                        f"A family reaches an operator through "
                        f"manifests/{_CATALOG_ENTRY_POINT}, where a site opts in "
                        f"per family."
                    )
        assert not offenders, "\n".join(offenders)

    def test_the_catalog_entry_point_exists_and_is_separate(self, workspace):
        """The check above is only meaningful while the entry point is its own file."""
        entry = workspace / "manifests" / _CATALOG_ENTRY_POINT
        assert entry.is_file(), (
            f"manifests/{_CATALOG_ENTRY_POINT} is missing, so every manifest "
            f"counts as a base path and the isolation check above is vacuous."
        )


class TestDeclarationKeysCannotReachABasePathTemplate:
    """A declaration key is not a name any install template accepts."""

    def test_declaration_keys_are_disjoint_from_base_path_template_parameters(
        self, workspace
    ):
        """Parameters are filtered per template by name.

        A base-path template that happened to declare `dataflows` would receive
        a catalog declaration as its input the moment the two ever shared a
        manifest. Keeping the names disjoint means that cannot happen by
        accident, rather than depending on the two never being composed.
        """
        keys = _declaration_keys(workspace)
        assert keys, "No declaration keys found, so this check covers nothing."

        catalog_dirs = {d.resolve() for d in _catalog_template_dirs(workspace)}
        collisions: list[str] = []
        for template in sorted((workspace / "templates").rglob("*.bicep")):
            if any(template.resolve().is_relative_to(d) for d in catalog_dirs):
                continue
            declared = set(_PARAM_DECL.findall(template.read_text(encoding="utf-8")))
            for name in sorted(declared & keys):
                collisions.append(
                    f"{template.relative_to(workspace)} declares `param {name}`, "
                    f"which is also a catalog declaration key. Rename one of "
                    f"them, since parameters are matched to templates by name."
                )
        assert not collisions, "\n".join(collisions)

    def test_declaration_keys_are_disjoint_from_committed_site_parameters(
        self, workspace
    ):
        """A site parameter outranks a manifest-level declaration.

        `base-site.yaml` already carries `brokerConfig` and
        `defaultDataflowInstanceCount`, which are dataflow-adjacent names. A
        site parameter sharing a declaration key would silently replace the
        whole declared array for that site, since lists replace rather than
        merge.
        """
        keys = _declaration_keys(workspace)
        collisions: list[str] = []
        for site_file in sorted((workspace / "sites").rglob("*.yaml")):
            data = yaml.safe_load(site_file.read_text(encoding="utf-8")) or {}
            parameters = data.get("parameters") or {}
            if not isinstance(parameters, dict):
                continue
            for name in sorted(set(parameters) & keys):
                collisions.append(
                    f"{site_file.relative_to(workspace)} sets "
                    f"`parameters.{name}`, which is a catalog declaration key. "
                    f"A site parameter outranks the declaration and would "
                    f"replace it wholesale."
                )
        assert not collisions, "\n".join(collisions)

    def test_declaration_keys_are_disjoint_from_the_catalogs_other_parameter_files(
        self, workspace
    ):
        """A declaration shares its tier with the common and release files.

        All of them attach at manifest level on the catalog entry point and
        merge in list order, so a shared key means one silently replaces the
        other and which one wins depends on the order the paths happen to be
        listed in. `common.yaml` already supplies `aioInstanceName` and
        `customLocationName`, which the family templates also accept.
        """
        keys = _declaration_keys(workspace)
        siblings = _catalog_sibling_parameter_files(workspace)
        assert siblings, (
            "The catalog entry point loads no manifest-level file besides the "
            "declaration, so this check covers nothing. Either the entry point "
            "changed or the declaration directories are being excluded wrongly."
        )

        collisions: list[str] = []
        for path in siblings:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            for name in sorted(set(data) & keys):
                collisions.append(
                    f"{path.relative_to(workspace)} sets `{name}`, which is "
                    f"also a catalog declaration key. Both attach at manifest "
                    f"level on manifests/{_CATALOG_ENTRY_POINT}, so one "
                    f"silently replaces the other. Rename one of them."
                )
        assert not collisions, "\n".join(collisions)
