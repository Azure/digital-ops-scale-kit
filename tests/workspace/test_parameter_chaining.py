"""Tests that parameter chaining files reference valid step outputs."""

import re
from pathlib import Path

import yaml


# Pattern to extract step references: {{ steps.<step_name>.outputs.<path> }}
STEP_OUTPUT_PATTERN = re.compile(r"\{\{\s*steps\.([^.]+)\.outputs\.(\S+?)\s*\}\}")


class TestParameterChaining:
    """Chaining parameter files should reference steps and outputs that exist."""

    def _get_chaining_refs(self, param_file: Path) -> list[tuple[str, str, str]]:
        """Extract (step_name, output_path, raw_template) from a parameter file."""
        with open(param_file, "r", encoding="utf-8") as f:
            content = f.read()

        refs = []
        for match in STEP_OUTPUT_PATTERN.finditer(content):
            step_name = match.group(1)
            output_path = match.group(2)
            refs.append((step_name, output_path, match.group(0)))
        return refs

    def _get_manifest_step_names(self, manifest_path: Path, workspace_root: Path | None = None) -> set[str]:
        """Get all step names from a manifest (post-include flatten)."""
        from siteops.models import Manifest
        manifest = Manifest.from_file(manifest_path, workspace_root=workspace_root)
        return {s.name for s in manifest.steps}

    def test_secretsync_inputs_refs_valid_steps(self, workspace):
        """parameters/inputs/secretsync.yaml should only reference steps that exist in manifests."""
        chaining_file = workspace / "parameters" / "inputs" / "secretsync.yaml"
        refs = self._get_chaining_refs(chaining_file)
        assert len(refs) > 0, "No step output references found in inputs/secretsync.yaml"

        # Get step names from both manifests that use this inputs file
        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml", workspace_root=workspace)
        secretsync_steps = self._get_manifest_step_names(workspace / "manifests" / "secretsync.yaml", workspace_root=workspace)
        all_valid_steps = aio_steps | secretsync_steps

        for step_name, output_path, raw in refs:
            assert step_name in all_valid_steps, (
                f"inputs/secretsync.yaml references unknown step '{step_name}': {raw}"
            )

    def test_secretsync_inputs_refs_valid_outputs(self, workspace):
        """Every output referenced in inputs/secretsync.yaml should exist in resolve-aio.bicep."""
        chaining_file = workspace / "parameters" / "inputs" / "secretsync.yaml"
        refs = self._get_chaining_refs(chaining_file)

        # Parse output names from resolve-aio.bicep
        resolve_aio = workspace / "templates" / "aio" / "resolve-aio.bicep"
        bicep_content = resolve_aio.read_text(encoding="utf-8")
        output_names = set(re.findall(r"^output\s+(\w+)\s+", bicep_content, re.MULTILINE))
        assert len(output_names) > 0, "No outputs found in resolve-aio.bicep"

        for step_name, output_path, raw in refs:
            if step_name != "resolve-aio":
                continue
            # The top-level output name is the first segment of the path
            top_level_output = output_path.split(".")[0]
            assert top_level_output in output_names, (
                f"inputs/secretsync.yaml references unknown output "
                f"'{top_level_output}' from resolve-aio: {raw}\n"
                f"Available outputs: {sorted(output_names)}"
            )

    def test_aio_instance_inputs_refs_in_aio_install(self, workspace):
        """parameters/inputs/aio-instance.yaml should only reference steps that exist in aio-install.yaml."""
        chaining_file = workspace / "parameters" / "inputs" / "aio-instance.yaml"
        refs = self._get_chaining_refs(chaining_file)

        if not refs:
            return

        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml", workspace_root=workspace)

        for step_name, output_path, raw in refs:
            assert step_name in aio_steps, (
                f"inputs/aio-instance.yaml references unknown step '{step_name}': {raw}"
            )

    def test_aio_instance_outputs_refs_in_aio_install(self, workspace):
        """parameters/outputs/aio-instance.yaml should only reference steps that exist in aio-install.yaml."""
        chaining_file = workspace / "parameters" / "outputs" / "aio-instance.yaml"
        refs = self._get_chaining_refs(chaining_file)

        if not refs:
            return

        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml", workspace_root=workspace)

        for step_name, output_path, raw in refs:
            assert step_name in aio_steps, (
                f"outputs/aio-instance.yaml references unknown step '{step_name}': {raw}"
            )

    def test_opc_ua_solution_inputs_refs_valid_steps(self, workspace):
        """opc-ua-solution inputs.yaml only references steps in the standalone manifest."""
        chaining_file = workspace / "samples" / "opc-ua-solution" / "inputs.yaml"
        refs = self._get_chaining_refs(chaining_file)
        assert len(refs) > 0, "No step output references found in samples/opc-ua-solution/inputs.yaml"

        opc_ua_steps = self._get_manifest_step_names(workspace / "samples" / "opc-ua-solution" / "manifest.yaml", workspace_root=workspace)

        for step_name, output_path, raw in refs:
            assert step_name in opc_ua_steps, (
                f"samples/opc-ua-solution/inputs.yaml references unknown step '{step_name}': {raw}"
            )

    def test_opc_ua_solution_inputs_refs_valid_outputs(self, workspace):
        """Every output referenced in samples/opc-ua-solution/inputs.yaml exists in resolve-aio.bicep."""
        chaining_file = workspace / "samples" / "opc-ua-solution" / "inputs.yaml"
        refs = self._get_chaining_refs(chaining_file)

        resolve_aio = workspace / "templates" / "aio" / "resolve-aio.bicep"
        bicep_content = resolve_aio.read_text(encoding="utf-8")
        output_names = set(re.findall(r"^output\s+(\w+)\s+", bicep_content, re.MULTILINE))
        assert len(output_names) > 0, "No outputs found in resolve-aio.bicep"

        for step_name, output_path, raw in refs:
            if step_name != "resolve-aio":
                continue
            top_level_output = output_path.split(".")[0]
            assert top_level_output in output_names, (
                f"samples/opc-ua-solution/inputs.yaml references unknown output "
                f"'{top_level_output}' from resolve-aio: {raw}\n"
                f"Available outputs: {sorted(output_names)}"
            )


class TestConditionalStepCoverage:
    """Every when: condition should reference a property that exists in base-site.yaml."""

    def _get_conditions_from_manifest(self, manifest_path: Path, workspace: Path) -> list[tuple[str, str]]:
        """Extract (step_name, condition) pairs from a manifest."""
        from siteops.models import Manifest
        manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
        conditions = []
        for step in manifest.steps:
            if step.when:
                conditions.append((step.name, step.when))
        return conditions

    def _get_base_site_property_paths(self, workspace: Path) -> set[str]:
        """Get all dot-separated property paths defined in base-site.yaml."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        paths = set()
        properties = data.get("properties", {})

        def collect_paths(d: dict, prefix: str = ""):
            for k, v in d.items():
                full = f"{prefix}.{k}" if prefix else k
                paths.add(full)
                if isinstance(v, dict):
                    collect_paths(v, full)

        collect_paths(properties)
        return paths

    def test_all_when_conditions_reference_known_properties(self, workspace):
        """Every when: condition property path should exist in base-site.yaml."""
        known_paths = self._get_base_site_property_paths(workspace)
        prop_pattern = re.compile(r"site\.properties\.([\w.]+)")

        manifests_dir = workspace / "manifests"
        for manifest_file in sorted(manifests_dir.glob("*.yaml")):
            conditions = self._get_conditions_from_manifest(manifest_file, workspace)

            for step_name, condition in conditions:
                match = prop_pattern.search(condition)
                if not match:
                    continue

                prop_path = match.group(1)
                assert prop_path in known_paths, (
                    f"{manifest_file.name} step '{step_name}' references unknown property "
                    f"'site.properties.{prop_path}' in when condition.\n"
                    f"Known property paths: {sorted(known_paths)}"
                )


class TestUpdateInstanceDispatch:
    """Ensure callers of update-instance.bicep pass every param the router declares.

    Adding a new param to the shared UPDATE primitive without wiring it into
    every caller would silently omit the value at deploy time. All params
    have defaults in the caller signature via ARM, meaning the original
    property would be wiped on PUT without any test failure. This structural
    check is cheap insurance against that class of regression.
    """

    PARAM_DECL_RE = re.compile(
        r"^\s*param\s+(\w+)\s+(\w+|\w+\?)", re.MULTILINE
    )

    def _router_params(self, workspace: Path) -> set[str]:
        bicep = (
            workspace / "templates" / "aio" / "modules" / "update-instance.bicep"
        ).read_text(encoding="utf-8")
        return {m.group(1) for m in self.PARAM_DECL_RE.finditer(bicep)}

    def _caller_module_params(self, caller_path: Path) -> set[str]:
        """Parse the `params: { ... }` block of the first `../aio/modules/update-instance.bicep`
        module invocation in the caller. The containing module block may embed
        `${...}` interpolation in `name:` so the outer regex uses lazy `.*?` with
        DOTALL rather than a negated-brace class."""
        text = caller_path.read_text(encoding="utf-8")
        module_re = re.compile(
            r"module\s+\w+\s+'[^']*update-instance\.bicep'\s*=\s*\{"
            r".*?params:\s*\{(.*?)^\s*\}",
            re.DOTALL | re.MULTILINE,
        )
        m = module_re.search(text)
        assert m, f"{caller_path.name}: no update-instance.bicep module invocation found"
        body = m.group(1)
        return set(re.findall(r"^\s*(\w+)\s*:", body, re.MULTILINE))

    def test_enable_secretsync_passes_all_router_params(self, workspace):
        router = self._router_params(workspace)
        caller = self._caller_module_params(
            workspace / "templates" / "secretsync" / "enable-secretsync.bicep"
        )
        missing = router - caller
        assert missing == set(), (
            f"enable-secretsync.bicep does not forward these update-instance "
            f"router params: {sorted(missing)}. Every param on "
            f"templates/aio/modules/update-instance.bicep must be passed, or "
            f"the corresponding instance property will be wiped on PUT."
        )
        extra = caller - router
        assert extra == set(), (
            f"enable-secretsync.bicep passes params not declared by the "
            f"update-instance router: {sorted(extra)}. Remove them or add "
            f"them to templates/aio/modules/update-instance.bicep."
        )


class TestAioUpgradeChaining:
    """Structural integrity of the aio-upgrade.yaml chain.

    The upgrade manifest fans resolve-aio -> resolve-extensions ->
    update-extensions through per-consumer chaining files (one chaining
    YAML per consumer step, named after the manifest + consumer step).
    Each consumer step's required Bicep params must be satisfied by
    either its chaining file or the version YAML
    (parameters/aio-releases/<release>.yaml), and every chained
    `{{ steps.X.outputs.Y }}` reference must hit a real output. A break
    here would silently produce wrong PUTs at deploy time.

    Also asserts the install-side `aioExtensionName(clusterId)` deriver
    invariant: the upgrade flow MUST receive the connected cluster's full
    resource ID so it recomputes the same name install stamped.
    """

    PARAM_DECL_RE = re.compile(
        r"^\s*param\s+(\w+)\s+[^=\n]+?(=\s*[^\n]+)?$",
        re.MULTILINE,
    )
    OUTPUT_RE = re.compile(r"^\s*output\s+(\w+)\s+", re.MULTILINE)

    # consumer step name -> (chaining file path under parameters/, bicep template path parts)
    CONSUMERS = [
        (
            "resolve-extensions",
            ("inputs", "aio-upgrade-resolve-extensions.yaml"),
            ("templates", "aio", "upgrade", "resolve-extensions.bicep"),
        ),
        (
            "update-extensions",
            ("inputs", "aio-upgrade-update-extensions.yaml"),
            ("templates", "aio", "upgrade", "update-extensions.bicep"),
        ),
    ]

    def _bicep_params(self, bicep: Path) -> tuple[set[str], set[str]]:
        """Return (all_params, required_params) for a Bicep template."""
        text = bicep.read_text(encoding="utf-8")
        all_params: set[str] = set()
        required: set[str] = set()
        for match in self.PARAM_DECL_RE.finditer(text):
            name = match.group(1)
            has_default = match.group(2) is not None
            all_params.add(name)
            if not has_default:
                required.add(name)
        return all_params, required

    def _bicep_outputs(self, bicep: Path) -> set[str]:
        return set(self.OUTPUT_RE.findall(bicep.read_text(encoding="utf-8")))

    def _chaining_keys(self, chaining: Path) -> set[str]:
        with open(chaining, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return set(data.keys())

    def _release_yaml_keys(self, workspace: Path) -> set[str]:
        """Return the INTERSECTION of keys across all release YAML files.

        Required params must be satisfiable regardless of which release file the
        operator pins; using the intersection guarantees that. A separate test
        (`TestReleaseConfigs.test_release_yaml_keys_consistent_across_files`)
        asserts the key sets match exactly to catch divergence.
        """
        release_files = sorted((workspace / "parameters" / "aio-releases").glob("*.yaml"))
        assert release_files, "no aio-releases YAML files found"
        per_file_keys: list[set[str]] = []
        for release_file in release_files:
            with open(release_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            per_file_keys.append(set(data.keys()))
        return set.intersection(*per_file_keys)

    def _get_chaining_refs(self, chaining: Path) -> list[tuple[str, str, str]]:
        text = chaining.read_text(encoding="utf-8")
        return [
            (m.group(1), m.group(2), m.group(0))
            for m in STEP_OUTPUT_PATTERN.finditer(text)
        ]

    def test_aio_upgrade_chaining_refs_valid_steps(self, workspace):
        from siteops.models import Manifest
        manifest = Manifest.from_file(workspace / "manifests" / "aio-upgrade.yaml", workspace_root=workspace)
        manifest_steps = {step.name for step in manifest.steps}

        for _, chaining_parts, _ in self.CONSUMERS:
            chaining = workspace / "parameters" / Path(*chaining_parts)
            chaining_name = chaining_parts[-1]
            for step_name, _, raw in self._get_chaining_refs(chaining):
                assert step_name in manifest_steps, (
                    f"{chaining_name} references unknown step "
                    f"'{step_name}': {raw}"
                )

    def test_aio_upgrade_chaining_refs_valid_outputs(self, workspace):
        outputs_by_step = {
            "resolve-aio": self._bicep_outputs(
                workspace / "templates" / "aio" / "resolve-aio.bicep"
            ),
            "resolve-extensions": self._bicep_outputs(
                workspace / "templates" / "aio" / "upgrade" / "resolve-extensions.bicep"
            ),
        }
        for _, chaining_parts, _ in self.CONSUMERS:
            chaining = workspace / "parameters" / Path(*chaining_parts)
            chaining_name = chaining_parts[-1]
            for step_name, output_path, raw in self._get_chaining_refs(chaining):
                top_level = output_path.split(".")[0]
                available = outputs_by_step.get(step_name)
                assert available is not None, (
                    f"{chaining_name} references step '{step_name}' "
                    f"with no known template mapping in this test"
                )
                assert top_level in available, (
                    f"{chaining_name} references unknown output "
                    f"'{top_level}' from {step_name}: {raw}\n"
                    f"Available outputs: {sorted(available)}"
                )

    def test_aio_upgrade_required_params_satisfied(self, workspace):
        """Every required Bicep param on each upgrade consumer must be supplied."""
        release_keys = self._release_yaml_keys(workspace)
        for _, chaining_parts, bicep_parts in self.CONSUMERS:
            chaining_path = workspace / "parameters" / Path(*chaining_parts)
            chaining_name = chaining_parts[-1]
            chaining_keys = self._chaining_keys(chaining_path)
            supplied = chaining_keys | release_keys
            consumer = workspace.joinpath(*bicep_parts)
            _, required = self._bicep_params(consumer)
            missing = required - supplied
            assert missing == set(), (
                f"{consumer.name} has required params not satisfied by "
                f"{chaining_name} or aio-releases YAML: {sorted(missing)}.\n"
                f"Chaining keys: {sorted(chaining_keys)}\n"
                f"Release keys: {sorted(release_keys)}"
            )

    def test_aio_upgrade_chaining_keys_consumed(self, workspace):
        """Every chaining key should map to a param on its consumer.

        Catches stale chaining entries left behind by refactors. With
        per-consumer chaining files this is now a tight 1:1 check rather
        than a union check.
        """
        for _, chaining_parts, bicep_parts in self.CONSUMERS:
            chaining_path = workspace / "parameters" / Path(*chaining_parts)
            chaining_name = chaining_parts[-1]
            chaining_keys = self._chaining_keys(chaining_path)
            consumer = workspace.joinpath(*bicep_parts)
            params, _ = self._bicep_params(consumer)
            unused = chaining_keys - params
            assert unused == set(), (
                f"{chaining_name} has keys not consumed by "
                f"{consumer.name}: {sorted(unused)}"
            )

    def test_aio_extension_name_deriver_parity(self, workspace):
        """The upgrade flow must call aioExtensionName(connectedClusterResourceId)
        with the SAME argument the install path uses, so the derived name matches
        what install stamped. Both sides must accept the full cluster resource ID.
        """
        ext_names = (
            workspace / "templates" / "common" / "extension-names.bicep"
        ).read_text(encoding="utf-8")
        # The deriver function must take a clusterResourceId arg.
        assert re.search(
            r"func\s+aioExtensionName\s*\(\s*clusterResourceId\s+string\s*\)",
            ext_names,
        ), "aioExtensionName(clusterResourceId) signature changed; install/upgrade parity at risk"

        # Install side passes clusterResourceId.
        for install_module in [
            workspaces_path
            for workspaces_path in [
                workspace / "templates" / "aio" / "modules" / "instance-2025-10-01.bicep",
                workspace / "templates" / "aio" / "modules" / "instance-2026-03-01.bicep",
            ]
        ]:
            text = install_module.read_text(encoding="utf-8")
            assert "deriveAioExtensionName(clusterResourceId)" in text, (
                f"{install_module.name} must call deriveAioExtensionName(clusterResourceId) "
                f"to stamp the install-time extension name"
            )

        # Upgrade side imports + calls the same deriver with the chained cluster ID.
        resolve_ext = (
            workspace / "templates" / "aio" / "upgrade" / "resolve-extensions.bicep"
        ).read_text(encoding="utf-8")
        assert "aioExtensionName as deriveAioExtensionName" in resolve_ext, (
            "resolve-extensions.bicep must import the shared aioExtensionName deriver"
        )
        assert "deriveAioExtensionName(connectedClusterResourceId)" in resolve_ext, (
            "resolve-extensions.bicep must call deriveAioExtensionName with the "
            "chained connectedClusterResourceId; otherwise the resolved name "
            "will not match the name install stamped"
        )



# Required fields in every version config file
VERSION_CONFIG_REQUIRED_FIELDS = {
    "aioVersion",
    "aioTrain",
    "aioApiVersion",
    "certManagerVersion",
    "certManagerTrain",
    "secretStoreVersion",
    "secretStoreTrain",
}


class TestReleaseConfigs:
    """Release config YAML files should be valid and consistent."""

    def _get_release_files(self, workspace: Path) -> list[Path]:
        releases_dir = workspace / "parameters" / "aio-releases"
        return sorted(releases_dir.glob("*.yaml"))

    def test_release_files_exist(self, workspace):
        """At least one release config should exist."""
        files = self._get_release_files(workspace)
        assert len(files) >= 1, "No release config files found in parameters/aio-releases/"

    def test_release_configs_have_required_fields(self, workspace):
        """Every release config must have all required fields."""
        for release_file in self._get_release_files(workspace):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            actual_keys = set(config.keys())
            missing = VERSION_CONFIG_REQUIRED_FIELDS - actual_keys
            assert missing == set(), (
                f"{release_file.name} missing required fields: {missing}"
            )

    def test_release_config_values_are_non_empty(self, workspace):
        """All release config values must be non-empty strings."""
        for release_file in self._get_release_files(workspace):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            for key in VERSION_CONFIG_REQUIRED_FIELDS:
                value = config.get(key)
                assert value is not None and str(value).strip() != "", (
                    f"{release_file.name}: '{key}' is empty or missing"
                )

    def test_base_site_aio_release_has_config_file(self, workspace):
        """The aioRelease in base-site.yaml must have a matching config file."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        aio_release = data.get("properties", {}).get("aioRelease")
        assert aio_release, "base-site.yaml missing properties.aioRelease"

        release_file = workspace / "parameters" / "aio-releases" / f"{aio_release}.yaml"
        assert release_file.exists(), (
            f"base-site.yaml references aioRelease '{aio_release}' "
            f"but parameters/aio-releases/{aio_release}.yaml does not exist"
        )

    def test_all_sites_aio_releases_have_config_files(self, workspace):
        """Every committed site that pins an aioRelease must reference an existing config file.

        Catches drift where a site is added or updated to use a release whose YAML
        was never created (e.g., typo, or deleted release without migrating sites).
        """
        releases_dir = workspace / "parameters" / "aio-releases"
        sites_dir = workspace / "sites"
        if not sites_dir.exists():
            return

        for site_file in sorted(sites_dir.glob("*.yaml")):
            with open(site_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            aio_release = (data.get("properties") or {}).get("aioRelease")
            if not aio_release:
                continue
            release_file = releases_dir / f"{aio_release}.yaml"
            assert release_file.exists(), (
                f"{site_file.name} references aioRelease '{aio_release}' "
                f"but parameters/aio-releases/{aio_release}.yaml does not exist"
            )

    def test_release_yaml_keys_consistent_across_files(self, workspace):
        """All aio-releases YAML files should declare the same key set.

        If `2603.yaml` adds a key like `storageVersion` that `2512.yaml` doesn't
        have, upgrades to older targets would fail with missing required params
        (or silently use defaults). Catch divergence early.
        """
        release_files = self._get_release_files(workspace)
        assert release_files, "no aio-releases YAML files found"
        per_file: dict[str, set[str]] = {}
        for release_file in release_files:
            with open(release_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            per_file[release_file.name] = set(data.keys())

        common = set.intersection(*per_file.values())
        for fname, keys in per_file.items():
            extra = keys - common
            missing = common - keys
            assert extra == set() and missing == set(), (
                f"{fname} key set diverges from other release files.\n"
                f"  extra keys: {sorted(extra)}\n"
                f"  missing keys: {sorted(missing)}\n"
                f"All release files must declare the same key set."
            )

    def test_version_config_api_versions_are_allowed_in_bicep(self, workspace):
        """Every aioApiVersion must appear in the @allowed list of the dispatching bicep templates.

        Single source of truth: the @allowed([...]) block in templates/aio/instance.bicep
        and templates/aio/modules/update-instance.bicep. Prevents shipping a version YAML
        whose aioApiVersion the templates cannot route to (which would only surface at
        deploy time as an opaque Bicep parameter error).
        """
        dispatchers = [
            workspace / "templates" / "aio" / "instance.bicep",
            workspace / "templates" / "aio" / "modules" / "update-instance.bicep",
            workspace / "templates" / "aio" / "resolve-aio.bicep",
            workspace / "templates" / "secretsync" / "enable-secretsync.bicep",
        ]

        # Extract the @allowed([...]) block immediately preceding `param aioApiVersion`.
        # Matches:  @allowed([\n  '2025-10-01'\n  '2026-03-01'\n])\nparam aioApiVersion
        allowed_block_re = re.compile(
            r"@allowed\(\s*\[([^\]]*)\]\s*\)\s*param\s+aioApiVersion\b",
            re.MULTILINE,
        )
        literal_re = re.compile(r"'([^']+)'")

        def extract_allowed(bicep_path: Path) -> set[str]:
            text = bicep_path.read_text(encoding="utf-8")
            match = allowed_block_re.search(text)
            assert match, f"{bicep_path.name}: could not find @allowed block before `param aioApiVersion`"
            return set(literal_re.findall(match.group(1)))

        allowed_sets = {p.name: extract_allowed(p) for p in dispatchers}
        # Sanity: both dispatchers must agree on the allowed set.
        values = list(allowed_sets.values())
        assert all(s == values[0] for s in values), (
            f"@allowed lists for aioApiVersion diverge between dispatchers: {allowed_sets}"
        )
        allowed = values[0]
        assert allowed, "No @allowed values parsed (regex or template changed)"

        for release_file in self._get_release_files(workspace):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            api_version = config.get("aioApiVersion")
            assert api_version in allowed, (
                f"{release_file.name}: aioApiVersion '{api_version}' is not in the "
                f"@allowed set {sorted(allowed)} declared by "
                f"{', '.join(sorted(allowed_sets.keys()))}. "
                f"Add the new API version to both dispatchers' @allowed blocks and "
                f"their ternary dispatch before shipping this version YAML."
            )

    def test_version_config_adr_api_versions_are_allowed_in_bicep(self, workspace):
        """Every adrApiVersion must appear in the @allowed list of templates/deps/adr-ns.bicep.

        Same shape as test_version_config_api_versions_are_allowed_in_bicep but
        for the ADR (Microsoft.DeviceRegistry) dispatch. The ADR namespace API
        version moves with AIO releases (devices/assets project to cluster).
        """
        dispatcher = workspace / "templates" / "deps" / "adr-ns.bicep"
        text = dispatcher.read_text(encoding="utf-8")
        match = re.search(
            r"@allowed\(\s*\[([^\]]*)\]\s*\)\s*param\s+adrApiVersion\b",
            text,
            re.MULTILINE,
        )
        assert match, (
            f"{dispatcher.name}: could not find @allowed block before "
            f"`param adrApiVersion`"
        )
        allowed = set(re.findall(r"'([^']+)'", match.group(1)))
        assert allowed, "No @allowed values parsed for adrApiVersion"

        for release_file in self._get_release_files(workspace):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            api_version = config.get("adrApiVersion")
            assert api_version in allowed, (
                f"{release_file.name}: adrApiVersion '{api_version}' is not in "
                f"the @allowed set {sorted(allowed)} declared by "
                f"{dispatcher.name}. Add the new API version to the @allowed "
                f"block and dispatch in {dispatcher.name} (and create a matching "
                f"per-version module under templates/deps/modules/) before "
                f"shipping this version YAML."
            )

    def test_version_config_adr_api_versions_have_module(self, workspace):
        """Every adrApiVersion must have a matching templates/deps/modules/adr-ns-<ver>.bicep.

        Parallel to test_version_config_api_versions_are_allowed_in_bicep but for
        the per-version module file the ADR dispatcher routes to. Catches the
        case where the @allowed list is updated but the module file is missing.
        """
        modules_dir = workspace / "templates" / "deps" / "modules"
        for release_file in self._get_release_files(workspace):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            api_version = config.get("adrApiVersion")
            module_path = modules_dir / f"adr-ns-{api_version}.bicep"
            assert module_path.is_file(), (
                f"{release_file.name}: adrApiVersion '{api_version}' has no "
                f"matching module at {module_path.relative_to(workspace)}. "
                f"Create the per-version module by copying the previous one "
                f"and changing the API version string."
            )

    def test_version_config_aio_api_versions_have_modules(self, workspace):
        """Every aioApiVersion must have matching instance/resolve-instance/update-instance modules."""
        modules_dir = workspace / "templates" / "aio" / "modules"
        for release_file in self._get_release_files(workspace):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            api_version = config.get("aioApiVersion")
            for prefix in ("instance", "resolve-instance", "update-instance"):
                module_path = modules_dir / f"{prefix}-{api_version}.bicep"
                assert module_path.is_file(), (
                    f"{release_file.name}: aioApiVersion '{api_version}' has no "
                    f"matching {prefix} module at {module_path.relative_to(workspace)}."
                )


class TestSampleTemplateApiPolicy:
    """Sample templates under samples/ pin to the oldest supported API version.

    Rationale: a single sample template that works against every shipped release
    avoids per-version dispatch in samples. See docs/aio-releases.md
    ("Sample template API-version policy").
    """

    _RP_TO_VERSION_KEY = {
        "Microsoft.IoTOperations": "aioApiVersion",
        "Microsoft.DeviceRegistry": "adrApiVersion",
    }

    def _oldest_versions(self, workspace: Path) -> dict[str, str]:
        releases_dir = workspace / "parameters" / "aio-releases"
        oldest: dict[str, str] = {}
        for release_file in sorted(releases_dir.glob("*.yaml")):
            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            for rp, key in self._RP_TO_VERSION_KEY.items():
                value = config.get(key)
                if value is None:
                    continue
                if rp not in oldest or value < oldest[rp]:
                    oldest[rp] = value
        return oldest

    def test_samples_pin_to_oldest_api_version(self, workspace):
        """Every Microsoft.IoTOperations / Microsoft.DeviceRegistry reference under
        samples/ must equal the oldest API version in the release-YAML matrix.

        If this test fails after shipping a newer version YAML, the fix is to
        leave the sample alone. If it fails because the oldest version was
        retired from the matrix, bump the pin in the sample to match the new
        oldest.
        """
        oldest = self._oldest_versions(workspace)
        assert oldest, "Could not derive oldest API versions from version YAMLs"

        rp_pattern = re.compile(r"(Microsoft\.(?:IoTOperations|DeviceRegistry))/[^@'\s]+@(\d{4}-\d{2}-\d{2}(?:-preview)?)")
        samples_dir = workspace / "samples"
        bicep_files = list(samples_dir.rglob("*.bicep"))
        assert bicep_files, f"No bicep files found under {samples_dir}"

        violations: list[str] = []
        for bicep in bicep_files:
            text = bicep.read_text(encoding="utf-8")
            for match in rp_pattern.finditer(text):
                rp = match.group(1)
                api_version = match.group(2)
                expected = oldest.get(rp)
                if expected is None:
                    continue
                if api_version != expected:
                    violations.append(
                        f"{bicep.relative_to(workspace)}: {rp} pinned to "
                        f"'{api_version}' but oldest supported is '{expected}'"
                    )
        assert not violations, (
            "Sample templates must pin to the oldest supported API version "
            "(see docs/aio-releases.md 'Sample template API-version policy'):\n  "
            + "\n  ".join(violations)
        )
