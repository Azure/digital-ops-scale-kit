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

    def test_resolve_aio_outputs_all_have_consumers(self, workspace):
        """Every output resolve-aio emits is chained by a parameter file a manifest attaches.

        The resolve step exists to feed downstream steps, so an output nothing
        reads is either dead weight or a half-wired mechanism whose consumer was
        never added. Only files a manifest actually attaches count, since a
        reference sitting in an unreferenced file consumes nothing at deploy time.
        """
        from tests.workspace.test_manifest_validation import _all_manifest_files

        resolve_aio = workspace / "templates" / "aio" / "resolve-aio.bicep"
        outputs = set(
            re.findall(r"^output\s+(\w+)\s+", resolve_aio.read_text(encoding="utf-8"), re.MULTILINE)
        )
        assert outputs, "No outputs found in resolve-aio.bicep"

        attached: set[Path] = set()
        for manifest_path in _all_manifest_files(workspace):
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            declared = list(raw.get("parameters") or [])
            for step in raw.get("steps") or []:
                if isinstance(step, dict):
                    declared.extend(step.get("parameters") or [])
            for param_path in declared:
                if not isinstance(param_path, str):
                    continue
                # A path variable selects one of a set, so check every candidate.
                pattern = re.sub(r"\{\{[^}]*\}\}", "*", param_path)
                attached.update(workspace.glob(pattern))

        assert attached, "No manifest-attached parameter files resolved"

        consumed: set[str] = set()
        for param_file in sorted(attached):
            for step_name, output_path, _ in self._get_chaining_refs(param_file):
                if step_name == "resolve-aio":
                    consumed.add(output_path.split(".")[0])

        orphaned = outputs - consumed
        assert not orphaned, (
            f"resolve-aio.bicep emits outputs no attached parameter file consumes: "
            f"{sorted(orphaned)}. Either chain each into the step that needs it, or drop it."
        )

    def test_every_chained_output_is_declared_by_its_producer(self, workspace):
        """Every `{{ steps.X.outputs.Y }}` names an output step X's template declares.

        The engine validates that step X exists and runs earlier, but not that
        Y is real. A reference to a misspelled output resolves to nothing, and
        the step deploys with that parameter missing. Per-file tests covered
        some chaining files and not others, so this discovers them instead.

        Only the first path segment is checked. A nested path such as
        `customLocation.id` indexes into an output object whose shape lives in
        the template's own expression rather than in its output declarations.
        """
        from tests.workspace.test_manifest_validation import _all_manifest_files

        output_decl = re.compile(r"^output\s+(\w+)\s+", re.MULTILINE)
        checked = 0
        failures: list[str] = []

        for manifest_path in _all_manifest_files(workspace):
            from siteops.models import Manifest

            manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            template_for_step = {
                s.name: getattr(s, "template", None) for s in manifest.steps
            }

            # Read the flattened manifest rather than the raw YAML. A step
            # contributed by an `include:` carries its own parameter files, and
            # those are invisible in the raw steps list, which holds the include
            # entry instead.
            declared = list(manifest.parameters or [])
            for step in manifest.steps:
                declared.extend(getattr(step, "parameters", []) or [])

            param_files: set[Path] = set()
            for param_path in declared:
                if not isinstance(param_path, str):
                    continue
                pattern = re.sub(r"\{\{[^}]*\}\}", "*", param_path)
                param_files.update(workspace.glob(pattern))

            for param_file in sorted(param_files):
                for step_name, output_path, raw_ref in self._get_chaining_refs(param_file):
                    template = template_for_step.get(step_name)
                    if not template:
                        # The producer is spliced in by whatever composes this
                        # manifest, and is checked there.
                        continue
                    template_file = workspace / template
                    if not template_file.exists() or template_file.suffix != ".bicep":
                        continue
                    declared_outputs = set(
                        output_decl.findall(template_file.read_text(encoding="utf-8"))
                    )
                    if not declared_outputs:
                        continue

                    checked += 1
                    root = output_path.split(".")[0]
                    if root not in declared_outputs:
                        failures.append(
                            f"{param_file.relative_to(workspace)} references "
                            f"'{root}' from step '{step_name}', which "
                            f"{template} does not declare: {raw_ref}\n"
                            f"    Available: {sorted(declared_outputs)}"
                        )

        assert checked > 0, (
            "No chained output references were checked, so this test would pass "
            "without examining anything. If the chaining convention moved, "
            "update the discovery rather than deleting the test."
        )
        assert not failures, "\n\n".join(failures)

    def test_secret_provider_class_preservation_is_wired(self, workspace):
        """Enablement still preserves an existing object list rather than dropping it.

        Both templates PUT the Secret Provider Class. When a site declares no
        secrets, enablement keeps the cluster's current `objects` by reading them
        back. That spans the resolve output, the chaining file, the parameter, and
        the module whose result reaches the write, so this asserts the behavior
        rather than the declarations that support it.
        """
        resolve_output = "defaultSecretProviderClassResourceId"
        enable_param = "existingSpcResourceId"

        resolve_aio = (workspace / "templates" / "aio" / "resolve-aio.bicep").read_text(encoding="utf-8")
        assert re.search(rf"^output\s+{resolve_output}\s+", resolve_aio, re.MULTILINE), (
            f"resolve-aio.bicep no longer emits '{resolve_output}', which enablement "
            f"needs to read the existing Secret Provider Class."
        )

        # Parse the mapping rather than pattern-matching raw text, so requoting or
        # reformatting the YAML does not fail the test.
        chaining_file = workspace / "parameters" / "inputs" / "secretsync.yaml"
        mapping = yaml.safe_load(chaining_file.read_text(encoding="utf-8")) or {}
        expected = "{{ steps.resolve-aio.outputs." + resolve_output + " }}"
        assert mapping.get(enable_param) == expected, (
            f"inputs/secretsync.yaml no longer maps '{resolve_output}' to "
            f"'{enable_param}'. Without it enablement receives an empty id and "
            f"writes the Secret Provider Class without preserving its object list. "
            f"Found: {mapping.get(enable_param)!r}"
        )

        enable = (workspace / "templates" / "secretsync" / "enable-secretsync.bicep").read_text(encoding="utf-8")
        assert re.search(rf"^param\s+{enable_param}\s+string", enable, re.MULTILINE), (
            f"enable-secretsync.bicep no longer declares '{enable_param}', so the "
            f"chained value would be filtered out before it reaches the template."
        )

        # Declaring the parameter is not enough. The value the class is written with
        # has to actually come from the module that reads the current one, otherwise
        # every link above can stay intact while the preservation itself is gone.
        module_match = re.search(
            r"module\s+(\w+)\s+'\./modules/read-spc-objects\.bicep'\s*=\s*if\s*\(([^)]+)\)",
            enable,
        )
        assert module_match, (
            "enable-secretsync.bicep no longer instantiates modules/read-spc-objects.bicep "
            "behind a condition, so it cannot preserve an existing object list."
        )
        module_symbol, module_condition = module_match.group(1), module_match.group(2)
        # The condition is usually a variable, so resolve one level of indirection
        # before checking that the guard ultimately depends on the chained id.
        condition_source = module_condition
        var_match = re.search(
            rf"^var\s+{re.escape(module_condition.strip())}\s*=\s*(.+)$", enable, re.MULTILINE
        )
        if var_match:
            condition_source = var_match.group(1)
        assert enable_param in condition_source, (
            f"the read module's guard does not depend on '{enable_param}', so it would "
            f"run on a first install where there is nothing to read. "
            f"Found: {condition_source!r}"
        )

        objects_var = re.search(r"^var\s+spcObjects\s*=\s*(.+)$", enable, re.MULTILINE)
        assert objects_var, "enable-secretsync.bicep no longer defines `spcObjects`."
        assert f"{module_symbol}!.outputs" in objects_var.group(1), (
            f"`spcObjects` does not read from the '{module_symbol}' module, so the class "
            f"is written without the object list it currently carries. "
            f"Found: {objects_var.group(1)!r}"
        )

        spc_write = re.search(
            r"resource\s+spc\s+'Microsoft\.SecretSyncController[^']*'\s*=\s*\{(.+?)\n\}",
            enable,
            re.DOTALL,
        )
        assert spc_write and "spcObjects" in spc_write.group(1), (
            "the Secret Provider Class resource no longer writes `spcObjects`, so the "
            "resolved object list never reaches the PUT."
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


class TestArmResourceShape:
    """Resource bodies keep the shape ARM can validate before deployment."""

    def test_properties_are_object_literals(self, workspace):
        """No resource builds its whole `properties` from a function call.

        A function such as `union()` compiles the entire object into one
        expression. ARM cannot evaluate that before preflight when any part of
        it reads a resource or a module output, so the provider receives the
        unevaluated string and rejects the deployment. Per-property expressions
        inside an object literal are fine, which is what every provider expects.
        Templates compile either way, so this is the only place it is caught
        before a live deploy.
        """
        pattern = re.compile(r"^\s*properties:\s*([A-Za-z_]\w*)\s*\(", re.MULTILINE)
        violations: list[str] = []
        for bicep in sorted(workspace.rglob("*.bicep")):
            for match in pattern.finditer(bicep.read_text(encoding="utf-8")):
                line = bicep.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
                violations.append(
                    f"{bicep.relative_to(workspace)}:{line}: properties built by "
                    f"{match.group(1)}()"
                )

        assert not violations, (
            "Build `properties` as an object literal and put any condition on the "
            "individual property value instead.\n" + "\n".join(violations)
        )


class TestParameterAttachmentTier:
    """A parameter file's attachment tier follows from what the file is.

    Chaining files carry `{{ steps.X.outputs.Y }}` wiring and attach at step
    level, the highest-precedence tier, scoped to the one consumer. Declaration
    files carry operator intent and attach at manifest level, below site
    parameters, so a site overlay overrides them and every step in the pipeline
    reads the same values.
    """

    def _manifest_level_parameter_paths(self, manifest_path: Path) -> list[str]:
        """Read the raw manifest-level `parameters:` list, path variables intact."""
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return [p for p in (raw.get("parameters") or []) if isinstance(p, str)]

    def test_manifest_level_parameters_carry_no_step_output_refs(self, workspace):
        """Manifest-level parameter files declare values rather than wire steps.

        Manifest-level parameters apply to every step in the flattened pipeline,
        including steps that run before a referenced producer, so a step output
        reference there cannot be guaranteed to resolve. Structural validation
        checks output references on step-level files only, which is why this
        boundary is worth asserting directly.
        """
        from tests.workspace.test_manifest_validation import _all_manifest_files

        manifests = _all_manifest_files(workspace)
        assert manifests, "No manifests discovered"

        violations: list[str] = []
        checked = 0
        for manifest_path in manifests:
            for param_path in self._manifest_level_parameter_paths(manifest_path):
                # A path variable such as {{ site.properties.aioRelease }} selects
                # one of a set of files. Check every file it can resolve to.
                pattern = re.sub(r"\{\{[^}]*\}\}", "*", param_path)
                for param_file in sorted(workspace.glob(pattern)):
                    checked += 1
                    for _, _, raw in TestParameterChaining()._get_chaining_refs(param_file):
                        violations.append(
                            f"{manifest_path.relative_to(workspace)} attaches "
                            f"{param_file.relative_to(workspace)} at manifest level, "
                            f"but it chains a step output: {raw}"
                        )

        assert checked > 0, "No manifest-level parameter files resolved, so nothing was checked"
        assert not violations, (
            "Manifest-level parameter files must not chain step outputs. Move the "
            "chaining keys into a step-level file and leave the declared values at "
            "manifest level.\n" + "\n".join(violations)
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

    def test_output_consumers_share_their_producer_guard(self, workspace):
        """A step that chains an output runs under the same guard as its producer.

        A gated producer that is skipped emits no outputs, so an ungated consumer
        resolves `{{ steps.X.outputs.Y }}` to nothing and sends the literal
        template to ARM. Include-level `when:` propagates to every spliced step,
        so this compares the post-flatten guards rather than the authored ones.
        """
        from siteops.models import Manifest
        from tests.workspace.test_manifest_validation import _all_manifest_files

        violations: list[str] = []
        for manifest_path in _all_manifest_files(workspace):
            manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            guards = {s.name: (s.when or "") for s in manifest.steps}

            for step in manifest.steps:
                for param_path in getattr(step, "parameters", []) or []:
                    pattern = re.sub(r"\{\{[^}]*\}\}", "*", param_path)
                    for param_file in sorted(workspace.glob(pattern)):
                        for producer, _, raw in TestParameterChaining()._get_chaining_refs(param_file):
                            # A producer outside this manifest is spliced in by
                            # whatever composes it, and is checked there.
                            if producer not in guards:
                                continue
                            if guards[producer] and guards[producer] != guards[step.name]:
                                violations.append(
                                    f"{manifest_path.relative_to(workspace)}: step "
                                    f"'{step.name}' chains {raw} from '{producer}', "
                                    f"which is gated by \"{guards[producer]}\" while the "
                                    f"consumer is gated by \"{guards[step.name] or 'nothing'}\""
                                )

        assert not violations, (
            "A step that consumes an output must run under its producer's guard. "
            "Give the consumer the same `when:`, or gate the include that contributes "
            "it.\n" + "\n".join(sorted(set(violations)))
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
    update-extensions -> deploy-release-resources through per-consumer
    chaining files (one chaining YAML per consumer step, named after the
    manifest + consumer step).
    Each consumer step's required Bicep params must be satisfied by
    its chaining file, a fixed manifest-level parameter file, or the
    version YAML (parameters/aio-releases/<release>.yaml), and every
    chained `{{ steps.X.outputs.Y }}` reference must hit a real output.
    A break here would silently produce wrong PUTs at deploy time.

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
        (
            "deploy-release-resources",
            ("inputs", "aio-upgrade-deploy-release-resources.yaml"),
            ("templates", "aio", "upgrade", "deploy-release-resources.bicep"),
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

    def _fixed_manifest_parameter_keys(self, workspace: Path) -> set[str]:
        manifest_path = workspace / "manifests" / "aio-upgrade.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        keys: set[str] = set()
        for parameter_path in manifest.get("parameters", []):
            if "{{" in parameter_path:
                continue
            with open(workspace / parameter_path, "r", encoding="utf-8") as f:
                keys.update((yaml.safe_load(f) or {}).keys())
        return keys

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
        fixed_manifest_keys = self._fixed_manifest_parameter_keys(workspace)
        for _, chaining_parts, bicep_parts in self.CONSUMERS:
            chaining_path = workspace / "parameters" / Path(*chaining_parts)
            chaining_name = chaining_parts[-1]
            chaining_keys = self._chaining_keys(chaining_path)
            supplied = chaining_keys | release_keys | fixed_manifest_keys
            consumer = workspace.joinpath(*bicep_parts)
            _, required = self._bicep_params(consumer)
            missing = required - supplied
            assert missing == set(), (
                f"{consumer.name} has required params not satisfied by "
                f"{chaining_name}, fixed manifest parameters, or "
                f"aio-releases YAML: {sorted(missing)}.\n"
                f"Chaining keys: {sorted(chaining_keys)}\n"
                f"Fixed manifest keys: {sorted(fixed_manifest_keys)}\n"
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

        # Every install-side module passes the full cluster resource ID.
        install_modules = sorted(
            (workspace / "templates" / "aio" / "modules").glob(
                "instance-*.bicep"
            )
        )
        assert install_modules
        for install_module in install_modules:
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
        assert "deriveAioExtensionSuffix(connectedClusterResourceId)" in resolve_ext



# Required fields in every version config file
VERSION_CONFIG_REQUIRED_FIELDS = {
    "aioVersion",
    "aioTrain",
    "aioApiVersion",
    "adrApiVersion",
    "aioReleaseConfiguration",
    "certManagerVersion",
    "certManagerTrain",
    "certManagerConfigurationOverrides",
    "secretStoreVersion",
    "secretStoreTrain",
}

VERSION_CONFIG_OBJECT_FIELDS = {
    "aioReleaseConfiguration",
    "certManagerConfigurationOverrides",
}

AIO_RELEASE_CONFIGURATION_SECTIONS = {"extension", "resources"}
AIO_RELEASE_EXTENSION_OPTIONAL_FIELDS = {
    "configurationOverrides": dict,
}
AIO_RELEASE_EXTENSION_SCOPED_FIELDS = {
    "securityPkiSubjectName": ({2605, 2606}, bool),
    "wasmGraphControllerMqttTrust": ({2608}, bool),
}
AIO_RELEASE_RESOURCE_SCOPED_FIELDS = {
    "opcUaConnector": (2608, dict),
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
                if key in VERSION_CONFIG_OBJECT_FIELDS:
                    assert isinstance(value, dict), (
                        f"{release_file.name}: '{key}' must be an object"
                    )
                else:
                    assert value is not None and str(value).strip() != "", (
                        f"{release_file.name}: '{key}' is empty or missing"
                    )

    def test_release_config_value_shapes(self, workspace):
        """Release metadata must be well formed for the contracts that consume it.

        Extension versions reach Helm, and API versions are substituted into
        Bicep dispatchers, so a malformed value here surfaces as a deployment
        failure rather than a validation error. Shapes are asserted rather than
        exact values, which git history already records.
        """
        semver = re.compile(r"^\d+\.\d+\.\d+")
        api_version = re.compile(r"^\d{4}-\d{2}-\d{2}(-preview)?$")

        for release_file in self._get_release_files(workspace):
            assert release_file.stem.isdigit(), (
                f"{release_file.name}: release moniker must be numeric so releases order"
            )

            with open(release_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            for key in ("aioVersion", "certManagerVersion", "secretStoreVersion"):
                assert semver.match(str(config[key])), (
                    f"{release_file.name}: '{key}' is not a version: {config[key]!r}"
                )

            for key in ("aioApiVersion", "adrApiVersion"):
                assert api_version.match(str(config[key])), (
                    f"{release_file.name}: '{key}' is not an ARM API version: {config[key]!r}"
                )

            release_configuration = config["aioReleaseConfiguration"]
            unknown = set(release_configuration) - AIO_RELEASE_CONFIGURATION_SECTIONS
            assert not unknown, (
                f"{release_file.name}: aioReleaseConfiguration has unknown "
                f"sections: {sorted(unknown)}"
            )

            extension = release_configuration.get("extension", {})
            resources = release_configuration.get("resources", {})
            assert isinstance(extension, dict), (
                f"{release_file.name}: aioReleaseConfiguration.extension "
                "must be a mapping"
            )
            assert isinstance(resources, dict), (
                f"{release_file.name}: aioReleaseConfiguration.resources "
                "must be a mapping"
            )

            allowed_extension = (
                set(AIO_RELEASE_EXTENSION_OPTIONAL_FIELDS)
                | set(AIO_RELEASE_EXTENSION_SCOPED_FIELDS)
            )
            unknown_extension = set(extension) - allowed_extension
            assert not unknown_extension, (
                f"{release_file.name}: aioReleaseConfiguration.extension has "
                f"unknown fields: {sorted(unknown_extension)}"
            )

            unknown_resources = set(resources) - set(
                AIO_RELEASE_RESOURCE_SCOPED_FIELDS
            )
            assert not unknown_resources, (
                f"{release_file.name}: aioReleaseConfiguration.resources has "
                f"unknown fields: {sorted(unknown_resources)}"
            )

            release_number = int(release_file.stem)
            expected_extension = {
                key
                for key, (
                    releases,
                    _,
                ) in AIO_RELEASE_EXTENSION_SCOPED_FIELDS.items()
                if release_number in releases
            }
            actual_extension = (
                set(extension) & set(AIO_RELEASE_EXTENSION_SCOPED_FIELDS)
            )
            assert actual_extension == expected_extension, (
                f"{release_file.name}: aioReleaseConfiguration.extension "
                f"scoped fields are {sorted(actual_extension)}, expected "
                f"{sorted(expected_extension)}."
            )

            expected_resources = {
                key
                for key, (
                    introduced,
                    _,
                ) in AIO_RELEASE_RESOURCE_SCOPED_FIELDS.items()
                if release_number >= introduced
            }
            actual_resources = (
                set(resources) & set(AIO_RELEASE_RESOURCE_SCOPED_FIELDS)
            )
            assert actual_resources == expected_resources, (
                f"{release_file.name}: aioReleaseConfiguration.resources "
                f"scoped fields are {sorted(actual_resources)}, expected "
                f"{sorted(expected_resources)}."
            )

            for key, expected_type in AIO_RELEASE_EXTENSION_OPTIONAL_FIELDS.items():
                if key in extension:
                    assert isinstance(extension[key], expected_type), (
                        f"{release_file.name}: "
                        f"aioReleaseConfiguration.extension.{key} "
                        f"must be a {expected_type.__name__}"
                    )

            for key, (_, expected_type) in AIO_RELEASE_EXTENSION_SCOPED_FIELDS.items():
                if key in extension:
                    assert isinstance(extension[key], expected_type), (
                        f"{release_file.name}: "
                        f"aioReleaseConfiguration.extension.{key} must be a "
                        f"{expected_type.__name__}, got {extension[key]!r}"
                    )

            connector = resources.get("opcUaConnector")
            if connector is not None:
                assert isinstance(connector, dict), (
                    f"{release_file.name}: "
                    "aioReleaseConfiguration.resources.opcUaConnector must be "
                    "a mapping"
                )
                unknown_connector = set(connector) - {"version"}
                assert not unknown_connector, (
                    f"{release_file.name}: "
                    "aioReleaseConfiguration.resources.opcUaConnector has "
                    f"unknown fields: {sorted(unknown_connector)}"
                )
                connector_version = connector.get("version")
                assert semver.match(str(connector_version)), (
                    f"{release_file.name}: "
                    "aioReleaseConfiguration.resources.opcUaConnector.version "
                    f"is not a version: {connector_version!r}"
                )

            overrides = config["certManagerConfigurationOverrides"]
            assert all(isinstance(k, str) for k in overrides), (
                f"{release_file.name}: configuration override keys must be strings"
            )

    def test_base_site_defaults_to_latest_supported_release(self, workspace):
        """The base site should select the highest release moniker that ships."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        default_release = data["properties"]["aioRelease"]
        latest_release = max(
            (f.stem for f in self._get_release_files(workspace)), key=int
        )
        assert default_release == latest_release

    def test_all_sites_aio_releases_have_config_files(self, workspace):
        """Every committed site resolves an aioRelease to an existing config file.

        Sites are read through the orchestrator so inheritance applies. Reading
        the raw YAML would have to skip a site that declares no `aioRelease` of
        its own, which cannot tell a site that inherits one from a site whose
        whole chain lacks it. The second case resolves the release path to a
        literal `{{ ... }}`, which names no file, and six manifests select a
        release that way.
        """
        from siteops.orchestrator import Orchestrator

        releases_dir = workspace / "parameters" / "aio-releases"
        sites_dir = workspace / "sites"
        if not sites_dir.exists():
            return

        orchestrator = Orchestrator(workspace)
        checked = 0
        for site_file in sorted(sites_dir.glob("*.yaml")):
            if site_file.name == "base-site.yaml":
                # A template rather than a deployable site.
                continue
            site = orchestrator.load_site(site_file.stem)
            aio_release = (site.properties or {}).get("aioRelease")
            assert aio_release, (
                f"{site_file.name} resolves no `properties.aioRelease`, and "
                f"neither does anything it inherits. Every manifest that pins a "
                f"release resolves its path from that key, so the path would "
                f"keep its template literal and name no file."
            )
            release_file = releases_dir / f"{aio_release}.yaml"
            assert release_file.exists(), (
                f"{site_file.name} references aioRelease '{aio_release}' "
                f"but parameters/aio-releases/{aio_release}.yaml does not exist"
            )
            checked += 1

        assert checked > 0, "No committed sites were checked."

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

    def test_version_config_adr_api_versions_have_module(self, workspace):
        """Every adrApiVersion must have a matching templates/deps/modules/adr-ns-<ver>.bicep.

        The dispatcher's `@allowed` list and the module files it routes to are
        separate facts. `test_aio_dispatch_shape.py` reads the dispatcher source
        for its routing conditions and never checks the file exists, so an
        allowed version whose module was deleted or never created is caught
        only here.
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

    _VERSIONED_MODULE = re.compile(r"-\d{4}-\d{2}-\d{2}(?:-preview)?\.bicep$")

    def _pin_violations(self, workspace: Path, bicep_files: list[Path]) -> list[str]:
        oldest = self._oldest_versions(workspace)
        assert oldest, "Could not derive oldest API versions from version YAMLs"

        rp_pattern = re.compile(
            r"(Microsoft\.(?:IoTOperations|DeviceRegistry))/[^@'\s]+@(\d{4}-\d{2}-\d{2}(?:-preview)?)"
        )
        violations: list[str] = []
        for bicep in bicep_files:
            text = bicep.read_text(encoding="utf-8")
            for match in rp_pattern.finditer(text):
                rp, api_version = match.group(1), match.group(2)
                expected = oldest.get(rp)
                if expected is not None and api_version != expected:
                    violations.append(
                        f"{bicep.relative_to(workspace)}: {rp} pinned to "
                        f"'{api_version}' but oldest supported is '{expected}'"
                    )
        return violations

    def test_templates_pin_to_oldest_api_version(self, workspace):
        """Workspace templates that are not per-version modules follow the same pin.

        A template naming a single API version literal serves every release, so it
        must name the oldest one. Files whose name carries an API version are the
        per-version modules a dispatcher routes to, and are exempt by definition.
        Keying the exemption on the filename rather than on a directory keeps a
        template from escaping the policy by living under `modules/`.
        """
        templates_dir = workspace / "templates"
        bicep_files = [
            f for f in sorted(templates_dir.rglob("*.bicep"))
            if not self._VERSIONED_MODULE.search(f.name)
        ]
        assert bicep_files, f"No bicep files found under {templates_dir}"

        violations = self._pin_violations(workspace, bicep_files)
        assert not violations, (
            "Templates that are not per-version modules must pin to the oldest "
            "supported API version, or route through a dispatcher "
            "(see docs/aio-releases.md):\n  " + "\n  ".join(violations)
        )

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
