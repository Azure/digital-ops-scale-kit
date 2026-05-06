# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Core data models for Azure Site Ops.

This module defines the core resource types:
- Site: A deployment target (subscription, resource group, location)
- Manifest: Orchestrates deployment steps across sites
- DeploymentStep: A single Bicep/ARM template deployment
- KubectlStep: A kubectl operation against an Arc-connected cluster

Resources support K8s-style apiVersion/kind validation:
- apiVersion defaults to 'siteops/v1' if not specified
- kind is validated if present, but optional
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import re
import yaml

VALID_SCOPES = {"subscription", "resourceGroup"}
DEFAULT_API_VERSION = "siteops/v1"
SUPPORTED_API_VERSIONS = {"siteops/v1"}

# Maximum depth of recursive `include:` resolution. Anything deeper is a smell;
# the cap exists to surface mistakes early rather than to bound real designs.
MAX_INCLUDE_DEPTH = 8

# Reserved keys for the `include:` step shape. Any other key on an include step
# is an authoring error.
_INCLUDE_ALLOWED_KEYS = {"include", "when"}

# Allowed top-level keys on a flat-shape Manifest (most common form). Any
# other key triggers a parse-time error with a "did you mean?" hint when the
# unknown key is close to a known one. Catches typos like `site:` (singular)
# or `selctor:` that today silently degrade to "missing field".
_MANIFEST_FLAT_KNOWN_KEYS = {
    "apiVersion",
    "kind",
    "name",
    "description",
    "sites",
    "selector",
    "siteSelector",
    "parallel",
    "parameters",
    "steps",
}

# K8s-style nested envelope. Top-level allows only the four envelope keys.
# `metadata` carries name/description/labels. `spec` carries everything else.
_MANIFEST_NESTED_TOP_KEYS = {"apiVersion", "kind", "metadata", "spec"}
_MANIFEST_NESTED_METADATA_KEYS = {"name", "description", "labels"}
_MANIFEST_NESTED_SPEC_KEYS = _MANIFEST_FLAT_KNOWN_KEYS - {"apiVersion", "kind", "name", "description"}


def _suggest_known_key(unknown: str, known: set[str]) -> str | None:
    """Return a 'did you mean X?' suggestion for a typo if there is a close match."""
    import difflib
    matches = difflib.get_close_matches(unknown, sorted(known), n=1, cutoff=0.7)
    return matches[0] if matches else None


def _validate_known_keys(
    actual: dict, allowed: set[str], path: Path, context: str
) -> None:
    """Reject any keys in `actual` that are not in `allowed`.

    Args:
        actual: The dict whose keys to validate.
        allowed: The closed set of permitted keys.
        path: Source file path, used in the error message.
        context: Where in the manifest this dict lives (e.g. "top-level",
            "spec", "metadata"), used to disambiguate the error.
    """
    unknown = sorted(set(actual.keys()) - allowed)
    if not unknown:
        return
    parts = []
    for key in unknown:
        suggestion = _suggest_known_key(key, allowed)
        if suggestion:
            parts.append(f"`{key}` (did you mean `{suggestion}`?)")
        else:
            parts.append(f"`{key}`")
    raise ValueError(
        f"Manifest '{path}' has unknown {context} key(s): {', '.join(parts)}. "
        f"Allowed: {sorted(allowed)}."
    )


class IncludeError(ValueError):
    """Raised when a manifest `include:` directive cannot be resolved.

    Subclass of ValueError so existing callers that catch ValueError still work.
    """


# Pattern for condition expressions in 'when' clauses
# Supports:
#   - Comparison: site.labels.<key> == 'value' or site.properties.<path> != 'value'
#   - Boolean shorthand: site.properties.<path> (truthy check)
# Values can be quoted strings ('value' or "value") or unquoted booleans (true/false)
CONDITION_PATTERN = re.compile(
    r"\{\{\s*site\.(labels\.[a-zA-Z0-9_-]+|properties\.[a-zA-Z0-9_.\[\]-]+)"
    r"(?:\s*(==|!=)\s*(?:['\"]([^'\"]*?)['\"]|(true|false)))?\s*\}\}"
)

# Supported kubectl operations (extensible for future operations like 'wait', 'delete')
KUBECTL_OPERATIONS = {"apply"}


class NoTargetingError(ValueError):
    """Raised when neither the manifest nor the CLI provides any targeting.

    Distinct from generic `ValueError` so callers can differentiate the
    "generic library manifest with no CLI selector" case from selector
    parse errors. `validate()` treats this as structurally OK and skips
    site-dependent checks. `cmd_deploy` surfaces it as a hard error.
    """


class SelectorParseError(ValueError):
    """Raised when a `-l/--selector` string fails to parse.

    Distinct from generic `ValueError` so `validate()` can attribute
    the failure to selector input (and skip the redundant no-match
    diagnostic) without substring-matching the error message.
    """


def parse_selector(selector: str | None) -> dict[str, list[str]]:
    """Parse a label selector string into key to value-list pairs.

    Within a single selector string, comma-separated `key=value` pairs are
    AND-combined across distinct keys. Duplicate keys follow these rules:

    - The special `name` key may repeat. Repeated values OR-combine and
      duplicates are deduped (preserving first-seen order).
    - Any non-name key may only appear once. Duplicate non-name keys
      raise `SelectorParseError`. This matches kubectl, Terraform, and
      Ansible label-selector grammars where AND across distinct keys is
      the rule.

    Args:
        selector: Comma-separated `key=value` pairs (e.g.,
            `environment=prod,region=eastus`), or None/empty for no
            filtering.

    Returns:
        Dict mapping each key to a list of allowed values. Non-name keys
        always map to a single-element list. The `name` key may map to
        multiple values (OR-combined). Empty dict if `selector` is None
        or empty.

    Raises:
        SelectorParseError: If a non-name key appears more than once.

    Example:
        >>> parse_selector('environment=prod,region=eastus')
        {'environment': ['prod'], 'region': ['eastus']}
        >>> parse_selector('name=a,name=b,name=a')
        {'name': ['a', 'b']}
        >>> parse_selector(None)
        {}
    """
    if not selector:
        return {}

    labels: dict[str, list[str]] = {}
    for part in selector.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise SelectorParseError(
                f"Selector term `{part}` has empty key. Use `key=value` form."
            )
        if not value:
            raise SelectorParseError(
                f"Selector key `{key}` has empty value. Use `{key}=<value>`."
            )
        if key in labels:
            if key != "name":
                raise SelectorParseError(
                    f"Selector key `{key}` may only appear once. Selectors "
                    f"AND across keys, so duplicating a key would always "
                    f"match zero sites. Only `name=` supports multiple "
                    f"values (OR-combined)."
                )
            if value not in labels[key]:
                labels[key].append(value)
        else:
            labels[key] = [value]
    return labels


def _merge_selector_strings(strings: list[str] | None) -> str | None:
    """Merge multiple selector strings into a single comma-separated string.

    Used by the CLI to flatten repeated `-l/--selector` flags into a single
    string before parsing. The grammar is associative under comma joining:
    `parse_selector(",".join(parts))` enforces the same name-OR /
    non-name-error rules across the merged input.
    """
    if not strings:
        return None
    merged = ",".join(s for s in strings if s)
    return merged or None


def _normalize_site_identifier(identifier: str) -> str:
    """Validate and normalize a site identifier or path-form identifier.

    Accepts:
    - Bare basename (`munich-dev`)
    - Forward-slash relative path (`regions/eu/munich-dev`)
    - Backslash relative path (normalized to forward slashes)

    Rejects (raises `ValueError`):
    - Empty string
    - Leading `./`
    - Leading `/` (absolute path)
    - Trailing `/`
    - `..` path segments (path traversal)
    - `.` path segments
    - Empty path segments (e.g., `a//b`)

    Returns the normalized form (forward-slash separators, no leading or
    trailing slash).
    """
    if not identifier:
        raise ValueError("Site identifier must not be empty")
    normalized = identifier.replace("\\", "/")
    if normalized.startswith("./"):
        raise ValueError(
            f"Site identifier '{identifier}' must not start with `./`. "
            f"Use the relative form (e.g., `regions/eu/munich`)."
        )
    if normalized.startswith("/"):
        raise ValueError(
            f"Site identifier '{identifier}' must be relative (no leading `/`)."
        )
    if normalized.endswith("/"):
        raise ValueError(
            f"Site identifier '{identifier}' must not end with `/`."
        )
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        raise ValueError(
            f"Site identifier '{identifier}' must not contain `..` segments."
        )
    if any(p == "." for p in parts):
        raise ValueError(
            f"Site identifier '{identifier}' must not contain `.` segments."
        )
    if any(not p for p in parts):
        raise ValueError(
            f"Site identifier '{identifier}' must not contain empty path segments."
        )
    return normalized


def _validate_resource(data: dict[str, Any], expected_kind: str | list[str], path: Path) -> str:
    """Validate apiVersion and kind for a resource file.

    Args:
        data: Parsed YAML data
        expected_kind: The expected kind(s) (e.g., 'Site' or ['Site', 'SiteTemplate'])
        path: File path for error messages

    Returns:
        The validated apiVersion string

    Raises:
        ValueError: If kind doesn't match expected or apiVersion is unsupported

    Note:
        - apiVersion defaults to 'siteops/v1' if not specified
        - kind is only validated if present; if omitted, the resource type
          is determined by the calling context
    """
    api_version = data.get("apiVersion", DEFAULT_API_VERSION)
    kind = data.get("kind")

    # Normalize expected_kind to a list for consistent handling
    expected_kinds = [expected_kind] if isinstance(expected_kind, str) else list(expected_kind)

    if api_version not in SUPPORTED_API_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_API_VERSIONS))
        raise ValueError(f"Unsupported apiVersion '{api_version}' in {path}. Supported: {supported}")

    if kind is not None and kind not in expected_kinds:
        if len(expected_kinds) == 1:
            raise ValueError(f"Invalid kind '{kind}' in {path}. Expected '{expected_kinds[0]}'")
        else:
            expected_str = ", ".join(f"'{k}'" for k in expected_kinds)
            raise ValueError(f"Invalid kind '{kind}' in {path}. Expected one of: {expected_str}")

    return api_version


@dataclass(frozen=True)
class ParallelConfig:
    """Configuration for parallel site execution.

    Controls how many sites are deployed concurrently during manifest execution.

    Attributes:
        sites: Maximum concurrent sites.
            - 0 means unlimited (all sites run concurrently)
            - 1 means sequential (one site at a time)
            - N means at most N sites run concurrently

    Examples:
        >>> ParallelConfig.from_value(3)
        ParallelConfig(sites=3)
        >>> ParallelConfig.from_value(True)
        ParallelConfig(sites=0)
        >>> ParallelConfig.from_value({"sites": 2})
        ParallelConfig(sites=2)
    """

    sites: int = 1

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.sites < 0:
            raise ValueError(f"parallel.sites must be >= 0, got {self.sites}")

    @classmethod
    def from_value(cls, value: Any) -> "ParallelConfig":
        """Parse parallel config from a manifest value.

        Args:
            value: One of:
                - None: Returns default (sequential)
                - bool: True = unlimited, False = sequential
                - int: Max concurrent sites (0 = unlimited)
                - dict: Object form with 'sites' key

        Returns:
            Configured ParallelConfig instance

        Raises:
            ValueError: If value is invalid type or out of range

        Examples:
            parallel: 3           -> ParallelConfig(sites=3)
            parallel: 0           -> ParallelConfig(sites=0)  # unlimited
            parallel: true        -> ParallelConfig(sites=0)  # unlimited
            parallel: false       -> ParallelConfig(sites=1)  # sequential
            parallel:
              sites: 3            -> ParallelConfig(sites=3)
        """
        if value is None:
            return cls()

        if isinstance(value, bool):
            return cls(sites=0 if value else 1)

        if isinstance(value, int):
            return cls(sites=value)

        if isinstance(value, dict):
            sites = value.get("sites", 1)
            if not isinstance(sites, int):
                raise ValueError(f"parallel.sites must be an integer, got {type(sites).__name__}")
            return cls(sites=sites)

        raise ValueError(f"Invalid parallel value: expected bool, int, or dict, " f"got {type(value).__name__}")

    @property
    def is_sequential(self) -> bool:
        """Return True if deployment runs one site at a time."""
        return self.sites == 1

    @property
    def is_unlimited(self) -> bool:
        """Return True if all sites run concurrently."""
        return self.sites == 0

    @property
    def max_workers(self) -> int | None:
        """Return max workers for ThreadPoolExecutor, or None for unlimited."""
        return None if self.sites == 0 else self.sites

    def __str__(self) -> str:
        """Return human-readable description."""
        if self.is_unlimited:
            return "unlimited"
        if self.is_sequential:
            return "sequential"
        return f"max {self.sites}"


@dataclass
class Site:
    """Deployment target representing an Azure subscription/resource group.

    Attributes:
        name: Unique identifier for the site
        subscription: Azure subscription ID
        resource_group: Azure resource group name
        location: Azure region (e.g., 'eastus', 'westus2')
        labels: Key-value string pairs for filtering with selectors
        properties: Structured data for complex site-specific configuration
        parameters: Default parameters to include in all deployments to this site
    """

    name: str
    subscription: str
    resource_group: str
    location: str
    labels: dict[str, str] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)

    def matches_selector(self, selector: dict[str, list[str]]) -> bool:
        """Check if site matches all selector criteria.

        Supports:
        - `name`: site name must be one of the listed values (OR-combined)
        - any other `<label>`: site label value must equal the single
          listed value

        Args:
            selector: Dict mapping each key to a list of allowed values.
                Non-name keys must map to a single-element list (enforced
                by `parse_selector`).

        Returns:
            True if all selector criteria match.
        """
        for key, values in selector.items():
            if key == "name":
                if self.name not in values:
                    return False
            else:
                # Non-name keys carry a single value (enforced upstream).
                # Use list containment so a malformed multi-value list still
                # produces deterministic match behavior.
                if self.labels.get(key) not in values:
                    return False
        return True

    @classmethod
    def from_file(cls, path: Path) -> "Site":
        """Load a site from a YAML file.

        Supports two formats:
        1. Flat format (recommended):
            ```yaml
            apiVersion: siteops/v1
            kind: Site
            name: dev-eastus
            subscription: "..."
            resourceGroup: "..."
            location: eastus
            labels:
              environment: dev
            properties:
              deviceEndpoints:
                - host: 10.0.1.100
                  port: 4840
            ```

        2. K8s-style nested format:
            ```yaml
            apiVersion: siteops/v1
            kind: Site
            metadata:
              name: dev-eastus
              labels:
                environment: dev
            spec:
              subscription: "..."
              resourceGroup: "..."
              location: eastus
              properties:
                deviceEndpoints:
                  - host: 10.0.1.100
                    port: 4840
            ```

        Args:
            path: Path to the YAML file

        Returns:
            Site instance

        Raises:
            ValueError: If file is empty, invalid, or missing required fields

        Note:
            This is a low-level loader. It does NOT apply `inherits:` chains
            or overlays from `sites.local/` / extras dirs. Use
            `Orchestrator.load_site(name)` for fully-resolved sites.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty or invalid YAML file: {path}")

        _validate_resource(data, "Site", path)

        if "spec" in data:
            spec = data["spec"]
            metadata = data.get("metadata", {})
            name = metadata.get("name", path.stem)
            labels = metadata.get("labels", {})
        else:
            spec = data
            name = data.get("name", path.stem)
            labels = data.get("labels", {})

        required = ["subscription", "location"]
        for req in required:
            if req not in spec:
                raise ValueError(f"Missing required field '{req}' in site: {path}")

        return cls(
            name=name,
            subscription=spec["subscription"],
            resource_group=spec.get("resourceGroup", ""),
            location=spec["location"],
            labels=labels,
            properties=spec.get("properties", {}),
            parameters=spec.get("parameters", {}),
        )

    @property
    def is_subscription_level(self) -> bool:
        """Check if this is a subscription-level site (no resource group).

        Subscription-level sites are used for deploying shared resources
        once per subscription (e.g., Azure Edge Sites). They have only
        subscription + location, no resourceGroup.

        Returns:
            True if site has no resource_group (subscription-level)
            False if site has a resource_group (RG-level)
        """
        return not self.resource_group

    def get_all_parameters(self) -> dict[str, Any]:
        """Get a copy of site-level parameters.

        Returns:
            Copy of the parameters dict (modifications won't affect the site)
        """
        return dict(self.parameters)

    def __repr__(self) -> str:
        return f"Site(name={self.name!r}, location={self.location!r})"


@dataclass
class DeploymentStep:
    """A single Bicep/ARM deployment step within a manifest.

    Attributes:
        name: Unique name for the step (used in deployment names and output references)
        template: Path to the Bicep/ARM template file (relative to workspace)
        parameters: List of parameter file paths (relative to workspace)
        scope: Deployment scope - 'resourceGroup' or 'subscription'
        when: Optional condition expression (e.g., "{{ site.labels.X == 'Y' }}")
    """

    name: str
    template: str
    parameters: list[str] = field(default_factory=list)
    scope: str = "resourceGroup"
    when: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{self.scope}'. Must be one of: {VALID_SCOPES}")

        if self.when and not CONDITION_PATTERN.fullmatch(self.when.strip()):
            raise ValueError(
                f"Invalid 'when' condition syntax: {self.when}. "
                "Expected: {{ site.labels.X == 'value' }}, {{ site.properties.path == true }}, "
                "or {{ site.properties.path }} (truthy check)"
            )


@dataclass
class ArcCluster:
    """Arc-connected Kubernetes cluster configuration.

    Attributes:
        name: Cluster name (supports template variables like {{ site.labels.clusterName }})
        resource_group: Resource group containing the cluster (supports template variables)
    """

    name: str
    resource_group: str


@dataclass
class KubectlStep:
    """A kubectl operation step within a manifest.

    Executes kubectl commands against an Arc-connected Kubernetes cluster.
    Site Ops automatically manages the `az connectedk8s proxy` lifecycle.

    Attributes:
        name: Unique name for the step
        operation: Kubectl operation ('apply' is currently supported)
        arc: Arc cluster configuration (name and resourceGroup)
        files: List of file paths (relative to workspace) or HTTPS URLs to apply
        when: Optional condition expression (e.g., "{{ site.labels.X == 'Y' }}")

    Example manifest usage:
        ```yaml
        - name: apply-config
          type: kubectl
          operation: apply
          arc:
            name: "{{ site.labels.clusterName }}"
            resourceGroup: "{{ site.resourceGroup }}"
          files:
            - https://example.com/manifest.yaml
            - configs/local-config.yaml
          when: "{{ site.labels.enableConfig == 'true' }}"
        ```
    """

    name: str
    operation: str
    arc: ArcCluster
    files: list[str] = field(default_factory=list)
    when: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in KUBECTL_OPERATIONS:
            raise ValueError(
                f"Invalid kubectl operation '{self.operation}'. " f"Supported: {', '.join(sorted(KUBECTL_OPERATIONS))}"
            )

        if not self.files:
            raise ValueError(f"KubectlStep '{self.name}' must specify at least one file")

        if self.when and not CONDITION_PATTERN.fullmatch(self.when.strip()):
            raise ValueError(
                f"Invalid 'when' condition syntax: {self.when}. "
                "Expected: {{ site.labels.X == 'value' }}, {{ site.properties.path == true }}, "
                "or {{ site.properties.path }} (truthy check)"
            )


# Union type for manifest steps - allows type checking to distinguish step types
ManifestStep = DeploymentStep | KubectlStep


@dataclass
class Manifest:
    """Deployment manifest that orchestrates templates across sites.

    A manifest defines:
    - Which sites to deploy to (explicit list or label selector)
    - What steps to execute (Bicep/ARM deployments or kubectl operations)
    - The order of deployment (steps execute sequentially per site)
    - Whether to deploy to sites in parallel
    - Shared parameters applied to all steps (with auto-filtering)

    Attributes:
        name: Unique identifier for the manifest
        description: Human-readable description
        sites: Explicit list of site names to deploy to
        steps: Ordered list of steps (DeploymentStep or KubectlStep)
        site_selector: Label selector string (e.g., 'environment=prod')
        parallel: Parallelization config (int, bool, or object with 'sites' key)
        parameters: Manifest-level parameter files applied to all steps

    Parallel Configuration:
        - parallel: 0           # Unlimited concurrency (all sites at once)
        - parallel: 1           # Sequential (one site at a time, default)
        - parallel: 3           # Max 3 sites concurrently
        - parallel: true        # Unlimited concurrency
        - parallel: false       # Sequential
        - parallel:
            sites: 3            # Object form, max 3 sites concurrently
    """

    name: str
    description: str
    sites: list[str]
    steps: list[ManifestStep]
    site_selector: str | None = None
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    parameters: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path, *, workspace_root: Path) -> "Manifest":
        """Load a manifest from a YAML file.

        Resolves any `- include: <path>` steps recursively, splicing the
        included manifests' steps into this one's step list at the include's
        position. See docs/manifest-includes.md for the full include contract.

        Example manifest:
            ```yaml
            apiVersion: siteops/v1
            kind: Manifest
            name: iot-operations
            description: Deploy Azure IoT Operations
            parallel: 2  # Max 2 sites concurrently

            sites:
              - dev-eastus

            steps:
              - name: aio-enablement
                template: templates/enablement.bicep
                scope: subscription
                parameters:
                  - parameters/enablement.yaml

              - name: configure-cluster
                type: kubectl
                operation: apply
                arc:
                  name: "{{ site.labels.clusterName }}"
                  resourceGroup: "{{ site.resourceGroup }}"
                files:
                  - https://example.com/config.yaml
                when: "{{ site.labels.enableConfig == 'true' }}"
            ```

        Args:
            path: Path to the YAML file.
            workspace_root: Workspace root directory. Required, keyword-only.
                Used as the anti-traversal boundary when resolving any
                `include:` step paths and to scope all workspace-relative
                references. In production this is `Orchestrator.workspace`;
                in tests, pass the workspace fixture (or `manifest_path.parent`
                for a self-contained synthetic manifest).

        Returns:
            Manifest instance with all includes resolved into a flat step list.

        Raises:
            ValueError: If file is empty, invalid, or steps are misconfigured.
            IncludeError: If an include cycles, exceeds depth, escapes the
                workspace root, names a missing or non-Manifest file,
                conflicts with a step's own `when:`, or contributes zero steps.
        """
        path = Path(path)
        root = Path(workspace_root)

        spec, name, description = _read_manifest_spec(path)

        sites = []
        for item in spec.get("sites", []):
            if isinstance(item, str):
                try:
                    sites.append(_normalize_site_identifier(item))
                except ValueError as e:
                    raise ValueError(
                        f"Invalid site identifier in `{path}` `sites:` list: {e}"
                    ) from e

        # `selector:` is the preferred manifest field. `siteSelector:` is
        # accepted for backward compatibility but logs a one-time deprecation
        # notice per file. Both refer to the same label expression.
        if "selector" in spec and "siteSelector" in spec:
            raise ValueError(
                f"Manifest '{path}' declares both `selector:` and "
                f"`siteSelector:`. Use `selector:` only."
            )
        if "siteSelector" in spec:
            import logging as _logging
            _logging.getLogger("siteops.models").warning(
                "%s uses deprecated `siteSelector:`. Rename to `selector:`.",
                path,
            )
            site_selector = spec.get("siteSelector")
        else:
            site_selector = spec.get("selector")
        parallel = ParallelConfig.from_value(spec.get("parallel"))

        # Recursive include resolution. The recursion stack tracks the current
        # DFS path so a fragment shared by two siblings is not flagged as a
        # cycle. The include chain captures the full provenance for diagnostics.
        steps, parameters = _resolve_steps_and_params(
            spec=spec,
            manifest_path=path,
            workspace_root=root.resolve(),
            recursion_stack=[path.resolve()],
            include_chain=[path],
            depth=0,
        )

        _validate_no_step_name_collisions(steps)

        return cls(
            name=name,
            description=description,
            sites=sites,
            steps=steps,
            site_selector=site_selector,
            parallel=parallel,
            parameters=parameters,
        )

    def resolve_parameter_path(self, param_path: str, site: "Site") -> str:
        """Resolve template variables in a parameter file path.

        Supports:
        - {{ site.name }} - Site name
        - {{ site.location }} - Site location
        - {{ site.resourceGroup }} - Site resource group
        - {{ site.subscription }} - Site subscription
        - {{ site.labels.<key> }} - Site label value
        - {{ site.properties.<path> }} - Site property value (nested paths supported)

        Args:
            param_path: Parameter file path with optional template variables
            site: Site to resolve variables from

        Returns:
            Resolved path string
        """
        result = param_path
        result = result.replace("{{ site.name }}", site.name)
        result = result.replace("{{ site.location }}", site.location)
        result = result.replace("{{ site.resourceGroup }}", site.resource_group)
        result = result.replace("{{ site.subscription }}", site.subscription)

        for key, value in site.labels.items():
            result = result.replace(f"{{{{ site.labels.{key} }}}}", value)

        # Resolve {{ site.properties.<path> }} templates
        for match in re.finditer(r"\{\{\s*site\.properties\.(\S+?)\s*\}\}", result):
            prop_path = match.group(1)
            value = site.properties
            for part in prop_path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    value = None
                    break
            if value is not None:
                result = result.replace(match.group(0), str(value))

        return result


# ---------------------------------------------------------------------------
# Manifest loading helpers (include resolution, step parsing)
# ---------------------------------------------------------------------------


def _read_manifest_spec(path: Path) -> tuple[dict[str, Any], str, str]:
    """Read a manifest YAML file and return (spec, name, description).

    Validates apiVersion + kind, rejects unknown top-level keys with a
    "did you mean?" hint, and unwraps the K8s-style `spec:` envelope when
    present. Raises ValueError on empty files, wrong kind, or unknown
    top-level keys.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"Empty or invalid YAML file: {path}")

    _validate_resource(data, "Manifest", path)

    if "spec" in data:
        _validate_known_keys(data, _MANIFEST_NESTED_TOP_KEYS, path, "top-level")
        metadata = data.get("metadata", {}) or {}
        if metadata:
            _validate_known_keys(metadata, _MANIFEST_NESTED_METADATA_KEYS, path, "metadata")
        spec = data["spec"] or {}
        if isinstance(spec, dict):
            _validate_known_keys(spec, _MANIFEST_NESTED_SPEC_KEYS, path, "spec")
        name = metadata.get("name", path.stem)
        description = metadata.get("description", "")
    else:
        _validate_known_keys(data, _MANIFEST_FLAT_KNOWN_KEYS, path, "top-level")
        spec = data
        name = data.get("name", path.stem)
        description = data.get("description", "")

    return spec, name, description


def _is_include_step(step_data: dict[str, Any]) -> bool:
    return isinstance(step_data, dict) and "include" in step_data


def _format_include_chain(chain: list[Path]) -> str:
    return " -> ".join(str(p) for p in chain)


def _validate_include_step(step_data: dict[str, Any], parent_path: Path, index: int) -> str:
    """Validate an `include:` step shape and return the path string."""
    extra = set(step_data.keys()) - _INCLUDE_ALLOWED_KEYS
    if extra:
        raise IncludeError(
            f"Step {index + 1} in '{parent_path}' has unexpected keys alongside "
            f"`include:`: {sorted(extra)}. Only `include` and `when` are allowed."
        )
    target = step_data.get("include")
    if not isinstance(target, str) or not target.strip():
        raise IncludeError(
            f"Step {index + 1} in '{parent_path}' must provide a non-empty "
            f"string path for `include`."
        )
    return target


def _resolve_include_path(raw: str, parent_path: Path, workspace_root: Path) -> Path:
    """Resolve a relative include path under the workspace root.

    The resolved absolute path must be a descendant of workspace_root.
    Site-driven (Mustache) include paths are not supported in v1 and will
    fail the workspace-root check or the file-exists check.
    """
    candidate = (parent_path.parent / raw).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        raise IncludeError(
            f"Include path '{raw}' in '{parent_path}' resolves outside the "
            f"workspace root '{workspace_root}'."
        ) from None
    if not candidate.exists():
        raise IncludeError(
            f"Include path '{raw}' in '{parent_path}' does not exist "
            f"(resolved to '{candidate}')."
        )
    return candidate


def _propagate_when(step: "ManifestStep", include_when: str | None, source: Path) -> None:
    """Apply an include's `when:` to a spliced step.

    Raises IncludeError if the step already has its own `when:`. Combining
    two expressions is not supported in v1.
    """
    if include_when is None:
        return
    if step.when:
        raise IncludeError(
            f"Step '{step.name}' from '{source}' already has a `when:` "
            f"and the parent include also sets one. Consolidate into a "
            f"single condition on either the include or the step."
        )
    step.when = include_when


def _parse_inline_step(step_data: dict[str, Any], source_path: Path, index: int) -> "ManifestStep":
    """Parse a single non-include step into a DeploymentStep or KubectlStep."""
    if "name" not in step_data:
        raise ValueError(f"Step {index + 1} missing required field 'name' in manifest: {source_path}")

    step_type = step_data.get("type", "deployment")

    if step_type == "kubectl":
        if "operation" not in step_data:
            raise ValueError(
                f"Step '{step_data['name']}' (type: kubectl) missing 'operation' in manifest: {source_path}"
            )
        if "arc" not in step_data:
            raise ValueError(
                f"Step '{step_data['name']}' (type: kubectl) missing 'arc' configuration in manifest: {source_path}"
            )
        arc_data = step_data["arc"]
        if "name" not in arc_data or "resourceGroup" not in arc_data:
            raise ValueError(
                f"Step '{step_data['name']}' arc config must have 'name' and 'resourceGroup': {source_path}"
            )
        if "files" not in step_data or not step_data["files"]:
            raise ValueError(
                f"Step '{step_data['name']}' (type: kubectl) missing 'files' in manifest: {source_path}"
            )
        return KubectlStep(
            name=step_data["name"],
            operation=step_data["operation"],
            arc=ArcCluster(
                name=arc_data["name"],
                resource_group=arc_data["resourceGroup"],
            ),
            files=step_data["files"],
            when=step_data.get("when"),
        )

    if "template" not in step_data:
        raise ValueError(f"Step '{step_data['name']}' missing 'template' in manifest: {source_path}")
    return DeploymentStep(
        name=step_data["name"],
        template=step_data["template"],
        parameters=step_data.get("parameters", []),
        scope=step_data.get("scope", "resourceGroup"),
        when=step_data.get("when"),
    )


def _merge_parameters(parent: list[str], fragment: list[str]) -> list[str]:
    """Append fragment parameters after parent's, deduplicating by raw path.

    Parent wins on duplicate paths. Comparison is on the normalized POSIX
    string of the raw path, not on the resolved-with-Mustache path.
    """
    seen = {Path(p).as_posix() for p in parent}
    merged = list(parent)
    for p in fragment:
        key = Path(p).as_posix()
        if key not in seen:
            merged.append(p)
            seen.add(key)
    return merged


def _resolve_steps_and_params(
    spec: dict[str, Any],
    manifest_path: Path,
    workspace_root: Path,
    recursion_stack: list[Path],
    include_chain: list[Path],
    depth: int,
) -> tuple[list["ManifestStep"], list[str]]:
    """Recursively resolve `include:` steps into a flat (steps, parameters) pair.

    Args:
        spec: The current manifest's parsed `spec` dict (i.e., the body
            holding `steps:` and `parameters:`).
        manifest_path: Path of the manifest whose spec is being processed.
        workspace_root: Resolved absolute workspace root for traversal checks.
        recursion_stack: Resolved paths of manifests on the current DFS path.
            Used for cycle detection (NOT a global visited set).
        include_chain: Human-readable include chain for diagnostics.
        depth: Current recursion depth, capped by MAX_INCLUDE_DEPTH.
    """
    if depth > MAX_INCLUDE_DEPTH:
        raise IncludeError(
            f"Include depth exceeded {MAX_INCLUDE_DEPTH} levels at "
            f"{_format_include_chain(include_chain)}."
        )

    steps: list[ManifestStep] = []
    parameters: list[str] = list(spec.get("parameters", []))

    raw_steps = spec.get("steps") or []
    for index, step_data in enumerate(raw_steps):
        if not isinstance(step_data, dict):
            raise ValueError(
                f"Step {index + 1} in '{manifest_path}' is not a mapping."
            )

        if not _is_include_step(step_data):
            steps.append(_parse_inline_step(step_data, manifest_path, index))
            continue

        raw_target = _validate_include_step(step_data, manifest_path, index)
        include_when = step_data.get("when")

        target_path = _resolve_include_path(raw_target, manifest_path, workspace_root)

        if target_path in recursion_stack:
            cycle = include_chain + [target_path]
            raise IncludeError(
                f"Include cycle detected: {_format_include_chain(cycle)}."
            )

        try:
            sub_spec, _, _ = _read_manifest_spec(target_path)
        except ValueError as exc:
            raise IncludeError(
                f"Include '{raw_target}' in '{manifest_path}' could not be loaded as a Manifest: {exc}"
            ) from exc

        sub_steps, sub_params = _resolve_steps_and_params(
            spec=sub_spec,
            manifest_path=target_path,
            workspace_root=workspace_root,
            recursion_stack=recursion_stack + [target_path],
            include_chain=include_chain + [target_path],
            depth=depth + 1,
        )

        # Manifest-level parameters merge unconditionally into every parent
        # step. A gated include that contributes parameters would silently
        # affect ungated parent steps. Check sub_params (post-recursion) so a
        # fragment that only includes another fragment with parameters is
        # still caught.
        if include_when and sub_params:
            raise IncludeError(
                f"Include '{raw_target}' in '{manifest_path}' has a `when:` "
                f"but its include subtree contributes manifest-level "
                f"`parameters:`. Drop the `when:` or move the parameters onto "
                f"individual fragment steps."
            )

        if not sub_steps:
            raise IncludeError(
                f"Include '{raw_target}' in '{manifest_path}' contributed "
                f"zero steps. An include must define at least one step."
            )

        for sub_step in sub_steps:
            _propagate_when(sub_step, include_when, target_path)

        steps.extend(sub_steps)
        parameters = _merge_parameters(parameters, sub_params)

    return steps, parameters


def _validate_no_step_name_collisions(steps: list["ManifestStep"]) -> None:
    """Reject duplicate step names in the post-flatten step list."""
    seen: set[str] = set()
    for step in steps:
        if step.name in seen:
            raise ValueError(
                f"Duplicate step name '{step.name}' after include flattening. "
                f"Step names must be unique across the entire flattened "
                f"pipeline (parent steps and all included fragments)."
            )
        seen.add(step.name)
