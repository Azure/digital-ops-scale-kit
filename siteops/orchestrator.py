# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Workspace resolution, composition, planning, and execution.

This module provides the Orchestrator class which handles:
- Loading sites and manifests from the workspace
- Resolving and composing parameters with template variable substitution
- Executing Bicep/ARM, kubectl, and wait steps across sites
- Parallel and sequential deployment modes with configurable concurrency
"""

import copy
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from siteops import yamlio
from siteops.composition import (
    CompositionContract,
    CompositionError,
    CompositionResult,
    LoadedParameterSource,
    compose_sources,
    contains_composition_metadata,
    load_contract,
    merge_contracts,
    report_composition_error,
)
from siteops.executor import (
    AzCliExecutor,
    DeploymentResult,
    KubectlResult,
    WaitResult,
    get_template_parameters,
)
from siteops.models import (
    CONDITION_PATTERN,
    AnyCondition,
    ArmTagCondition,
    DeploymentStep,
    KubectlStep,
    Manifest,
    ManifestStep,
    MultipleSubscriptionSitesError,
    NoTargetingError,
    ParallelConfig,
    ParameterSelectionError,
    ParameterSource,
    SelectorParseError,
    Site,
    WaitStep,
    _normalize_site_identifier,
    _validate_resource,
    format_when_condition,
    parse_selector,
)
from siteops.planning import (
    STEP_OUTPUT_PATTERN,
    ArmTagWaitOperation,
    CompositionReference,
    CompositionRequirement,
    CompositionResource,
    CompositionSource,
    DataReference,
    DeploymentOperation,
    DeploymentPlan,
    DiagnosticSeverity,
    InputStatus,
    KubectlOperation,
    LiteralValue,
    MappingValue,
    OperationIdentity,
    OperationKind,
    OperationScope,
    OutputValue,
    PlanBuildResult,
    PlanComposition,
    PlanDiagnostic,
    PlanDisposition,
    PlanExecutionMode,
    PlanIntent,
    PlanNotExecutableError,
    PlanSkipReason,
    PlanStatus,
    PlanStep,
    PlanValueResolutionError,
    PreparedOperation,
    PreparedTarget,
    ResourceDisposition,
    ResourceIdentity,
    SkipReasonCode,
    TargetKind,
    classify_plan_value,
    collect_data_references,
    render_plain_plan,
    resolve_plan_value,
)
from siteops.sanitize import (
    is_redaction_enabled,
    report_parameter_selection_error,
    report_site_load_error,
    scrub_for_output,
    scrub_site_for_output,
    site_name_for_output,
)

logger = logging.getLogger(__name__)


def _freeze_cache_state(value: Any) -> Any:
    """Convert nested runtime state into a deterministic, type-aware value."""
    if is_dataclass(value) and not isinstance(value, type):
        return (
            "dataclass",
            f"{type(value).__module__}.{type(value).__qualname__}",
            tuple(
                (item.name, _freeze_cache_state(getattr(value, item.name)))
                for item in fields(value)
            ),
        )
    if isinstance(value, dict):
        items = [
            (_freeze_cache_state(key), _freeze_cache_state(item))
            for key, item in value.items()
        ]
        return (
            "mapping",
            tuple(sorted(items, key=lambda item: repr(item[0]))),
        )
    if isinstance(value, list):
        return ("list", tuple(_freeze_cache_state(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_freeze_cache_state(item) for item in value))
    if isinstance(value, (set, frozenset)):
        frozen = [_freeze_cache_state(item) for item in value]
        return ("set", tuple(sorted(frozen, key=repr)))
    if isinstance(value, Path):
        return ("path", str(value))
    if value is None or isinstance(value, (bool, int, float, str)):
        return (type(value).__name__, value)
    return (
        "object",
        f"{type(value).__module__}.{type(value).__qualname__}",
        repr(value),
    )


def _cache_state_digest(value: Any) -> str:
    frozen = repr(_freeze_cache_state(value)).encode("utf-8")
    return hashlib.sha256(frozen).hexdigest()


# A template whose closing delimiter is intact but whose opening one is not,
# such as `{ site.name }}`. Anchored on a template path so that data rendered
# into a string, which ends in `}}` whenever it nests, is not mistaken for one.
# The path is a run of anything that is not a brace, rather than a list of the
# characters a path may contain, so a name using a character nobody enumerated
# is still caught. A hyphenated step name is the common one.
#
# The lookbehind is what makes this mean "malformed". Without it the single
# brace matches the second brace of a well-formed `{{ ... }}`, so every correct
# template reads as a mistyped one.
_MALFORMED_TEMPLATE_PATTERN = re.compile(r"(?<!\{)\{\s*(?:site|steps)\.[^{}]*\}\}")

# Pattern for {{ site.properties.<path> }}
# Supports nested paths and array indices like: site.properties.endpoints[0].host
SITE_PROPERTIES_PATTERN = re.compile(r"\{\{\s*site\.properties\.([a-zA-Z0-9_.\[\]]+)\s*\}\}")

# Pattern for {{ site.parameters.<path> }}
# Supports nested paths like: site.parameters.brokerConfig.memoryProfile
SITE_PARAMETERS_PATTERN = re.compile(r"\{\{\s*site\.parameters\.([a-zA-Z0-9_.\[\]]+)\s*\}\}")
FOR_EACH_SITE_PROPERTY_PATTERN = re.compile(
    r"^\{\{\s*site\.properties\.([a-zA-Z0-9_.\[\]-]+)\s*\}\}$"
)

# Result type that can be a deployment, kubectl, or wait result
StepResult = DeploymentResult | KubectlResult | WaitResult

def _normalize_null_site_mappings(data: dict[str, Any]) -> dict[str, Any]:
    """Treat an explicitly empty site mapping as an empty mapping before merge."""
    result = copy.deepcopy(data)
    if "spec" in result and isinstance(result.get("spec"), dict):
        spec = result["spec"]
        for key in ("properties", "parameters"):
            if key in spec and spec[key] is None:
                spec[key] = {}
        metadata = result.get("metadata")
        if isinstance(metadata, dict) and metadata.get("labels") is None:
            metadata["labels"] = {}
        return result

    for key in ("labels", "properties", "parameters"):
        if key in result and result[key] is None:
            result[key] = {}
    return result


def _resolve_parameter_mapping(
    original: dict[Any, Any],
    resolve: Callable[[Any], Any],
) -> dict[Any, Any]:
    """Resolve a parameter mapping's names as well as its values.

    Resolution mapped `{k: resolve(v)}`, so a name kept its braces and reached
    ARM as literal text. The fail-closed guard walked values only, so it missed
    the same class, leaving the one check meant to stop an unresolved template
    unable to see it.

    Two cases are rejected rather than resolved. A name that resolves to a
    whole object or list cannot be a name, and two names that resolve to the
    same string would silently drop a value the operator wrote.

    Args:
        original: The mapping to resolve.
        resolve: Applied to each name and each value.

    Returns:
        A new mapping with both halves resolved.

    Raises:
        ValueError: A name resolved to a non-string, or two names collided.
    """
    resolved: dict[Any, Any] = {}
    origins: dict[Any, Any] = {}

    for key, value in original.items():
        new_key = key
        if isinstance(key, str) and "{{" in key:
            new_key = resolve(key)
            if not isinstance(new_key, str):
                raise ValueError(
                    f"Parameter name '{key}' resolved to "
                    f"{type(new_key).__name__}, which cannot be a name. A name "
                    f"template must resolve to a single value."
                )

        if new_key in resolved:
            # The resolved name is a site value, so it can be an address, a
            # resource group, or anything else the operator put in the site.
            # The two templates that produced it say the same thing to the
            # person fixing it, without publishing the value.
            raise ValueError(
                f"Parameter names '{origins[new_key]}' and '{key}' both "
                f"resolve to the same name. Rename one, since keeping either "
                f"would drop the other."
            )

        origins[new_key] = key
        resolved[new_key] = resolve(value)

    return resolved


def _carries_template(text: str) -> bool:
    """True when a string still carries a template delimiter.

    An opening delimiter always counts, so `{{ site.name }` is caught even
    though its closing brace is mistyped. A token like that resolves to
    nothing, matches no declared parameter, and would otherwise be filtered out
    and deploy defaults while reporting success.

    A closing delimiter counts only alongside a template path, since data
    rendered into a string ends in `}}` whenever it nests and reporting that
    would block a deployment the operator wrote correctly.
    """
    if "{{" in text:
        return True
    return bool(_MALFORMED_TEMPLATE_PATTERN.search(text))


def _reportable_subscription(sub_id: str) -> str:
    """Return a subscription id short enough to name without identifying a tenant.

    Truncated before scrubbing, so the prefix survives redaction. That keeps two
    subscriptions distinguishable in a message that names both, which a full
    placeholder would not.
    """
    return scrub_for_output(f"{sub_id[:8]}...") or ""


# Lock for thread-safe console output
_print_lock = threading.Lock()


def _thread_safe_print(*args: Any, **kwargs: Any) -> None:
    """Print with lock to avoid interleaved output from multiple threads."""
    with _print_lock:
        print(*args, **kwargs)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dict from JSON pairs, refusing a key written twice.

    JSON keeps the last of a repeated key, exactly as YAML does, so a parameter
    file that sets one twice deploys only the second value and discards the
    first in silence. The YAML loader rejects that, and a parameter file should
    not behave differently for being written in JSON.

    Raises:
        ValueError: A key appeared more than once.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(
                f"Duplicate key '{key}' in a JSON parameter file. JSON keeps the "
                f"last one, so the first is discarded. Merge them or rename one."
            )
        seen[key] = value
    return seen


class Orchestrator:
    """Resolve, validate, plan, and execute manifests across sites.

    The orchestrator is responsible for:
    - Loading and caching sites from the workspace
    - Resolving manifest steps, parameter composition, and template variables
    - Executing deployment, kubectl, and wait steps
    - Managing parallel deployment to multiple sites with configurable concurrency

    Attributes:
        workspace: Path to the Site Ops workspace directory
        dry_run: If True, commands are logged but not executed
        executor: The AzCliExecutor instance for running commands
    """

    def __init__(
        self,
        workspace: Path,
        dry_run: bool = False,
        extra_trusted_sites_dirs: list[Path] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.dry_run = dry_run
        self.executor = AzCliExecutor(workspace=self.workspace, dry_run=dry_run)
        self._params_cache: dict[Path, dict[str, Any]] = {}
        self._params_cache_lock = threading.Lock()
        self._composition_cache: dict[
            tuple[str, str, str, str],
            tuple[
                dict[str, Any],
                CompositionResult | None,
                CompositionContract | None,
            ],
        ] = {}
        self._composition_cache_lock = threading.Lock()
        self._site_cache: dict[str, Site] = {}
        self._cache_lock = threading.Lock()
        # Lazy site indexes built on first lookup. Workspace-load
        # invariants enforced during build (see `_build_site_indexes`).
        # - basename_index: `munich-dev` to abs path. Unique
        #   workspace-wide so `-l name=munich-dev` resolves unambiguously
        #   under nested `sites/` subdirectories.
        # - rel_path_index: `regions/eu/munich-dev` to abs path. Used
        #   for relative-path lookups (`sites: [regions/eu/munich-dev]`).
        # - internal_name_index: declared `name:` to abs path. Lets a
        #   site resolve by an internal name distinct from its filename.
        self._basename_index: dict[str, Path] | None = None
        self._rel_path_index: dict[str, Path] | None = None
        self._internal_name_index: dict[str, Path] | None = None
        self._internal_name_index_lock = threading.Lock()
        # Memo of `_is_site_template(path)` keyed by resolved path.
        # Avoids 3N+ YAML re-parses across `_get_all_site_names`,
        # `_build_site_indexes`, and per-site `load_site` calls.
        self._template_check_cache: dict[Path, bool] = {}
        # Memo of the deduped site list returned by `load_all_sites`.
        self._all_sites_cache: list[Site] | None = None
        # Sites that failed to load on the last `load_all_sites`, as
        # `(name, reportable_error)`. Empty until that runs.
        self.skipped_sites: list[tuple[str, str]] = []
        # Memo of `_load_inherited_data(path)` keyed by resolved path,
        # used only when no provenance dict is being recorded. With N
        # sites sharing one template, the template would otherwise be
        # parsed N times. Returns are deepcopied to keep callers safe.
        self._inherited_data_cache: dict[Path, dict[str, Any]] = {}
        self._extra_trusted_sites_dirs = self._normalize_extra_sites_dirs(
            extra_trusted_sites_dirs or []
        )

    def _normalize_extra_sites_dirs(self, dirs: list[Path]) -> list[Path]:
        """Validate and deduplicate extra trusted site directories.

        Extra trusted dirs are searched between the workspace's `sites/` and
        `sites.local/` directories, and receive the same trust level as
        `sites/`: site files in them are allowed to declare `inherits`.

        Args:
            dirs: Candidate directories to add to the trusted search path.

        Returns:
            Resolved, deduplicated, order-preserving list.

        Raises:
            FileNotFoundError: If any directory does not exist.
            ValueError: If a directory collides with the workspace's own
                `sites/` or `sites.local/`. A `sites.local/` collision
                is specifically refused because registering it as trusted
                would let overlays inject inheritance, breaking the overlay
                security invariant.
        """
        primary = (self.workspace / "sites").resolve()
        overlay = (self.workspace / "sites.local").resolve()
        result: list[Path] = []
        seen: set[Path] = set()
        for candidate in dirs:
            resolved = Path(candidate).resolve()
            if not resolved.is_dir():
                raise FileNotFoundError(
                    f"Extra trusted site directory not found: {candidate}"
                )
            if resolved == primary:
                raise ValueError(
                    f"Extra site dir '{candidate}' is the workspace's "
                    f"sites/ directory, which is already included by default."
                )
            if resolved == overlay:
                raise ValueError(
                    f"Extra site dir '{candidate}' is the workspace's "
                    f"sites.local/ directory. Registering it as trusted "
                    f"would allow overlays to inject inheritance, so it is "
                    f"refused for security."
                )
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
        return result

    @property
    def _trusted_sites_dirs(self) -> list[Path]:
        """All trusted site directories, in merge order.

        Trusted means: `inherits` is honored in files from these dirs.
        Excludes `sites.local/` (overlay, always strips `inherits`).
        """
        return [self.workspace / "sites", *self._extra_trusted_sites_dirs]

    def _find_trusted_site_file(self, identifier: str) -> Path | None:
        """Return the trusted file path for the named site.

        Resolves `identifier` against three workspace indexes built on
        first call:

        1. Path-form index (`regions/eu/munich-dev`) for explicit
           relative paths under any trusted `sites/` directory.
        2. Basename index (`munich-dev`) for the common shorthand. The
           basename invariant guarantees the basename maps to one file
           workspace-wide.
        3. Internal-name index for sites that declare a `name:` field
           distinct from their filename.

        The eager build catches workspace-wide drift (basename
        collisions, internal-name shadows) on the first lookup, so the
        invariants fire even for commands that only use the basename
        path.

        SiteTemplates are findable via a direct path probe so
        `load_site` can surface a friendly "cannot deploy a template"
        error rather than a generic "not found".

        `sites.local/` is never searched. Sites must live in a
        repository-controlled or operator-vouched trusted location.
        """
        self._ensure_site_indexes()
        # Path-form lookup first. A `/` in the identifier signals an
        # explicit relative path under a trusted `sites/` dir.
        if "/" in identifier or "\\" in identifier:
            try:
                normalized = _normalize_site_identifier(identifier)
            except ValueError:
                return None
            hit = self._rel_path_index.get(normalized)
            if hit is not None:
                return hit
            return self._find_template_path(normalized)
        # Basename lookup. The basename invariant makes this unambiguous.
        if identifier in self._basename_index:
            return self._basename_index[identifier]
        # Internal `name:` fallback.
        hit = self._internal_name_index.get(identifier)
        if hit is not None:
            return hit
        return self._find_template_path(identifier)

    def _find_template_path(self, identifier: str) -> Path | None:
        """Locate a SiteTemplate file matching `identifier`.

        Used by `_find_trusted_site_file` as a fallback so callers can
        surface a clear "this is a SiteTemplate, not deployable" error
        rather than a generic "not found". Walks subdirectories so a
        nested template (e.g., `sites/shared/base.yaml` resolved as
        `base`) gets the friendly error too.
        """
        for sites_dir in self._trusted_sites_dirs:
            if not sites_dir.exists():
                continue
            # Direct path probe (path-form identifier).
            for ext in (".yaml", ".yml"):
                candidate = sites_dir / f"{identifier}{ext}"
                if candidate.exists() and self._is_site_template(candidate):
                    return candidate
            # Recursive basename probe (so nested templates also hit
            # the friendly error path).
            if "/" not in identifier:
                for ext in ("*.yaml", "*.yml"):
                    for path in sorted(sites_dir.rglob(ext)):
                        if path.stem == identifier and self._is_site_template(path):
                            return path
        return None

    def _ensure_site_indexes(self) -> None:
        """Build the trusted-site indexes if they have not been built yet.

        Called by every site-touching entry point so the workspace
        invariants are enforced regardless of which lookup path the
        caller takes.
        """
        with self._internal_name_index_lock:
            if self._internal_name_index is None:
                basename, rel_path, internal = self._build_site_indexes()
                self._basename_index = basename
                self._rel_path_index = rel_path
                self._internal_name_index = internal

    def _iter_trusted_site_files(
        self, include_templates: bool = False
    ) -> Iterator[tuple[Path, Path]]:
        """Yield `(sites_dir, abs_path)` for every Site file under a
        trusted directory, walking subdirectories.

        Skips SiteTemplates (`kind: SiteTemplate`) by default since
        those are inheritance-only and never selectable. Pass
        `include_templates=True` to keep them, useful when callers want
        to surface a friendly error if the operator tries to load one
        directly.
        """
        for sites_dir in self._trusted_sites_dirs:
            if not sites_dir.exists():
                continue
            for ext in ("*.yaml", "*.yml"):
                for path in sorted(sites_dir.rglob(ext)):
                    if not include_templates and self._is_site_template(path):
                        continue
                    yield sites_dir, path

    def _build_site_indexes(self) -> tuple[dict[str, Path], dict[str, Path], dict[str, Path]]:
        """Walk trusted dirs and build the basename, relative-path, and
        internal-name indexes.

        Workspace-load invariants enforced during the build:

        - Within any one trusted directory, every basename is unique
          across all subdirectories. Lets `-l name=munich-dev` resolve
          unambiguously when nested layouts are used.
        - Across trusted directories, basename collisions are
          legitimate overlays only when the relative path also matches.
          Cross-directory collisions where the relative path differs
          would create two distinct logical sites sharing one identifier
          and are rejected.
        - No internal `name:` collides with another file's basename.
        - No internal `name:` collides with another file's relative path
          (the path-form identifier).
        - No two sites declare the same internal `name:`.

        Returns:
            `(basename_index, rel_path_index, internal_name_index)`.
        """
        basename_to_path: dict[str, Path] = {}
        rel_path_to_path: dict[str, Path] = {}

        # Group files by their owning trusted directory so the within-dir
        # uniqueness check does not flag legitimate cross-dir overlays.
        per_dir: dict[Path, list[Path]] = {}
        for sites_dir, path in self._iter_trusted_site_files():
            per_dir.setdefault(sites_dir, []).append(path)

        for sites_dir, paths in per_dir.items():
            dir_basenames: dict[str, Path] = {}
            for path in paths:
                rel_path = path.relative_to(sites_dir).with_suffix("").as_posix()
                basename = path.stem

                # Within-dir basename invariant. Catches nested
                # collisions that would make `-l name=basename`
                # ambiguous.
                existing = dir_basenames.get(basename)
                if existing is not None:
                    raise ValueError(
                        f"Two site files in `{sites_dir}` share basename "
                        f"`{basename}`: `{existing}` and `{path}`. Every "
                        f"basename must be unique within a trusted sites "
                        f"directory so `-l name={basename}` resolves "
                        f"unambiguously. Rename one of the files."
                    )
                dir_basenames[basename] = path
                # Cross-directory basename collisions are only valid
                # overlays when the relative path also matches. Otherwise
                # the same identifier would refer to two distinct logical
                # sites.
                existing_basename = basename_to_path.get(basename)
                if existing_basename is not None:
                    existing_rel = self._canonical_site_id(existing_basename)
                    if existing_rel != rel_path:
                        raise ValueError(
                            f"Cross-directory basename `{basename}` "
                            f"collision between `{existing_basename}` "
                            f"and `{path}`. Cross-directory basename "
                            f"matches are valid only when the relative "
                            f"path also matches (overlay). Different "
                            f"relative paths would let `-l name={basename}` "
                            f"refer to two distinct sites. Rename one of "
                            f"the files."
                        )
                # First trusted dir wins on basename and relative path
                # (overlay semantics).
                basename_to_path.setdefault(basename, path)
                rel_path_to_path.setdefault(rel_path, path)

        internal_name_to_path: dict[str, Path] = {}
        for path in basename_to_path.values():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = _normalize_null_site_mappings(yamlio.load(f) or {})
            except (yaml.YAMLError, OSError):
                # Defer parse errors to load_site() for context-rich reporting.
                continue
            internal_name = self._read_internal_name(data)
            if not internal_name or internal_name == path.stem:
                continue
            collider = basename_to_path.get(internal_name)
            if collider is not None and collider.resolve() != path.resolve():
                raise ValueError(
                    f"Site `{path}` declares `name: {internal_name}` "
                    f"which collides with file basename `{collider.name}`. "
                    f"Each site identity must resolve to exactly one file. "
                    f"If `{path.name}` is a copy you forgot to update, "
                    f"change its `name:` field to `{path.stem}`. Otherwise "
                    f"rename one of the files."
                )
            collider = rel_path_to_path.get(internal_name)
            if collider is not None and collider.resolve() != path.resolve():
                raise ValueError(
                    f"Site `{path}` declares `name: {internal_name}` "
                    f"which collides with the path-form identifier of "
                    f"file `{collider}`. Rename the `name:` field."
                )
            existing = internal_name_to_path.get(internal_name)
            if existing is not None and existing.resolve() != path.resolve():
                raise ValueError(
                    f"Two sites declare the same `name: {internal_name}`: "
                    f"`{existing}` and `{path}`. Site names must be "
                    f"unique across the workspace."
                )
            internal_name_to_path[internal_name] = path
        return basename_to_path, rel_path_to_path, internal_name_to_path

    @staticmethod
    def _read_internal_name(data: dict[str, Any]) -> str | None:
        """Read the internal `name:` from a parsed site file.

        Supports the flat shape (`name:` at top level) and the K8s-style
        nested shape (`metadata.name:`). Returns None if neither is set.

        Type-safe on purpose. This runs while the workspace index is built,
        which is before any site is validated, so a malformed `metadata`, or a
        `name` that is a list, would otherwise surface here as an attribute
        error or as an unhashable-key error naming neither the file nor the
        key. Returning None defers the real diagnostic to `Site.from_data`,
        which names both.
        """
        if "spec" in data:
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                return None
            name = metadata.get("name")
        else:
            name = data.get("name")
        return name if isinstance(name, str) else None

    def _canonical_site_id(self, site_path: Path) -> str:
        """Return the canonical relative-path identifier for a site file.

        Used to key the overlay merge in `_load_site_data`. Falls back to
        the basename when the path is not under any trusted directory
        as a defensive fallback that should not be needed in practice.
        """
        for sites_dir in self._trusted_sites_dirs:
            try:
                return site_path.relative_to(sites_dir).with_suffix("").as_posix()
            except ValueError:
                continue
        return site_path.stem

    def _deep_merge(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge two dictionaries, with override taking precedence.

        Behavior:
        - Nested dicts are merged recursively
        - Lists are REPLACED entirely (not concatenated)
        - Scalar values from override replace base values

        Args:
            base: Base dictionary
            override: Override dictionary (values take precedence)

        Returns:
            New merged dictionary (neither input is modified)

        Example:
            >>> base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
            >>> override = {"a": {"x": 10}, "b": [3]}
            >>> _deep_merge(base, override)
            {"a": {"x": 10, "y": 2}, "b": [3]}  # Note: list replaced, not merged
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _deep_merge_provenance(
        self,
        base: dict[str, Any],
        override: dict[str, Any],
        origin: str,
        prov: dict[str, str],
        prefix: str = "",
    ) -> dict[str, Any]:
        """Like `_deep_merge` but tracks per-key provenance.

        For each leaf key in `override`, records `prov[<dotted-path>] = origin`.
        Lists and scalars overwrite as a unit (matching `_deep_merge`'s
        list-replacement semantic), so the whole key gets the new origin.
        Nested dicts recurse so per-leaf attribution is preserved, even
        when the dict subtree is new (not present in `base`).

        `prov` is mutated in place. The returned dict is a new merged
        result. Neither input is modified.
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                if key in result and not isinstance(result[key], dict):
                    self._remove_provenance_subtree(prov, full_key)
                # Recurse whether or not the dict subtree exists in base.
                # When base lacks the key the inner walk attributes every
                # leaf. Otherwise it merges and only re-attributes leaves the
                # override actually touched.
                base_subtree = result[key] if key in result and isinstance(result[key], dict) else {}
                result[key] = self._deep_merge_provenance(
                    base_subtree, value, origin, prov, full_key
                )
            else:
                self._remove_provenance_subtree(prov, full_key)
                result[key] = copy.deepcopy(value)
                prov[full_key] = origin
        return result

    @staticmethod
    def _remove_provenance_subtree(
        prov: dict[str, str],
        path: str,
    ) -> None:
        """Remove an origin for `path` and every descendant below it."""
        descendant_prefix = f"{path}."
        stale = [
            key
            for key in prov
            if key == path or key.startswith(descendant_prefix)
        ]
        for key in stale:
            del prov[key]

    def _resolve_inherits(self, child_path: Path, inherits_value: str) -> Path:
        """Resolve an `inherits:` reference to an absolute path.

        Resolution order:
        1. Relative to the child file's directory (default, locality-preserving).
        2. Narrow fallback: if the relative path does not exist AND
           `inherits_value` is a bare filename (no path separators), look for
           it in the workspace's `sites/` directory. This lets a site file
           in an extra trusted dir reference a workspace-owned template
           (e.g. `base-site.yaml`) without copying the template or inventing
           a new syntax. The fallback is intentionally limited to
           `workspace/sites/`. It does NOT search other extras or
           `sites.local/`, so there is no cross-extra-dir shared namespace
           and no way for an overlay to inject a new inheritance target.

        `inherits:` is author-trusted: the value comes from a trusted site
        file (workspace `sites/` or an operator-vouched extras dir), so the
        resolver deliberately does NOT sandbox the resolved path to a
        specific set of filesystem roots. The real control is who may
        author files in those trusted locations. See the "Trust model"
        section in docs/site-configuration.md.

        Args:
            child_path: Absolute path of the file that declares `inherits`.
            inherits_value: The raw `inherits:` value from that file.

        Returns:
            Absolute, resolved path to the parent template.

        Raises:
            FileNotFoundError: If the parent cannot be resolved by either
                strategy. The error lists every path that was probed so
                the operator can see why fallback did not help.
            ValueError: If the value is not a path.
        """
        # Checked here rather than with the other field types, since this is
        # read while the merge is assembled and a non-string reaches a path
        # join before any model exists to validate it.
        if not isinstance(inherits_value, str):
            raise ValueError(
                f"'inherits' in site '{self._origin_label(child_path)}' must be "
                f"text naming one parent file, got "
                f"{type(inherits_value).__name__}. A site has a single "
                f"inheritance chain, so chain the parents instead of listing them."
            )

        tried: list[Path] = []

        relative = (child_path.parent / inherits_value).resolve()
        tried.append(relative)
        if relative.exists():
            return relative

        if "/" not in inherits_value and "\\" not in inherits_value:
            workspace_candidate = (self.workspace / "sites" / inherits_value).resolve()
            if workspace_candidate != relative:
                tried.append(workspace_candidate)
                if workspace_candidate.exists():
                    logger.debug(
                        f"`inherits: {inherits_value}` in {child_path} resolved "
                        f"via workspace fallback to {workspace_candidate}"
                    )
                    return workspace_candidate

        searched = "\n  - ".join(str(p) for p in tried)
        raise FileNotFoundError(
            f"Inherited file not found for `inherits: {inherits_value}` "
            f"declared in {child_path}. Searched:\n  - {searched}"
        )

    def _load_inherited_data(
        self,
        path: Path,
        seen: list[Path] | None = None,
        prov: dict[str, str] | None = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load inherited site template with support for chained inheritance.

        Resolves the `inherits` field recursively, merging parent data first.

        When called without a provenance dict, the merged result is
        memoized on `path.resolve()` for the orchestrator's lifetime so
        N sites sharing one template only parse it once. Provenance
        callers bypass the cache because each call mutates `prov`.

        Args:
            path: Absolute path to the inherited file
            seen: List of visited paths for cycle detection (preserves order)
            prov: Optional provenance dict. When supplied, every leaf key
                gets its origin attributed to the file that contributed
                the final value. Mutated in place.
            sources: Optional list collecting every file read, in merge order.
                Supplied only when a message needs to name them, since it
                bypasses the memo for the same reason `prov` does.

        Returns:
            Merged data from inheritance chain (with metadata fields stripped)

        Raises:
            FileNotFoundError: If inherited file doesn't exist
            ValueError: If circular inheritance is detected or kind is invalid
        """
        if seen is None:
            seen = []

        # Normalize path for consistent cycle detection
        normalized = path.resolve()
        if normalized in seen:
            cycle_path = " -> ".join(str(p) for p in seen) + f" -> {normalized}"
            raise ValueError(f"Circular inheritance detected: {cycle_path}")
        seen.append(normalized)

        # Cache hit returns a deep copy so callers may mutate freely.
        # Skip cache when prov is supplied because each provenance call
        # mutates the caller's prov dict and is not idempotent.
        if prov is None and sources is None and normalized in self._inherited_data_cache:
            return copy.deepcopy(self._inherited_data_cache[normalized])

        if not path.exists():
            raise FileNotFoundError(f"Inherited file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = _normalize_null_site_mappings(yamlio.load(f) or {})

        # Inherits parents must be SiteTemplates. A `kind: Site` parent
        # would chain deployable sites together, where editing one would
        # silently change the other. That is almost always an authoring
        # mistake. Use SiteTemplate for any reusable base.
        kind = data.get("kind")
        if kind is not None and kind != "SiteTemplate":
            raise ValueError(
                f"Cannot inherit from kind '{kind}' in {path}. "
                f"Inherits parents must be SiteTemplate."
            )

        # Handle chained inheritance
        if "inherits" in data:
            parent_path = self._resolve_inherits(path, data["inherits"])
            parent_data = self._load_inherited_data(
                parent_path, seen, prov=prov, sources=sources
            )
            # Recorded after the parent, so the list reads in merge order.
            if sources is not None:
                sources.append(self._origin_label(path))
            # Remove metadata fields before merging
            child_data = {
                k: v for k, v in data.items() if k not in ("inherits", "kind", "apiVersion")
            }
            if prov is not None:
                data = self._deep_merge_provenance(
                    parent_data, child_data, self._origin_label(path), prov
                )
            else:
                data = self._deep_merge(parent_data, child_data)
        else:
            # Remove metadata fields from leaf template
            leaf_data = {k: v for k, v in data.items() if k not in ("kind", "apiVersion")}
            if sources is not None:
                sources.append(self._origin_label(path))
            if prov is not None:
                # Attribute every leaf in the leaf template to itself.
                data = self._deep_merge_provenance(
                    {}, leaf_data, self._origin_label(path), prov
                )
            else:
                data = leaf_data

        if prov is None and sources is None:
            self._inherited_data_cache[normalized] = copy.deepcopy(data)

        logger.debug(f"Loaded inherited data from: {path}")
        return data

    def _load_site_data(
        self,
        name: str,
        prov: dict[str, str] | None = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load and merge site data with inheritance and overlay support.

        Merge order (later overrides earlier):
        1. inherits target         - Parent template (resolved recursively)
        2. sites/                  - Primary trusted site definitions (committed)
        3. extra_trusted_sites_dirs - Additional trusted dirs, in list order
        4. sites.local/            - Local/CI overrides (gitignored)

        `inherits` handling:
        - The FIRST trusted directory to contain the site establishes the
          inheritance chain (`inherits` is honored).
        - Any later file (in another trusted dir OR in `sites.local/`) has
          its `inherits` stripped. A site has exactly one inheritance
          chain, determined by its base file.

        This means `sites.local/` cannot inject inheritance at all: the
        security invariant is preserved regardless of how many extra trusted
        dirs are configured.

        Identity (`name`, `metadata.name`) is set by the BASE file. Overlays
        in other trusted dirs and in `sites.local/` cannot rename the site.
        Lifting that rule would let an overlay produce a site whose name is
        not findable through any of the workspace indexes (built from the
        base file). Use `inherits:` or rename the base file instead.

        Args:
            name: Site name (filename without extension).
            prov: Optional provenance dict. When supplied, every leaf key
                in the merged data gets attributed to the file whose
                value won. The outer merge of inherited data uses plain
                `_deep_merge` so attributions from the chain walk
                survive (the inherited dict was already attributed
                inside `_load_inherited_data`).

        Returns:
            Merged site data dictionary.

        Raises:
            FileNotFoundError: If no trusted dir or sites.local/ has the file.
            ValueError: If inheritance creates a cycle, references invalid
                kind, or an overlay tries to set `name`/`metadata.name`.
        """
        site_dirs = [
            *self._trusted_sites_dirs,
            self.workspace / "sites.local",
        ]

        merged_data: dict[str, Any] = {}
        found = False
        is_base_file = True  # First file found establishes the inheritance chain

        for sites_dir in site_dirs:
            for ext in (".yaml", ".yml"):
                path = sites_dir / f"{name}{ext}"
                if path.exists():
                    with open(path, "r", encoding="utf-8") as f:
                        data = _normalize_null_site_mappings(
                            yamlio.load(f) or {}
                        )

                    # Process inheritance only on the first file found (the base)
                    if is_base_file and "inherits" in data:
                        inherits_path = self._resolve_inherits(path, data["inherits"])
                        # Initialize seen list with current file to detect self-reference
                        inherited_data = self._load_inherited_data(
                            inherits_path, seen=[path.resolve()], prov=prov, sources=sources
                        )
                        # Merge inherited into the working dict WITHOUT
                        # re-attribution. The per-leaf provenance for
                        # inherited keys was already set during the chain
                        # walk. The outer merge would otherwise clobber it with
                        # the parent file's label.
                        merged_data = self._deep_merge(merged_data, inherited_data)
                        # Remove inherits from data before merging
                        data = {k: v for k, v in data.items() if k != "inherits"}
                    elif not is_base_file and "inherits" in data:
                        # Strip inherits from any non-base file. For sites.local/
                        # this prevents runtime injection of inheritance (security).
                        # For additional trusted dirs it reflects the rule that a
                        # site has exactly one inheritance chain, established by
                        # the base file.
                        data = {k: v for k, v in data.items() if k != "inherits"}

                    # Reject overlay-renames-site. Identity is set by the
                    # base file. The workspace name indexes are built from
                    # base files, so an overlay rename produces a
                    # site unfindable through any index. Allow overlays
                    # to RESTATE the same name (the common case where
                    # extras-dir overlays mirror the base shape) and
                    # reject only when the overlay tries to CHANGE it.
                    # When the base omits an explicit `name:`, identity
                    # defaults to the basename of the canonical id, so
                    # an overlay introducing a different name is also
                    # a rename.
                    if not is_base_file:
                        overlay_name = self._read_internal_name(data)
                        if overlay_name is not None:
                            existing_name = (
                                self._read_internal_name(merged_data)
                                or name.rsplit("/", 1)[-1]
                            )
                            if overlay_name != existing_name:
                                raise ValueError(
                                    f"Overlay {path} cannot rename the site "
                                    f"({existing_name!r} -> {overlay_name!r}). "
                                    f"Site identity is established by the base "
                                    f"file. Use `inherits:` or rename the base "
                                    f"file."
                                )

                    # Recorded after the inherit chain, so the list reads in
                    # merge order: parents first, then this file, then overlays.
                    if sources is not None:
                        sources.append(self._origin_label(path))

                    if prov is not None:
                        merged_data = self._deep_merge_provenance(
                            merged_data, data, self._origin_label(path), prov
                        )
                    else:
                        merged_data = self._deep_merge(merged_data, data)
                    found = True
                    if is_base_file:
                        logger.debug(f"Loaded site data from: {path}")
                    else:
                        # DEBUG: avoids per-overlay noise across large fleets.
                        logger.debug(f"Site '{name}': applied overlay {path}")
                    is_base_file = False  # Subsequent files are overlays
                    break  # Only load one file per directory (prefer .yaml)

        if not found:
            where = "sites/"
            if self._extra_trusted_sites_dirs:
                where += ", extra trusted sites dirs,"
            where += " or sites.local/"
            raise FileNotFoundError(f"Site '{name}' not found in {where}")

        return merged_data

    def _origin_label(self, path: Path) -> str:
        """Return a stable workspace-relative label for a source file.

        Used by the provenance walk so per-key attribution renders
        identically across machines. Falls back to the absolute path
        when the file lives outside the workspace (e.g., an extra
        trusted dir under a different parent).
        """
        try:
            return path.resolve().relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def _name_the_files_behind(self, canonical_id: str, error: str) -> str:
        """Add the files a site was merged from to a validation error.

        A site is checked after its inherit chain and every overlay are merged,
        so the key that failed can come from a parent template, an extra
        trusted directory, or `sites.local/`. The site's own name points at
        none of those, and one shared parent produces the same error for every
        site that inherits it.

        Collected here rather than during the load, since threading it through
        the normal path would bypass the inherit-chain memo for every site
        instead of only for the one that failed.
        """
        sources: list[str] = []
        try:
            self._load_site_data(canonical_id, sources=sources)
        except (OSError, ValueError):
            return error
        if not sources:
            return error
        return f"{error} Merged from: {', '.join(sources)}."

    def load_site_with_provenance(self, name: str) -> tuple[Site, dict[str, str]]:
        """Load a site and return per-key provenance for its merged data.

        The provenance dict maps every dotted leaf key in the merged
        site to the workspace-relative path of the file whose value
        won. Used by `siteops sites <name> --show-sources` to show where each
        value came from after inherit + overlay merge.

        For sites authored with the K8s envelope shape (`spec:`,
        `metadata:`), prov keys are normalized to the flat-shape view
        (`subscription`, `labels.X`, `properties.X`) so callers do not
        need to know about the on-disk envelope.

        Args:
            name: Basename, relative path, or internal `name:` value.

        Returns:
            `(site, provenance)` where `site` is the fully resolved
            Site (matching `load_site(name)`) and `provenance` is the
            per-leaf origin map.
        """
        if "/" in name or "\\" in name:
            try:
                lookup_key = _normalize_site_identifier(name)
            except ValueError:
                lookup_key = name
        else:
            lookup_key = name
        site_path = self._find_trusted_site_file(lookup_key)
        if site_path is None:
            where = "sites/"
            if self._extra_trusted_sites_dirs:
                where += " or extra trusted sites dirs"
            raise FileNotFoundError(f"Site file not found: {name} (searched {where})")
        if self._is_site_template(site_path):
            raise ValueError(
                f"Cannot load '{name}' as a site: it is a SiteTemplate "
                f"(inheritance-only). SiteTemplates cannot be deployed directly."
            )
        canonical_id = self._canonical_site_id(site_path)
        default_name = site_path.stem
        prov: dict[str, str] = {}
        merged_data = self._load_site_data(canonical_id, prov=prov)
        _validate_resource(merged_data, "Site", site_path)
        # Built from merged data, so validation covers a key an overlay
        # contributed as well as one the base file carries.
        try:
            site = Site.from_data(merged_data, source=name, default_name=default_name)
        except ValueError as e:
            raise ValueError(self._name_the_files_behind(canonical_id, str(e))) from e
        # Normalize prov to the flat-shape view that matches `Site` so
        # display-time lookups like `prov["subscription"]` succeed
        # regardless of whether the on-disk file used the K8s envelope.
        prov = self._normalize_provenance_to_flat_shape(merged_data, prov)
        return site, prov

    @staticmethod
    def _normalize_provenance_to_flat_shape(
        merged_data: dict[str, Any], prov: dict[str, str]
    ) -> dict[str, str]:
        """Rewrite K8s-envelope prov keys to the flat-shape view.

        When the merged data uses `spec:`/`metadata:`, the walker
        attributed keys like `spec.subscription` and `metadata.name`.
        The flat-shape view used by the CLI display is `subscription`
        and `name`. Translate so the consumer sees one shape.

        The trigger is conservative: only rewrite when the merged data
        actually has the K8s-envelope shape (a `spec:` or `metadata:`
        top-level dict), and only for `Site` (or unspecified-kind)
        resources. Anything else is passed through to avoid silently
        mis-normalizing a flat-shape dict that happens to have a
        top-level field named `spec`.
        """
        kind = merged_data.get("kind")
        if kind not in (None, "Site"):
            return prov
        has_envelope = (
            isinstance(merged_data.get("spec"), dict)
            or isinstance(merged_data.get("metadata"), dict)
        )
        if not has_envelope:
            return prov
        new_prov: dict[str, str] = {}
        for key, origin in prov.items():
            if key == "spec" or key == "metadata" or key == "metadata.labels":
                continue
            if key.startswith("spec."):
                new_prov[key[len("spec."):]] = origin
            elif key == "metadata.name":
                new_prov["name"] = origin
            elif key.startswith("metadata.labels."):
                new_prov[key.replace("metadata.labels.", "labels.", 1)] = origin
            else:
                new_prov[key] = origin
        return new_prov

    def load_site(self, name: str) -> Site:
        """Load a site by name, applying inheritance and local overlays.

        `name` may be the site file's basename, its relative path under
        a trusted `sites/` directory, OR its internal `name:` field. All
        three forms are symmetric (see `_find_trusted_site_file`).

        Resolution order (later sources override earlier):
        1. Inherited site/template (if 'inherits' specified on the base file).
        2. Base site file from `sites/` or any extra trusted dir (first
           trusted dir containing the file wins).
        3. Overlays from any remaining trusted dirs (`inherits` stripped).
        4. Local overlay from `sites.local/<relative-path>.yaml` if present
           (`inherits` stripped). Keyed by the relative path of the base
           file under its trusted dir, so nested sites have nested overlays.

        Args:
            name: Basename, relative path, OR internal `name:` value.

        Returns:
            Fully resolved Site instance.

        Raises:
            ValueError: If the site file is invalid, missing required
                fields, references a non-existent inherited file, or two
                files in the workspace would resolve to the same name.
            FileNotFoundError: If no form matches.
        """
        # Normalize path-form identifiers (forward-slash separators) so
        # the cache lookup is consistent across `regions/eu/munich` and
        # `regions\\eu\\munich` and similar variants.
        if "/" in name or "\\" in name:
            try:
                lookup_key = _normalize_site_identifier(name)
            except ValueError:
                lookup_key = name
        else:
            lookup_key = name
        with self._cache_lock:
            if lookup_key in self._site_cache:
                return self._site_cache[lookup_key]

        site_path = self._find_trusted_site_file(lookup_key)
        if site_path is None:
            where = "sites/"
            if self._extra_trusted_sites_dirs:
                where += " or extra trusted sites dirs"
            raise FileNotFoundError(f"Site file not found: {name} (searched {where})")

        # Canonical id keys the overlay merge in `_load_site_data`.
        # Equal to the basename for flat layouts, or to the relative
        # path under the owning trusted dir for nested layouts.
        canonical_id = self._canonical_site_id(site_path)
        # Default `Site.name` is the basename. Unique by invariant.
        default_name = site_path.stem

        # Check if this is a SiteTemplate (cannot be loaded directly)
        if self._is_site_template(site_path):
            raise ValueError(
                f"Cannot load '{name}' as a site: it is a SiteTemplate (inheritance-only). "
                f"SiteTemplates cannot be deployed directly."
            )

        # Load and merge site data (handles inheritance + local overlay)
        merged_data = self._load_site_data(canonical_id)

        # Validate merged data
        _validate_resource(merged_data, "Site", site_path)

        # Built from merged data, so validation covers a key an overlay
        # contributed as well as one the base file carries.
        try:
            site = Site.from_data(merged_data, source=name, default_name=default_name)
        except ValueError as e:
            raise ValueError(self._name_the_files_behind(canonical_id, str(e))) from e

        # Cache under every form the caller might use later. Always
        # under the canonical id (basename or relative path) and the
        # internal name. Also under whatever the caller actually passed
        # (and its normalized form, if a path-form identifier).
        with self._cache_lock:
            self._site_cache[canonical_id] = site
            if default_name != canonical_id:
                self._site_cache[default_name] = site
            if site.name and site.name not in self._site_cache:
                self._site_cache[site.name] = site
            self._site_cache[lookup_key] = site
            if name != lookup_key:
                self._site_cache[name] = site

        return site

    def _get_all_site_names(self) -> list[str]:
        """Get all deployable site names from trusted site directories.

        Recursively scans every trusted site directory for YAML files
        and returns the basenames of files that represent deployable
        sites (`kind: Site`). Files with `kind: SiteTemplate` are
        excluded (inheritance-only). Files in `sites.local/` are NOT
        discoverable. That directory is the overlay for committed and
        trusted sites, not a source of new site identities.

        The basename-uniqueness invariant (enforced by
        `_build_site_indexes`) guarantees each returned basename maps to
        exactly one file, even when nested under subdirectories.

        Returns:
            Sorted list of site basenames (filenames without extension).

        Note:
            Files that cannot be parsed are included and will error
            during `load_site()`. Allows proper error reporting with
            full context rather than silent omission.
        """
        site_names: set[str] = set()
        for _sites_dir, path in self._iter_trusted_site_files():
            site_names.add(path.stem)
        return sorted(site_names)  # Sort for deterministic order

    def _is_site_template(self, path: Path) -> bool:
        """Check if a YAML file is a SiteTemplate (inheritance-only).

        Memoized on resolved path for the orchestrator's lifetime.

        Args:
            path: Path to the YAML file

        Returns:
            True if the file has kind: SiteTemplate, False otherwise

        Note:
            Returns False if the file cannot be parsed, allowing load_site()
            to handle the error with proper context.
        """
        resolved = path.resolve()
        cached = self._template_check_cache.get(resolved)
        if cached is not None:
            return cached
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yamlio.load(f)
            result = bool(data and data.get("kind") == "SiteTemplate")
        except (yaml.YAMLError, OSError):
            # Let load_site() handle parsing errors with full context
            result = False
        self._template_check_cache[resolved] = result
        return result

    def load_all_sites(self) -> list[Site]:
        """Load all deployable sites from trusted site directories.

        Discovers sites from `sites/` and any extra trusted directories,
        then loads each (applying `sites.local/` overlays where present).
        Precedence within a single site: `sites.local/` > extra trusted
        dirs (last wins) > `sites/`.

        Memoized for the orchestrator's lifetime. The result is a stable
        snapshot of every site once the workspace finishes loading.
        Subsequent commands like `explain_no_match` reuse it.

        Returns:
            List of all Site instances found (with merged configuration).
        """
        if self._all_sites_cache is not None:
            return self._all_sites_cache

        sites: list[Site] = []
        skipped = []

        for name in self._get_all_site_names():
            try:
                site = self.load_site(name)
                sites.append(site)
            except (ValueError, yaml.YAMLError, OSError) as e:
                # Site errors can carry trusted directory paths and internal
                # names, so published output uses one generic diagnostic.
                reportable = report_site_load_error(e)
                logger.warning(
                    f"Failed to load site '{site_name_for_output(name)}': "
                    f"{reportable}"
                )
                skipped.append((name, reportable))

        if skipped:
            import sys

            print(f"\n\u26a0 Skipped {len(skipped)} site(s) due to errors:", file=sys.stderr)
            for name, error in skipped:
                print(
                    f"  \u2022 {site_name_for_output(name)}: {error}",
                    file=sys.stderr,
                )
            print(file=sys.stderr)

        # Recorded so a caller can fail on it. A site that does not load is one
        # the operator expected to deploy to, so a run that silently continues
        # against a smaller fleet is reporting success for work it did not do.
        self.skipped_sites = list(skipped)
        self._all_sites_cache = sites
        return sites

    def load_parameters(self, path: Path) -> dict[str, Any]:
        """Load parameters from a YAML/JSON file with caching.

        Thread-safe caching prevents re-reading files during parallel deployments.
        Returns a deep copy to prevent mutation of cached data.

        Args:
            path: Path to the parameter file

        Returns:
            Dict of parameters (deep copy from cache)
        """
        path = path.resolve()

        with self._params_cache_lock:
            if path in self._params_cache:
                return copy.deepcopy(self._params_cache[path])

        if not path.exists():
            logger.warning(f"Parameter file not found: {path}")
            return {}

        with open(path, "r", encoding="utf-8") as f:
            if path.suffix == ".json":
                result = json.load(f, object_pairs_hook=_reject_duplicate_json_keys)
            else:
                result = yamlio.load(f) or {}

        with self._params_cache_lock:
            self._params_cache[path] = result

        return copy.deepcopy(result)

    def _resolve_template_strings(
        self, value: Any, site: Site, step_outputs: dict[str, dict[str, Any]] | None = None
    ) -> Any:
        """Recursively resolve {{ site.X }} templates in values.

        Supports:
        - {{ site.name }}
        - {{ site.location }}
        - {{ site.resourceGroup }}
        - {{ site.subscription }}
        - {{ site.labels.<key> }}
        - {{ site.properties.<path> }} (nested paths supported)
        - {{ site.parameters.<path> }} (nested paths supported)

        Args:
            value: Value to resolve (string, dict, list, or other)
            site: Site to resolve variables from
            step_outputs: Optional step outputs for chaining

        Returns:
            Value with all site templates resolved
        """
        if isinstance(value, str):
            # Simple replacements
            result = value
            result = result.replace("{{ site.name }}", site.name)
            result = result.replace("{{ site.location }}", site.location)
            result = result.replace("{{ site.resourceGroup }}", site.resource_group)
            result = result.replace("{{ site.subscription }}", site.subscription)

            # Labels
            for key, val in site.labels.items():
                result = result.replace(f"{{{{ site.labels.{key} }}}}", str(val))

            # Properties (complex paths) - may return non-string for entire object/array templates
            result = self._resolve_properties_templates(result, site.properties)

            # Parameters (complex paths) - only if result is still a string
            # (properties resolution may have returned a list/dict for templates like {{ site.properties.endpoints }})
            if isinstance(result, str):
                result = self._resolve_parameters_templates(result, site.parameters)

            return result

        elif isinstance(value, dict):
            return _resolve_parameter_mapping(
                value, lambda v: self._resolve_template_strings(v, site, step_outputs)
            )
        elif isinstance(value, list):
            return [self._resolve_template_strings(v, site, step_outputs) for v in value]
        return value

    def _resolve_parameters_templates(self, value: str, parameters: dict[str, Any]) -> Any:
        """Resolve {{ site.parameters.<path> }} templates in a string.

        Supports nested paths like:
        - {{ site.parameters.clusterName }}
        - {{ site.parameters.brokerConfig.memoryProfile }}

        Args:
            value: String potentially containing parameter templates
            parameters: Site parameters dict

        Returns:
            Resolved value (may be non-string if entire value is a single template)
        """
        # Check if entire string is a single template (for complex types)
        stripped = value.strip()
        full_match = SITE_PARAMETERS_PATTERN.fullmatch(stripped)
        if full_match:
            path = full_match.group(1)
            resolved = self._resolve_property_path(parameters, path)
            if resolved is not None:
                return resolved
            # Return original if path not found
            return value

        # For strings with embedded templates, do string substitution
        def replacer(match: re.Match) -> str:
            path = match.group(1)
            resolved = self._resolve_property_path(parameters, path)
            if resolved is not None:
                return str(resolved)
            return match.group(0)  # Return original if not found

        return SITE_PARAMETERS_PATTERN.sub(replacer, value)

    def _resolve_properties_templates(self, value: str, properties: dict[str, Any]) -> Any:
        """Resolve {{ site.properties.<path> }} templates in a string.

        Supports nested paths like:
        - {{ site.properties.mqtt.broker }}
        - {{ site.properties.deviceEndpoints[0].host }}
        - {{ site.properties.deviceEndpoints }} (returns entire list/object)

        Args:
            value: String potentially containing property templates
            properties: Site properties dict

        Returns:
            Resolved value (may be non-string if entire value is a single template)
        """
        # Check if entire string is a single template (for complex types)
        stripped = value.strip()
        full_match = SITE_PROPERTIES_PATTERN.fullmatch(stripped)
        if full_match:
            path = full_match.group(1)
            resolved = self._resolve_property_path(properties, path)
            if resolved is not None:
                return resolved
            return value

        # For strings with embedded templates, do string substitution
        def replacer(match: re.Match) -> str:
            path = match.group(1)
            resolved = self._resolve_property_path(properties, path)
            if resolved is not None:
                # Convert to string for embedded templates
                if isinstance(resolved, (dict, list)):
                    return json.dumps(resolved)
                return str(resolved)
            return match.group(0)  # Return original if not found

        return SITE_PROPERTIES_PATTERN.sub(replacer, value)

    def _resolve_property_path(self, obj: Any, path: str) -> Any:
        """Resolve a dotted path with optional array indices.

        Examples:
            - "mqtt.broker" -> obj["mqtt"]["broker"]
            - "endpoints[0].host" -> obj["endpoints"][0]["host"]
            - "devices[0]" -> obj["devices"][0]

        Args:
            obj: Object to traverse
            path: Dotted path with optional [N] indices

        Returns:
            Resolved value or None if path doesn't exist
        """

        # Split path into segments, handling array notation
        # e.g., "endpoints[0].host" -> ["endpoints", "[0]", "host"]
        segments = re.split(r"\.(?![^\[]*\])", path)

        current = obj
        for segment in segments:
            if current is None:
                return None

            # Check for array index notation: "name[0]" or just "[0]"
            array_match = re.match(r"^([a-zA-Z0-9_]*)\[(\d+)\]$", segment)
            if array_match:
                key = array_match.group(1)
                index = int(array_match.group(2))

                if key:
                    if not isinstance(current, dict) or key not in current:
                        return None
                    current = current[key]

                if not isinstance(current, list) or index >= len(current):
                    return None
                current = current[index]
            else:
                if not isinstance(current, dict) or segment not in current:
                    return None
                current = current[segment]

        return current

    @staticmethod
    def _require_selected_parameter_file(
        declared: str, resolved: str, site: Site, workspace: Path, tier: str
    ) -> None:
        """Fail when a site-selected parameter file does not resolve to a real file.

        A path carrying a variable, such as
        `parameters/<area>/{{ site.properties.X }}.yaml`, means the site picks
        which file to load. Two cases are rejected:

        - The site does not carry the property, so the variable survives and the
          path names no file.
        - The site carries a value naming a file that does not exist.

        Either is a site or manifest defect rather than an intentional skip.
        Without this check the deploy reports success with those parameters
        missing, so neither is visible. A path with no variable in it is a fixed input the
        manifest author controls, and a missing one stays a warning so an
        optional file keeps working.

        Raises:
            ParameterSelectionError: If the path is site-selected and names no file.
        """
        if "{{" not in declared:
            return

        if "{{" in resolved:
            raise ParameterSelectionError(
                f"{tier} parameter path '{declared}' did not resolve for site "
                f"'{site.name}' (resolved to '{resolved}'). The site does not "
                f"carry the property the path selects on. Add it to the site or "
                f"to the site it inherits from."
            )

        resolved_path = Path(resolved)
        if resolved_path.is_absolute():
            raise ParameterSelectionError(
                f"{tier} parameter path '{declared}' resolved to "
                f"'{resolved}' for site '{site.name}', but site-selected "
                "parameter paths must be relative to the workspace."
            )

        if ".." in resolved_path.parts:
            raise ParameterSelectionError(
                f"{tier} parameter path '{declared}' resolved to "
                f"'{resolved}' for site '{site.name}', but site-selected "
                "parameter paths must not contain '..' path segments."
            )

        workspace_root = workspace.resolve()
        full_path = (workspace_root / resolved_path).resolve()
        try:
            full_path.relative_to(workspace_root)
        except ValueError as exc:
            raise ParameterSelectionError(
                f"{tier} parameter path '{declared}' resolved to "
                f"'{resolved}' for site '{site.name}', which resolves outside "
                "the workspace."
            ) from exc

        if not full_path.is_file():
            raise ParameterSelectionError(
                f"{tier} parameter path '{declared}' resolved to "
                f"'{resolved}' for site '{site.name}', which does not exist. "
                f"Check the value the site selects for a typo, or add the file."
            )

    def _resolve_property_path_with_presence(
        self,
        obj: Any,
        path: str,
    ) -> tuple[bool, Any]:
        """Resolve a property path while distinguishing absent from null."""
        segments = re.split(r"\.(?![^\[]*\])", path)
        current = obj
        traversed: list[str] = []
        for segment in segments:
            array_match = re.match(r"^([a-zA-Z0-9_-]*)\[(\d+)\]$", segment)
            if array_match:
                key = array_match.group(1)
                index = int(array_match.group(2))
                if key:
                    if not isinstance(current, dict):
                        parent = ".".join(traversed) or "properties"
                        raise ParameterSelectionError(
                            f"site.properties.{parent} must be a mapping to "
                            f"resolve site.properties.{path}, got "
                            f"{type(current).__name__}."
                        )
                    if key not in current:
                        return False, None
                    current = current[key]
                    traversed.append(key)
                if not isinstance(current, list) or index >= len(current):
                    parent = ".".join(traversed) or "properties"
                    raise ParameterSelectionError(
                        f"site.properties.{parent} must be a list containing "
                        f"index {index} to resolve site.properties.{path}."
                    )
                current = current[index]
                traversed.append(f"[{index}]")
                continue
            if not isinstance(current, dict):
                parent = ".".join(traversed) or "properties"
                raise ParameterSelectionError(
                    f"site.properties.{parent} must be a mapping to resolve "
                    f"site.properties.{path}, got "
                    f"{type(current).__name__}."
                )
            if segment not in current:
                return False, None
            current = current[segment]
            traversed.append(segment)
        return True, current

    def _expand_manifest_parameter_source(
        self,
        source: str | ParameterSource,
        manifest: Manifest,
        site: Site,
    ) -> list[tuple[str, tuple[str, ...]]]:
        """Resolve one manifest parameter source into ordered file paths."""
        if isinstance(source, str):
            resolved = manifest.resolve_parameter_path(source, site)
            self._require_selected_parameter_file(
                source,
                resolved,
                site,
                self.workspace,
                "Manifest",
            )
            return [(resolved, ())]

        if source.for_each is None:
            resolved = manifest.resolve_parameter_path(source.path, site)
            self._require_selected_parameter_file(
                source.path,
                resolved,
                site,
                self.workspace,
                "Manifest",
            )
            return [(resolved, source.collections)]

        match = FOR_EACH_SITE_PROPERTY_PATTERN.fullmatch(source.for_each)
        if not match:
            raise ParameterSelectionError(
                f"Manifest parameter source '{source.path}' declares forEach "
                f"as '{source.for_each}', but forEach must be one complete "
                "`{{ site.properties.X }}` expression."
            )

        property_path = match.group(1)
        found, values = self._resolve_property_path_with_presence(
            site.properties,
            property_path,
        )
        if not found:
            return []
        if values is None:
            raise ParameterSelectionError(
                f"Site '{site.name}' sets properties.{property_path} to null. "
                "Omit the key for no selection, or use [] to clear an "
                "inherited selection."
            )
        if isinstance(values, str):
            if values == "none":
                replacement = "[]"
            else:
                replacement = f"[{values}]"
            raise ParameterSelectionError(
                f"Site '{site.name}' sets properties.{property_path} to the "
                f"legacy scalar {values!r}. Replace it with the ordered list "
                f"{replacement}."
            )
        if not isinstance(values, list):
            raise ParameterSelectionError(
                f"Site '{site.name}' properties.{property_path} must be an "
                f"ordered list of resource set names, got "
                f"{type(values).__name__}."
            )

        items: list[str] = []
        for index, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                raise ParameterSelectionError(
                    f"Site '{site.name}' properties.{property_path}[{index}] "
                    "must be a non-empty resource set name."
                )
            item = value.strip()
            if item in items:
                raise ParameterSelectionError(
                    f"Site '{site.name}' properties.{property_path} selects "
                    f"resource set '{item}' more than once."
                )
            items.append(item)

        expanded: list[tuple[str, tuple[str, ...]]] = []
        for item in items:
            declared = source.path
            item_path = declared.replace("{{ item }}", item)
            resolved = manifest.resolve_parameter_path(item_path, site)
            self._require_selected_parameter_file(
                declared,
                resolved,
                site,
                self.workspace,
                "Manifest",
            )
            expanded.append((resolved, source.collections))
        return expanded

    def _load_composition_contract(
        self,
        manifest: Manifest,
    ) -> CompositionContract | None:
        if not manifest.parameter_compositions:
            return None
        contracts = [
            load_contract((self.workspace / path).resolve())
            for path in manifest.parameter_compositions
        ]
        return merge_contracts(contracts)

    def _resolve_manifest_parameters(
        self,
        manifest: Manifest,
        site: Site,
        *,
        validate_step_coverage: bool = True,
    ) -> tuple[
        dict[str, Any],
        CompositionResult | None,
        CompositionContract | None,
    ]:
        """Resolve and compose the manifest parameter tier once per site."""
        manifest_key = (
            str(manifest.source_path)
            if manifest.source_path is not None
            else "<memory>"
        )
        manifest_state = {
            "parameters": manifest.parameters,
            "parameterCompositions": manifest.parameter_compositions,
            "steps": manifest.steps,
        }
        site_state = {
                "name": site.name,
                "subscription": site.subscription,
                "resourceGroup": site.resource_group,
                "location": site.location,
                "labels": site.labels,
                "properties": site.properties,
                "parameters": site.parameters,
        }
        key = (
            manifest_key,
            _cache_state_digest(manifest_state),
            site.name,
            _cache_state_digest(site_state),
            validate_step_coverage,
        )
        with self._composition_cache_lock:
            cached = self._composition_cache.get(key)
        if cached is not None:
            parameters, result, contract = cached
            return copy.deepcopy(parameters), result, contract

        contract = self._load_composition_contract(manifest)
        loaded: list[LoadedParameterSource] = []
        plain_parameters: dict[str, Any] = {}
        resolved_sources: dict[Path, tuple[str, tuple[str, ...]]] = {}
        for source in manifest.parameters:
            for resolved_path, collections in (
                self._expand_manifest_parameter_source(
                    source,
                    manifest,
                    site,
                )
            ):
                full_path = (self.workspace / resolved_path).resolve()
                previous = resolved_sources.get(full_path)
                if previous is not None:
                    previous_path, previous_collections = previous
                    detail = (
                        ""
                        if previous_collections == collections
                        else " with different `collections` metadata"
                    )
                    raise ParameterSelectionError(
                        "Manifest parameter sources "
                        f"{previous_path!r} and {resolved_path!r} resolve to "
                        f"'{resolved_path}' more than once for site "
                        f"'{site.name}'{detail}. Select each source once."
                    )
                resolved_sources[full_path] = (resolved_path, collections)
                if not full_path.exists():
                    if collections:
                        raise CompositionError(
                            f"Governed parameter source '{resolved_path}' "
                            "does not exist."
                        )
                    logger.warning(
                        f"Manifest parameter file not found: {full_path}"
                    )
                    continue
                file_parameters = self.load_parameters(full_path)
                resolved_parameters = self._resolve_template_strings(
                    file_parameters,
                    site,
                )
                if contract is None:
                    if collections:
                        raise CompositionError(
                            f"{resolved_path}: parameter source declares "
                            "`collections` but the manifest has no "
                            "`parameterCompositions` contract."
                        )
                    if contains_composition_metadata(resolved_parameters):
                        raise CompositionError(
                            f"{resolved_path}: `_siteops` requires an active "
                            "`ParameterComposition` contract."
                        )
                    plain_parameters = self._deep_merge(
                        plain_parameters,
                        resolved_parameters,
                    )
                    continue
                loaded.append(
                    LoadedParameterSource(
                        path=Path(resolved_path),
                        data=resolved_parameters,
                        collections=collections,
                    )
                )

        result: CompositionResult | None = None
        if contract is not None:
            result = compose_sources(contract, loaded)
            plain_parameters = result.parameters
            if validate_step_coverage:
                self._validate_composition_step_coverage(
                    manifest,
                    site,
                    contract,
                    result,
                )
        self._validate_composition_lower_tiers(
            manifest,
            site,
            contract,
        )

        cached = (copy.deepcopy(plain_parameters), result, contract)
        with self._composition_cache_lock:
            self._composition_cache[key] = cached
        return copy.deepcopy(plain_parameters), result, contract

    def _validate_composition_lower_tiers(
        self,
        manifest: Manifest,
        site: Site,
        contract: CompositionContract | None,
    ) -> None:
        """Keep composition metadata and governed arrays at manifest level."""
        if contains_composition_metadata(site.parameters):
            raise CompositionError(
                f"Site '{site.name}' carries `_siteops` in site.parameters. "
                "Composition metadata is allowed only in manifest-level "
                "parameter sources."
            )
        governed_paths = (
            {spec.path for spec in contract.collections.values()}
            if contract is not None
            else set()
        )
        governed_site_keys = governed_paths & set(site.parameters)
        if governed_site_keys:
            raise CompositionError(
                f"Site '{site.name}' writes composed collection(s) "
                f"{sorted(governed_site_keys)} in site.parameters. Select a "
                "manifest-level resource set instead."
            )

        for step in manifest.steps:
            if not isinstance(step, DeploymentStep):
                continue
            for param_path in step.parameters:
                resolved_path = manifest.resolve_parameter_path(
                    param_path,
                    site,
                )
                if "{{" in resolved_path:
                    continue
                full_path = (self.workspace / resolved_path).resolve()
                if not full_path.is_file():
                    continue
                file_params = self.load_parameters(full_path)
                if contains_composition_metadata(file_params):
                    raise CompositionError(
                        f"Step parameter file '{resolved_path}' carries "
                        "`_siteops`. Composition metadata is allowed only in "
                        "manifest-level parameter sources."
                    )
                governed_step_keys = governed_paths & set(file_params)
                if governed_step_keys:
                    raise CompositionError(
                        f"Step parameter file '{resolved_path}' writes "
                        f"composed collection(s) "
                        f"{sorted(governed_step_keys)}. Attach resource "
                        "definitions at manifest level."
                    )

    def _validate_composition_step_coverage(
        self,
        manifest: Manifest,
        site: Site,
        contract: CompositionContract,
        result: CompositionResult,
    ) -> None:
        """Require each composed writer to reach an ordered deployment step."""
        collection_steps: dict[str, list[int]] = {
            name: [] for name in contract.collections
        }
        for index, step in enumerate(manifest.steps):
            if not isinstance(step, DeploymentStep):
                continue
            if self._check_step_site_compatibility(step, site) is not None:
                continue
            if not self._evaluate_condition(step.when, site):
                continue
            template_path = (self.workspace / step.template).resolve()
            if not template_path.is_file():
                continue
            accepted = get_template_parameters(str(template_path))
            for name, spec in contract.collections.items():
                if spec.path in accepted:
                    collection_steps[name].append(index)

        for name, composed_entries in result.entries.items():
            if not composed_entries:
                continue
            if not collection_steps[name]:
                raise CompositionError(
                    f"Site '{site.name}' composes collection '{name}', but no "
                    "selected deployment step accepts its parameter path "
                    f"'{contract.collections[name].path}'."
                )
            if len(collection_steps[name]) > 1:
                step_names = [
                    manifest.steps[index].name
                    for index in collection_steps[name]
                ]
                raise CompositionError(
                    f"Site '{site.name}' composes collection '{name}', but "
                    "more than one selected deployment step accepts its "
                    f"parameter path '{contract.collections[name].path}': "
                    f"{step_names}."
                )

        for reference in result.references:
            if (
                reference.target_collection is None
                or reference.external
                or reference.target_source is None
            ):
                continue
            provider_steps = collection_steps[reference.target_collection]
            consumer_steps = collection_steps[reference.source_collection]
            if not provider_steps or not consumer_steps:
                continue
            if min(provider_steps) > min(consumer_steps):
                raise CompositionError(
                    f"Site '{site.name}' rule '{reference.rule_id}' deploys "
                    f"provider collection '{reference.target_collection}' "
                    f"after consumer collection "
                    f"'{reference.source_collection}'."
                )

    def _merge_known_parameters(
        self,
        step: DeploymentStep,
        site: Site,
        manifest: Manifest,
    ) -> dict[str, Any]:
        """Merge parameter tiers and resolve values known from the site."""
        params, _, _ = self._resolve_manifest_parameters(
            manifest,
            site,
        )
        # 2. Merge site-level parameters (site-specific overrides)
        params = self._deep_merge(params, site.get_all_parameters())

        # 3. Merge step-level parameter files (step-specific overrides)
        for param_path in step.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            self._require_selected_parameter_file(param_path, resolved_path, site, self.workspace, "Step")
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                file_params = self.load_parameters(full_path)
                params = self._deep_merge(params, file_params)
            else:
                logger.warning(f"Step parameter file not found: {full_path}")

        # 4. Resolve template variables ({{ site.X }})
        return self._resolve_template_strings(params, site)

    def resolve_parameters(
        self,
        step: DeploymentStep,
        site: Site,
        manifest: Manifest,
        step_outputs: dict[str, dict[str, Any]] | None = None,
        subscription_outputs: (
            dict[str, dict[str, dict[str, Any]]] | None
        ) = None,
    ) -> dict[str, Any]:
        """Resolve and filter parameters for one deployment step.

        Use this for a single step. `build_plan()` and `execute_plan()` cover a
        whole manifest. The step must belong to `manifest`, and each prior-step
        output reference must have supplied outputs.
        """
        if step not in manifest.steps:
            raise ValueError(
                f"Step '{step.name}' is not part of manifest "
                f"'{manifest.name}'."
            )
        step_index = manifest.steps.index(step)
        prior_names = {
            prior.name for prior in manifest.steps[:step_index]
        }
        available_sources: dict[str, OperationIdentity] = {}
        outputs: dict[OperationIdentity, dict[str, Any]] = {}

        for name, values in (step_outputs or {}).items():
            if name not in prior_names:
                continue
            identity = OperationIdentity(target=site.name, step=name)
            available_sources[name] = identity
            outputs[identity] = values

        if subscription_outputs is not None:
            for name, values in subscription_outputs.get(
                site.subscription,
                {},
            ).items():
                if name not in prior_names:
                    continue
                identity = OperationIdentity(target=site.name, step=name)
                available_sources[name] = identity
                outputs[identity] = values

        details, _ = self._prepare_operation_details(
            step,
            site,
            manifest,
            available_sources,
        )
        if (
            not isinstance(details, DeploymentOperation)
            or details.parameters is None
        ):
            raise TypeError(
                "Deployment parameter preparation did not produce a "
                "deployment operation."
            )
        parameters = resolve_plan_value(details.parameters, outputs)
        if not isinstance(parameters, dict):
            raise TypeError(
                "Prepared deployment parameters did not resolve to a mapping."
            )
        self._validate_prepared_parameter_names(details, parameters)
        return parameters

    def _evaluate_condition(
        self,
        condition: str | AnyCondition | None,
        site: Site,
    ) -> bool:
        """Evaluate a step condition against a site.

        Supports:
        - {{ site.labels.key == 'value' }}
        - {{ site.labels.key != 'value' }}
        - {{ site.properties.path == 'value' }}
        - {{ site.properties.path != 'value' }}
        - {{ site.properties.nested.path == 'value' }}
        - {{ site.properties.array[0].field == 'value' }}
        - {{ site.properties.path == true }}
        - {{ site.properties.path == false }}
        - {{ site.properties.path }} (truthy check)
        - any: [<expression>, ...] (passes when any expression passes)

        Truthy check returns True if:
        - Boolean: value is True
        - String: value is not empty and not in ('false', '0') (case-insensitive)
        - Number: value is not 0
        - List/Dict: value is not empty

        Args:
            condition: The condition expression (or None)
            site: The site to evaluate against

        Returns:
            True if condition passes (or is None/empty), False otherwise
        """
        if not condition:
            return True
        if isinstance(condition, AnyCondition):
            return any(
                self._evaluate_condition(expression, site)
                for expression in condition.expressions
            )

        condition = condition.strip()
        match = CONDITION_PATTERN.fullmatch(condition)
        if not match:
            logger.warning(f"Invalid condition syntax: {condition}")
            return True

        field_path = match.group(1)  # e.g., "labels.environment" or "properties.deployOptions.enableSecretSync"
        operator = match.group(2)  # "==" or "!=" or None (for truthy check)
        # Group 3 is quoted string value, group 4 is unquoted boolean
        expected_value = match.group(3) if match.group(3) is not None else match.group(4)

        # Resolve the actual value based on field path
        if field_path.startswith("labels."):
            label_key = field_path[7:]  # Remove "labels." prefix
            actual_value = site.labels.get(label_key, "")
            raw_value = actual_value  # For truthy check
        elif field_path.startswith("properties."):
            prop_path = field_path[11:]  # Remove "properties." prefix
            raw_value = self._resolve_property_path(site.properties, prop_path)
            # Convert to string for comparison (booleans become "true"/"false")
            if raw_value is None:
                actual_value = ""
            elif isinstance(raw_value, bool):
                actual_value = "true" if raw_value else "false"
            else:
                actual_value = str(raw_value)
        else:
            logger.warning(f"Unknown condition field type: {field_path}")
            return True

        # Handle truthy check (no operator)
        if operator is None:
            # Truthy: True for bool True, non-empty strings, non-zero numbers
            if raw_value is None:
                return False
            if isinstance(raw_value, bool):
                return raw_value
            if isinstance(raw_value, str):
                return raw_value.lower() not in ("", "false", "0")
            if isinstance(raw_value, (int, float)):
                return raw_value != 0
            # For lists/dicts, truthy if non-empty
            return bool(raw_value)

        # Handle comparison operators
        if operator == "==":
            return actual_value == expected_value
        elif operator == "!=":
            return actual_value != expected_value

        return True

    @staticmethod
    def _check_step_site_compatibility(step: ManifestStep, site: Site) -> str | None:
        """Check if a step should run for a given site based on scope compatibility.

        Args:
            step: The manifest step to check
            site: The site to check against

        Returns:
            Skip reason string if incompatible, None if compatible
        """
        # Kubectl steps run on any site with a cluster
        if isinstance(step, KubectlStep):
            return None

        # Wait steps gate on an external condition, not a deployment scope.
        # They run on any site (the condition's resourceId carries the full
        # ARM path).
        if isinstance(step, WaitStep):
            return None

        # Check scope/site level compatibility
        is_sub_level = site.is_subscription_level
        if step.scope == "subscription" and not is_sub_level:
            return "subscription-scoped step, site has resource group"
        if step.scope == "resourceGroup" and is_sub_level:
            return "resourceGroup-scoped step, site has no resource group"

        return None

    def _any_subscription_step_would_execute(
        self,
        subscription_steps: list[DeploymentStep],
        rg_level_sites: list[Site],
    ) -> bool:
        """Check if any subscription-scoped step would execute for any RG-level site.

        Used during validation to determine if a subscription-level site is actually
        needed. If all subscription-scoped steps have `when` conditions that evaluate
        to False for all RG-level sites, no subscription-level site is required.

        Args:
            subscription_steps: List of subscription-scoped steps to check
            rg_level_sites: RG-level sites in the subscription

        Returns:
            True if at least one step would execute (needs subscription-level site)
        """
        for step in subscription_steps:
            # No condition = always runs
            if not step.when:
                return True

            # Check if condition passes for any RG-level site
            for site in rg_level_sites:
                if self._evaluate_condition(step.when, site):
                    return True

        return False

    @staticmethod
    def _reportable_deploy_error(
        error: Exception,
        site_name: str = "",
    ) -> str:
        if isinstance(error, CompositionError):
            return report_composition_error(error)
        if isinstance(error, ParameterSelectionError):
            return report_parameter_selection_error(error)
        if (
            isinstance(error, PlanValueResolutionError)
            and is_redaction_enabled()
        ):
            return error.public_message
        return scrub_site_for_output(str(error), site_name) or ""

    @staticmethod
    def _group_sites_by_subscription(
        sites: list[Site],
    ) -> dict[str, tuple[list[Site], list[Site]]]:
        """Group sites by subscription ID, separating subscription-level from RG-level.

        Args:
            sites: List of sites to group

        Returns:
            Dict mapping subscription_id to (subscription_sites, rg_sites) tuple
        """
        groups: dict[str, tuple[list[Site], list[Site]]] = {}

        for site in sites:
            sub_id = site.subscription
            if sub_id not in groups:
                groups[sub_id] = ([], [])

            sub_sites, rg_sites = groups[sub_id]
            if site.is_subscription_level:
                sub_sites.append(site)
            else:
                rg_sites.append(site)

        return groups

    def _print_deployment_summary(
        self,
        results: list[dict[str, Any]],
        total_elapsed: float,
    ) -> None:
        """Print deployment summary.

        Args:
            results: List of deployment results per site
            total_elapsed: Total elapsed time in seconds
        """
        succeeded = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")
        blocked = sum(1 for r in results if r["status"] == "blocked")
        total = len(results)

        print()
        print("=" * 60)
        print("  Deployment Summary")
        print("=" * 60)
        print()

        if is_redaction_enabled():
            summary_parts = [f"{succeeded} succeeded", f"{failed} failed"]
            if blocked:
                summary_parts.append(f"{blocked} blocked")
            print(f"  Total: {', '.join(summary_parts)} ({total} sites)")
            print(f"  Duration: {total_elapsed:.1f}s")

            reportable_errors: dict[str, int] = {}
            for result in results:
                if result["status"] == "success":
                    continue
                error = str(result.get("error", "Unknown error"))
                site = str(result.get("site", ""))
                reportable = (
                    scrub_site_for_output(error, site)
                    if site
                    else scrub_for_output(error)
                ) or "Unknown error"
                reportable_errors[reportable] = (
                    reportable_errors.get(reportable, 0) + 1
                )
            if reportable_errors:
                print()
                print("  Failures:")
                for error, count in reportable_errors.items():
                    print(f"    {count} site(s): {error}")
            print()
            return

        # Results table header
        print(f"  {'SITE':<25} {'STATUS':<10} {'STEPS':<15} {'DURATION':<10}")
        print(f"  {'-'*25} {'-'*10} {'-'*15} {'-'*10}")

        # Sort by site name for consistent output
        for result in sorted(results, key=lambda r: r["site"]):
            site = result["site"]
            result_status = result["status"]
            if result_status == "success":
                status = "+ Success"
            elif result_status == "blocked":
                status = "- Blocked"
            else:
                status = "x Failed"
            steps = f"{result['steps_completed']}/{result['steps_total']}"
            if result.get("steps_skipped"):
                steps += f" ({result['steps_skipped']} skip)"
            duration = f"{result['elapsed']:.1f}s"

            print(f"  {site:<25} {status:<10} {steps:<15} {duration:<10}")

        print()
        summary_parts = [f"{succeeded} succeeded", f"{failed} failed"]
        if blocked:
            summary_parts.append(f"{blocked} blocked")
        print(f"  Total: {', '.join(summary_parts)} ({total} sites)")
        print(f"  Duration: {total_elapsed:.1f}s")
        print()

        # Show errors for failed sites
        failed_results = [r for r in results if r["status"] == "failed"]
        if failed_results:
            print("  Failed Sites:")
            for result in failed_results:
                error = result.get("error", "Unknown error")
                print(f"    [{result['site']}] {scrub_for_output(error)}")
            print()

        # Show blocked sites
        blocked_results = [r for r in results if r["status"] == "blocked"]
        if blocked_results:
            print("  Blocked Sites:")
            for result in blocked_results:
                error = result.get("error", "Blocked due to subscription failure")
                print(f"    [{result['site']}] {scrub_for_output(error)}")
            print()

    def filter_sites(self, selector: dict[str, list[str]]) -> list[Site]:
        """Apply a parsed selector to the workspace's sites.

        Resolves `name=` keys via the trusted-file fast path (path-form,
        basename, or internal name) and falls back to a full-sweep
        attribute match for the remaining selector keys. Used by both
        `resolve_sites` (manifest deploy) and `cmd_sites` (CLI listing)
        so the two commands accept identical selector grammar.

        Args:
            selector: Parsed selector dict (from `parse_selector`).

        Returns:
            Matching Site instances. When the selector has a `name` key,
            results are sorted by `Site.name` and deduplicated so a name
            appearing in both the trusted-file and fallback sweeps is
            returned once. Other selectors return the underlying
            `load_all_sites()` order without an additional sort.
        """
        # When the operator explicitly names sites via `name=X` (or
        # repeated `name=X,name=Y`), route every name whose filename
        # exists in a trusted sites/ directory through load_site() so
        # load errors (broken inherits chain, invalid YAML) propagate
        # instead of being silently swallowed by load_all_sites() and
        # reported as "no sites matched". Names that have no trusted
        # filename match fall through to load_all_sites() so the
        # operator may also select by the site's internal `name:`
        # field, which is permitted to differ from the filename.
        if "name" in selector:
            requested_names = selector["name"]
            # The fast-path treats `_find_trusted_site_file` as the
            # name-key matcher. Re-checking via matches_selector
            # would fail when `name=` is a path-form or internal
            # name and `Site.name` defaults to the basename. Other
            # selector keys still apply.
            other_selector = {k: v for k, v in selector.items() if k != "name"}
            trusted_results: list[Site] = []
            untrusted_names: list[str] = []
            for n in requested_names:
                if self._find_trusted_site_file(n) is not None:
                    site = self.load_site(n)
                    if not other_selector or site.matches_selector(other_selector):
                        trusted_results.append(site)
                else:
                    untrusted_names.append(n)
            # Resolve untrusted names (and any other selector keys)
            # via the full sweep, scoped to the untrusted name set so
            # we do not double-count trusted sites.
            if untrusted_names:
                sweep_selector = {**selector, "name": untrusted_names}
                fallback = [
                    s for s in self.load_all_sites()
                    if s.matches_selector(sweep_selector)
                ]
                seen = {s.name for s in trusted_results}
                for s in fallback:
                    if s.name not in seen:
                        trusted_results.append(s)
                        seen.add(s.name)
            trusted_results.sort(key=lambda s: s.name)
            return trusted_results
        all_sites = self.load_all_sites()
        return [s for s in all_sites if s.matches_selector(selector)]

    def resolve_sites(self, manifest: Manifest, cli_selector: str | None = None) -> list[Site]:
        """Resolve sites from manifest, applying selectors.

        Priority:
        1. CLI --selector overrides everything
        2. Explicit sites list in manifest
        3. Manifest selector (`selector:`, or legacy `siteSelector:`)

        Raises:
            ValueError: When neither the manifest nor the CLI provides any
                site targeting. The manifest is "generic" (no `sites:` and
                no `selector:`) AND no `-l/--selector` was passed. The
                operator must add targeting to the manifest or supply it
                on the CLI.
            FileNotFoundError: When the manifest lists explicit site names
                that do not resolve to any file in the workspace.

        Args:
            manifest: The manifest
            cli_selector: Optional selector from CLI

        Returns:
            List of matching sites
        """
        # Hard error when the manifest declares no targeting AND the operator
        # passed no -l/--selector. Today this would silently resolve to the
        # empty set and cause a confusing "nothing to deploy" exit. Surface
        # the missing-targeting case so the operator can add targeting to the
        # manifest or pass it on the CLI.
        if not cli_selector and not manifest.sites and not manifest.site_selector:
            raise NoTargetingError(
                f"Manifest '{manifest.name}' has no targeting. "
                f"Add `sites:` or `selector:` to the manifest, or pass `-l <key>=<value>` on the CLI."
            )

        # CLI selector requires loading all sites for filtering
        if cli_selector:
            selector = parse_selector(cli_selector)
            return self.filter_sites(selector)

        # Explicit sites list - load only the named sites (most common case)
        if manifest.sites:
            missing = []
            sites = []
            # A site is cached under several keys (basename, canonical path,
            # internal name), so two manifest entries naming the same site yield
            # the same object. Deploying it twice would race two deployments
            # onto one ARM deployment name and collapse both into one result
            # row, so keep the first occurrence and drop the rest.
            seen: set[int] = set()
            duplicates: list[str] = []
            for name in manifest.sites:
                try:
                    site = self.load_site(name)
                except FileNotFoundError:
                    missing.append(name)
                    continue
                if id(site) in seen:
                    duplicates.append(name)
                    continue
                seen.add(id(site))
                sites.append(site)
            if missing:
                names = ", ".join(missing)
                raise FileNotFoundError(
                    f"Site files not found for manifest '{manifest.name}': {names}. "
                    f"Create those site YAML files under `sites/`, or fix the site names listed in the manifest."
                )
            if duplicates:
                logger.warning(
                    f"Manifest '{manifest.name}' names the same site more than once "
                    f"({', '.join(duplicates)}). Deploying it once."
                )
            return sites

        # Site selector requires loading all sites for filtering
        if manifest.site_selector:
            all_sites = self.load_all_sites()
            selector = parse_selector(manifest.site_selector)
            return [s for s in all_sites if s.matches_selector(selector)]

        return []

    def explain_no_match(self, cli_selector: str | None) -> str:
        """Diagnose why a CLI selector matched no workspace sites.

        For each selector key, report what values the operator
        requested and what values are actually present in the
        workspace. Distinguishes a typo (`-l env=prdo`) from an
        empty workspace or a missing label.

        Returns a single-paragraph diagnostic suitable for the
        `cmd_deploy` error path, or a generic message when
        `cli_selector` is None.
        """
        if not cli_selector:
            return "No sites matched the manifest's targeting."
        try:
            sel = parse_selector(cli_selector)
        except SelectorParseError as e:
            return f"CLI selector `-l {cli_selector}` is invalid: {e}"
        all_sites = self.load_all_sites()
        if not all_sites:
            if self.skipped_sites:
                names = ", ".join(name for name, _ in self.skipped_sites)
                return (
                    f"No site could be loaded, so CLI selector "
                    f"`-l {cli_selector}` has nothing to match. "
                    f"{len(self.skipped_sites)} site file(s) were rejected "
                    f"({names}). Fix those files rather than adding a new site."
                )
            return (
                f"No sites in workspace. CLI selector `-l {cli_selector}` "
                f"cannot match. Add a site file under `sites/` or pass "
                f"`--extra-sites-dir` to point at one."
            )
        parts: list[str] = []
        for key, requested in sel.items():
            if key == "name":
                names_in_ws = sorted({s.name for s in all_sites})
                missing = [v for v in requested if v not in names_in_ws]
                if missing:
                    parts.append(
                        f"`name={','.join(missing)}` not found. Workspace "
                        f"site names: {', '.join(names_in_ws)}."
                    )
                else:
                    # Names matched. Another selector key must have filtered
                    # them out. Surface the matched names so the operator does
                    # not get a generic "no match".
                    matched = ",".join(requested)
                    parts.append(
                        f"`name={matched}` matched a workspace site but "
                        f"another selector key filtered it out."
                    )
            else:
                values_in_ws = sorted(
                    {str(s.labels[key]) for s in all_sites if key in s.labels}
                )
                requested_str = ",".join(requested)
                if not values_in_ws:
                    parts.append(
                        f"`{key}={requested_str}` requested but no site "
                        f"declares the `{key}` label."
                    )
                else:
                    # Quoted, so a value that differs only by surrounding
                    # whitespace does not read as identical to the one asked
                    # for.
                    shown = ", ".join(repr(value) for value in values_in_ws)
                    parts.append(
                        f"`{key}={requested_str}` requested. Workspace "
                        f"`{key}` values: {shown}."
                    )
        if not parts:
            return f"CLI selector `-l {cli_selector}` matched no sites."
        return (
            f"CLI selector `-l {cli_selector}` matched no sites. " + " ".join(parts)
        )

    def validate(self, manifest_path: Path, selector: str | None = None) -> list[str]:
        """Validate manifest and return list of errors.

        Checks:
        - Manifest parses correctly
        - Sites exist and match criteria
        - Manifest parameter sources expand to valid workspace files
        - Parameter composition contracts, identities, references, and requirements are valid
        - Governed collections and composition metadata stay at manifest level
        - Template files exist
        - Parameter files exist and are valid YAML (manifest and step level)
        - Kubectl files exist (for local files) and use HTTPS
        - Conditions have valid syntax
        - Required site fields are present
        - Step output references point to valid prior steps (accounting for auto-filtering)

        Args:
            manifest_path: Path to manifest file
            selector: Optional site selector

        Returns:
            List of error messages (empty if valid)
        """
        errors: list[str] = []

        try:
            manifest = Manifest.from_file(manifest_path, workspace_root=self.workspace)
        except Exception as e:
            return [f"Failed to parse manifest: {e}"]

        try:
            sites = self.resolve_sites(manifest, selector)
            selector_parse_failed = False
        except NoTargetingError:
            # Generic library or partial manifest. Skip site-dependent
            # checks since they require a concrete site. `cmd_deploy`
            # surfaces the same condition as a hard error.
            sites = []
            selector_parse_failed = False
        except SelectorParseError as e:
            # CLI selector failed to parse. Append the parse error
            # (operator sees it alongside other manifest issues in one
            # diagnostic pass) but suppress the no-match diagnostic
            # below since the parse error is the higher-signal cause.
            errors.append(
                "Invalid site selector."
                if is_redaction_enabled()
                else str(e)
            )
            sites = []
            selector_parse_failed = True
        except ValueError as e:
            # Site-resolution failure (cycle, overlay-rename, missing
            # field, etc.). Append and continue so other manifest
            # issues still surface in this pass.
            errors.append(report_site_load_error(e))
            sites = []
            selector_parse_failed = False
        except FileNotFoundError as e:
            # Manifest `sites:` entry without a workspace file.
            errors.append(report_site_load_error(e))
            sites = []
            selector_parse_failed = False
        if not sites and (manifest.sites or manifest.site_selector or selector):
            if selector and not selector_parse_failed:
                # Rich diagnostic when CLI selector knocked everything
                # out and the selector itself parsed cleanly.
                errors.append(
                    "CLI selector matched no sites."
                    if is_redaction_enabled()
                    else self.explain_no_match(selector)
                )
            elif not selector:
                errors.append("No sites matched the specified criteria")

        # Validate manifest-level parameter files
        if sites:
            for site in sites:
                source_error = False
                for source in manifest.parameters:
                    try:
                        expanded = self._expand_manifest_parameter_source(
                            source,
                            manifest,
                            site,
                        )
                    except ParameterSelectionError as e:
                        errors.append(report_parameter_selection_error(e))
                        source_error = True
                        continue
                    for resolved, _ in expanded:
                        full_path = (self.workspace / resolved).resolve()
                        if not full_path.is_file():
                            errors.append(
                                "Manifest parameter file not found: "
                                f"{resolved}"
                            )
                            source_error = True
                            continue
                        try:
                            self.load_parameters(full_path)
                        except Exception as e:
                            errors.append(
                                f"Invalid manifest parameter file "
                                f"{resolved}: {e}"
                            )
                            source_error = True
                if source_error:
                    continue
                try:
                    self._resolve_manifest_parameters(manifest, site)
                except CompositionError as e:
                    errors.append(report_composition_error(e))
                except ParameterSelectionError as e:
                    errors.append(report_parameter_selection_error(e))
                except (
                    ValueError,
                    yaml.YAMLError,
                    OSError,
                ) as e:
                    errors.append(str(e))
        else:
            for source in manifest.parameters:
                raw_path = source if isinstance(source, str) else source.path
                if "{{" in raw_path or (
                    isinstance(source, ParameterSource)
                    and source.for_each is not None
                ):
                    continue
                full_path = (self.workspace / raw_path).resolve()
                if not full_path.exists():
                    errors.append(
                        f"Manifest parameter file not found: {raw_path}"
                    )
                else:
                    try:
                        self.load_parameters(full_path)
                    except Exception as e:
                        errors.append(
                            f"Invalid manifest parameter file {raw_path}: {e}"
                        )

        # Build step name lookup for output reference validation
        all_step_names = {step.name for step in manifest.steps}

        # Check for duplicate step names
        seen_names: set[str] = set()
        for step in manifest.steps:
            if step.name in seen_names:
                errors.append(f"Duplicate step name: '{step.name}'")
            seen_names.add(step.name)

        for step_index, step in enumerate(manifest.steps):
            # Steps that execute before this one (valid sources for output references)
            prior_step_names = {s.name for s in manifest.steps[:step_index]}

            if isinstance(step, KubectlStep):
                # Validate kubectl files (skip URLs and templates)
                for file_path in step.files:
                    if file_path.startswith("https://") or "{{" in file_path:
                        continue
                    if file_path.lower().startswith("http://"):
                        errors.append(f"HTTP URLs not allowed (use HTTPS): {file_path} (step: {step.name})")
                        continue
                    full_path = (self.workspace / file_path).resolve()
                    if not full_path.exists():
                        errors.append(f"Kubectl file not found: {file_path} (step: {step.name})")
            elif isinstance(step, WaitStep):
                # Validate that step-output references in the condition point
                # only to prior steps. A wait produces no outputs, so a self or
                # later reference can never resolve.
                condition = step.condition
                condition_strings: list[str] = []
                if condition.type == "arm-tag":
                    condition_strings = [
                        condition.resource_id,
                        condition.tag_key,
                        condition.expected_value,
                    ]
                for condition_string in condition_strings:
                    for match in STEP_OUTPUT_PATTERN.finditer(condition_string):
                        ref_step = match.group(1)
                        if ref_step not in all_step_names:
                            errors.append(
                                f"Step '{step.name}' wait condition references unknown step '{ref_step}'"
                            )
                        elif ref_step not in prior_step_names:
                            errors.append(
                                f"Step '{step.name}' wait condition references step '{ref_step}' "
                                f"which does not execute before it"
                            )
            else:
                template_path = (self.workspace / step.template).resolve()

                if not template_path.exists():
                    errors.append(f"Template not found: {step.template}")
                    continue

                for param_path in step.parameters:
                    if "{{" in param_path:
                        # Dynamic path: validate resolved path for each site
                        for site in sites:
                            resolved = manifest.resolve_parameter_path(param_path, site)
                            try:
                                self._require_selected_parameter_file(
                                    param_path, resolved, site, self.workspace, "Step"
                                )
                            except ParameterSelectionError as e:
                                errors.append(
                                    f"{report_parameter_selection_error(e)} "
                                    f"(step: {step.name})"
                                )
                                continue
                            try:
                                params = self.load_parameters(
                                    (self.workspace / resolved).resolve()
                                )
                                errors.extend(
                                    self._validate_output_references(
                                        params,
                                        step.name,
                                        prior_step_names,
                                        all_step_names,
                                        resolved,
                                        None,
                                    )
                                )
                            except Exception as e:
                                errors.append(f"Invalid parameter file {resolved}: {e}")
                        continue

                    full_path = (self.workspace / param_path).resolve()
                    if not full_path.exists():
                        errors.append(f"Parameter file not found: {param_path} (step: {step.name})")
                    else:
                        try:
                            params = self.load_parameters(full_path)

                            # Check if params contain self-references before expensive template parsing
                            has_self_ref = self._contains_self_reference(params, step.name)

                            template_params: frozenset | None = None
                            if has_self_ref:
                                # Only extract template params when needed for self-reference validation
                                try:
                                    from siteops.executor import get_template_parameters

                                    template_params = frozenset(get_template_parameters(str(template_path)))
                                except Exception as e:
                                    logger.debug(f"Could not extract template params for '{step.name}': {e}")
                                    # Continue without template params - validation will be conservative

                            # Validate step output references with auto-filter awareness
                            errors.extend(
                                self._validate_output_references(
                                    params,
                                    step.name,
                                    prior_step_names,
                                    all_step_names,
                                    param_path,
                                    template_params,
                                )
                            )
                        except Exception as e:
                            errors.append(f"Invalid parameter file {param_path}: {e}")

        if not manifest.steps:
            errors.append("Manifest has no steps defined")

        for step in manifest.steps:
            if step.when and isinstance(step.when, str):
                if not CONDITION_PATTERN.fullmatch(step.when.strip()):
                    errors.append(
                        f"Invalid 'when' condition in step '{step.name}': "
                        f"{step.when}"
                    )

        for step in manifest.steps:
            if isinstance(step, DeploymentStep) and step.scope == "resourceGroup":
                for site in sites:
                    # Subscription-level sites are exempt - they intentionally skip RG-scoped steps
                    if site.is_subscription_level:
                        continue
                    if not site.resource_group:
                        errors.append(f"Site '{site.name}' missing 'resourceGroup' required by step '{step.name}'")

        # Validate subscription-scoped steps
        subscription_steps = [
            step for step in manifest.steps if isinstance(step, DeploymentStep) and step.scope == "subscription"
        ]

        if subscription_steps and sites:
            # Group sites by subscription to check for subscription-level sites
            site_groups = self._group_sites_by_subscription(sites)

            # Check that each subscription has exactly one subscription-level site
            for sub_id, (sub_level_sites, rg_level_sites) in site_groups.items():
                if not sub_level_sites and rg_level_sites:
                    # RG-level sites exist but no subscription-level site.
                    # Check if any subscription-scoped step would actually execute
                    # based on its `when` condition evaluated against RG-level sites.
                    needs_subscription_site = self._any_subscription_step_would_execute(
                        subscription_steps, rg_level_sites
                    )

                    if needs_subscription_site:
                        site_names = ", ".join(s.name for s in rg_level_sites[:3])
                        if len(rg_level_sites) > 3:
                            site_names += f"... and {len(rg_level_sites) - 3} more"
                        errors.append(
                            f"Subscription '{_reportable_subscription(sub_id)}' has RG-level sites ({site_names}) "
                            f"but no subscription-level site for subscription-scoped steps"
                        )
                elif len(sub_level_sites) > 1:
                    # Multiple subscription-level sites for same subscription
                    site_names = ", ".join(s.name for s in sub_level_sites)
                    errors.append(
                        f"Subscription '{_reportable_subscription(sub_id)}' has multiple subscription-level sites: {site_names}. "
                        f"Only one subscription-level site per subscription is allowed."
                    )

        if is_redaction_enabled():
            site_names = {
                site.name for site in sites
            } | {
                name for name, _ in self.skipped_sites
            }
            for site_name in sorted(site_names, key=len, reverse=True):
                errors = [
                    scrub_site_for_output(error, site_name) or ""
                    for error in errors
                ]

        return errors

    def _contains_self_reference(self, value: Any, step_name: str) -> bool:
        """Check if a value contains a self-reference to the given step.

        This is a quick check to avoid expensive template parameter extraction
        when there are no self-references to validate.

        Args:
            value: Parameter value to check (recursively handles dict/list/str)
            step_name: Name of the current step

        Returns:
            True if value contains {{ steps.<step_name>.outputs... }}
        """
        if isinstance(value, dict):
            return any(self._contains_self_reference(v, step_name) for v in value.values())
        elif isinstance(value, list):
            return any(self._contains_self_reference(item, step_name) for item in value)
        elif isinstance(value, str):
            # Quick string check before regex
            pattern = f"steps.{step_name}."
            if pattern not in value:
                return False
            for match in STEP_OUTPUT_PATTERN.finditer(value):
                if match.group(1) == step_name:
                    return True
        return False

    def _composition_source_origins(
        self,
        manifest: Manifest,
        site: Site,
    ) -> dict[Path, str]:
        """Map selected composition files to what chose them."""
        try:
            loaded_site, provenance = self.load_site_with_provenance(site.name)
            runtime_override = (
                loaded_site.properties != site.properties
                or loaded_site.parameters != site.parameters
                or loaded_site.labels != site.labels
            )
        except (FileNotFoundError, ValueError):
            provenance = {}
            runtime_override = True

        result: dict[Path, str] = {}
        for source in manifest.parameters:
            if not isinstance(source, ParameterSource) or not source.collections:
                continue
            if source.for_each is None:
                origin = (
                    self._origin_label(source.declared_in)
                    if source.declared_in is not None
                    else "manifest"
                )
            else:
                match = FOR_EACH_SITE_PROPERTY_PATTERN.fullmatch(
                    source.for_each
                )
                property_path = match.group(1) if match else ""
                origin = (
                    "<runtime site state>"
                    if runtime_override
                    else provenance.get(
                        f"properties.{property_path}",
                        "<site default>",
                    )
                )
            for resolved, _ in self._expand_manifest_parameter_source(
                source,
                manifest,
                site,
            ):
                result[Path(resolved)] = origin
        return result

    @staticmethod
    def _reportable_composition_origin(origin: str) -> str:
        """Hide machine-specific roots from plan provenance."""
        path = Path(origin)
        if path.is_absolute():
            return f"<extra-sites>/{path.name}"
        return origin

    def _validate_output_references(
        self,
        value: Any,
        current_step: str,
        prior_steps: set,
        all_steps: set,
        source_file: Path,
        template_params: frozenset | None = None,
        _current_key: str | None = None,
    ) -> list[str]:
        """Validate step output references in parameter values.

        Finds all {{ steps.<name>.outputs.<path> }} patterns and validates that:
        1. The referenced step exists in the manifest
        2. The referenced step executes before the current step
        3. Self-references are only flagged if the template accepts that parameter
           (otherwise auto-filtering will remove it)

        Args:
            value: Parameter value to check (recursively handles dict/list/str)
            current_step: Name of the step using these parameters
            prior_steps: Set of step names that execute before current_step
            all_steps: Set of all step names in the manifest
            source_file: Parameter file path for error messages
            template_params: Set of parameter names the template accepts.
                            If None, self-references are always flagged (conservative).
            _current_key: Internal - tracks the top-level parameter key during recursion

        Returns:
            List of validation error messages
        """
        errors: list[str] = []

        if isinstance(value, dict):
            for key, val in value.items():
                # Track top-level key for self-reference validation
                top_level_key = _current_key if _current_key is not None else key
                errors.extend(
                    self._validate_output_references(
                        val,
                        current_step,
                        prior_steps,
                        all_steps,
                        source_file,
                        template_params,
                        top_level_key,
                    )
                )
        elif isinstance(value, list):
            for item in value:
                errors.extend(
                    self._validate_output_references(
                        item,
                        current_step,
                        prior_steps,
                        all_steps,
                        source_file,
                        template_params,
                        _current_key,
                    )
                )
        elif isinstance(value, str):
            for match in STEP_OUTPUT_PATTERN.finditer(value):
                ref_step = match.group(1)

                if ref_step not in all_steps:
                    errors.append(f"Step '{current_step}' references unknown step '{ref_step}' in {source_file}")
                elif ref_step == current_step:
                    # Self-reference: only error if template actually accepts this parameter
                    if template_params is None:
                        # No template info available - be conservative and flag it
                        errors.append(f"Step '{current_step}' cannot reference its own outputs in {source_file}")
                    elif _current_key is not None and _current_key in template_params:
                        # Template accepts this parameter - genuine circular dependency
                        errors.append(
                            f"Step '{current_step}' cannot reference its own outputs "
                            f"for parameter '{_current_key}' in {source_file}"
                        )
                    # else: auto-filtering will remove this parameter, so no error
                elif ref_step not in prior_steps:
                    errors.append(
                        f"Step '{current_step}' references step '{ref_step}' which runs later in {source_file}"
                    )

        return errors

    @staticmethod
    def _plan_resource_identity(
        contract: CompositionContract,
        collection: str,
        identity: tuple[str, ...],
    ) -> ResourceIdentity:
        spec = contract.collections[collection]
        return ResourceIdentity(
            collection=collection,
            components=tuple(
                (field.path, value)
                for field, value in zip(
                    spec.identity,
                    identity,
                    strict=True,
                )
            ),
        )

    def _build_plan_composition(
        self,
        manifest: Manifest,
        site: Site,
        composition: CompositionResult,
        contract: CompositionContract,
    ) -> PlanComposition:
        source_origins = self._composition_source_origins(manifest, site)
        sources = tuple(
            CompositionSource(
                path=source.path,
                selected_by=self._reportable_composition_origin(
                    source_origins.get(source.path, "<unknown>")
                ),
            )
            for source in composition.sources
        )

        resources: list[CompositionResource] = []
        for collection in contract.collections:
            resources.extend(
                CompositionResource(
                    identity=self._plan_resource_identity(
                        contract,
                        collection,
                        entry.identity,
                    ),
                    disposition=ResourceDisposition.APPLY,
                    source=entry.source,
                )
                for entry in composition.entries[collection]
            )
            resources.extend(
                CompositionResource(
                    identity=self._plan_resource_identity(
                        contract,
                        collection,
                        entry.identity,
                    ),
                    disposition=ResourceDisposition.EXTERNAL,
                    source=entry.source,
                    reason=entry.reason,
                )
                for entry in composition.external[collection]
            )

        references = tuple(
            CompositionReference(
                rule_id=reference.rule_id,
                source=self._plan_resource_identity(
                    contract,
                    reference.source_collection,
                    reference.source_identity,
                ),
                source_bindings=reference.source_bindings,
                target=(
                    self._plan_resource_identity(
                        contract,
                        reference.target_collection,
                        reference.target_identity,
                    )
                    if reference.target_collection is not None
                    and reference.target_identity is not None
                    else None
                ),
                target_member_name=reference.target_member_name,
                target_member_identity=reference.target_member_identity,
                external=reference.external,
                unverified_reason=reference.unverified_reason,
            )
            for reference in composition.references
        )
        requirements = tuple(
            CompositionRequirement(
                identity=self._plan_resource_identity(
                    contract,
                    requirement.collection,
                    requirement.identity,
                ),
                source=requirement.source,
            )
            for requirement in composition.requirements
        )
        return PlanComposition(
            sources=sources,
            resources=tuple(resources),
            references=references,
            requirements=requirements,
        )

    @staticmethod
    def _build_plan_step(
        step: ManifestStep,
        sequence: int,
    ) -> PlanStep:
        condition = format_when_condition(step.when) if step.when else None
        if isinstance(step, KubectlStep):
            kind = OperationKind.KUBECTL
            scope = OperationScope.TARGET
            details = KubectlOperation(
                input_status=InputStatus.DESCRIBED,
                operation=step.operation,
                cluster_name=LiteralValue(step.arc.name),
                cluster_resource_group=LiteralValue(
                    step.arc.resource_group
                ),
                files=tuple(LiteralValue(path) for path in step.files),
            )
        elif isinstance(step, WaitStep):
            kind = OperationKind.WAIT
            scope = OperationScope.TARGET
            details = ArmTagWaitOperation(
                input_status=InputStatus.DESCRIBED,
                resource_id=LiteralValue(step.condition.resource_id),
                tag_key=LiteralValue(step.condition.tag_key),
                expected_value=LiteralValue(
                    step.condition.expected_value
                ),
                failure_pattern=(
                    LiteralValue(step.condition.failure_pattern)
                    if step.condition.failure_pattern is not None
                    else None
                ),
                timeout_minutes=step.timeout_minutes,
                poll_interval_seconds=step.poll_interval_seconds,
            )
        else:
            kind = OperationKind.DEPLOYMENT
            scope = OperationScope(step.scope)
            details = DeploymentOperation(
                template=Path(step.template),
                input_status=InputStatus.DESCRIBED,
            )
        return PlanStep(
            name=step.name,
            sequence=sequence,
            kind=kind,
            scope=scope,
            details=details,
            condition=condition,
        )

    @staticmethod
    def _available_plan_sources(
        site: Site,
        prior_steps: list[ManifestStep],
        operations: dict[OperationIdentity, PreparedOperation],
        subscription_targets: dict[str, str],
    ) -> dict[str, OperationIdentity]:
        available: dict[str, OperationIdentity] = {}
        for step in prior_steps:
            if not isinstance(step, DeploymentStep):
                continue
            if step.scope == "subscription":
                target_name = (
                    site.name
                    if site.is_subscription_level
                    else subscription_targets.get(site.subscription)
                )
            elif site.is_subscription_level:
                target_name = None
            else:
                target_name = site.name
            if target_name is None:
                continue
            identity = OperationIdentity(
                target=target_name,
                step=step.name,
            )
            operation = operations.get(identity)
            if (
                operation is not None
                and operation.disposition is PlanDisposition.EXECUTE
                and isinstance(
                    operation.details,
                    DeploymentOperation,
                )
                and operation.details.input_status is InputStatus.PREPARED
            ):
                available[step.name] = identity
        return available

    def _prepare_operation_details(
        self,
        step: ManifestStep,
        site: Site,
        manifest: Manifest,
        available_sources: dict[str, OperationIdentity],
    ) -> tuple[
        DeploymentOperation | KubectlOperation | ArmTagWaitOperation,
        tuple[DataReference, ...],
    ]:
        if isinstance(step, DeploymentStep):
            params = self._merge_known_parameters(step, site, manifest)
            template_path = (self.workspace / step.template).resolve()
            accepted_parameters = get_template_parameters(
                str(template_path)
            )
            filtered: dict[Any, Any] = {}
            unused: list[Any] = []
            for key, value in params.items():
                if (
                    isinstance(key, str)
                    and _carries_template(key)
                ) or key in accepted_parameters:
                    filtered[key] = value
                else:
                    unused.append(key)
            if unused:
                logger.debug(
                    scrub_for_output(
                        f"Step '{step.name}': Filtered out parameters not "
                        f"in template: {unused}"
                    )
                )
            classified = classify_plan_value(
                filtered,
                available_sources,
                f"{site.name}.{step.name}.parameters",
            )
            if not isinstance(classified, MappingValue):
                raise TypeError(
                    "Prepared deployment parameters must be a mapping."
                )
            return (
                DeploymentOperation(
                    template=template_path,
                    input_status=InputStatus.PREPARED,
                    parameters=classified,
                    accepted_parameters=tuple(
                        sorted(accepted_parameters)
                    ),
                ),
                collect_data_references(classified),
            )

        if isinstance(step, KubectlStep):
            cluster_name = classify_plan_value(
                self._resolve_template_strings(step.arc.name, site),
                available_sources,
                f"{site.name}.{step.name}.clusterName",
            )
            cluster_resource_group = classify_plan_value(
                self._resolve_template_strings(
                    step.arc.resource_group,
                    site,
                ),
                available_sources,
                f"{site.name}.{step.name}.clusterResourceGroup",
            )
            files = tuple(
                classify_plan_value(
                    self._resolve_template_strings(path, site),
                    available_sources,
                    f"{site.name}.{step.name}.files[{index}]",
                )
                for index, path in enumerate(step.files)
            )
            details = KubectlOperation(
                input_status=InputStatus.PREPARED,
                operation=step.operation,
                cluster_name=cluster_name,
                cluster_resource_group=cluster_resource_group,
                files=files,
            )
            references: list[DataReference] = []
            for value in (
                cluster_name,
                cluster_resource_group,
                *files,
            ):
                for reference in collect_data_references(value):
                    if reference not in references:
                        references.append(reference)
            return details, tuple(references)

        condition = step.condition
        resource_id = classify_plan_value(
            self._resolve_template_strings(condition.resource_id, site),
            available_sources,
            f"{site.name}.{step.name}.resourceId",
        )
        tag_key = classify_plan_value(
            self._resolve_template_strings(condition.tag_key, site),
            available_sources,
            f"{site.name}.{step.name}.tagKey",
        )
        expected_value = classify_plan_value(
            self._resolve_template_strings(
                condition.expected_value,
                site,
            ),
            available_sources,
            f"{site.name}.{step.name}.expectedValue",
        )
        failure_pattern = (
            classify_plan_value(
                self._resolve_template_strings(
                    condition.failure_pattern,
                    site,
                ),
                available_sources,
                f"{site.name}.{step.name}.failurePattern",
            )
            if condition.failure_pattern is not None
            else None
        )
        details = ArmTagWaitOperation(
            input_status=InputStatus.PREPARED,
            resource_id=resource_id,
            tag_key=tag_key,
            expected_value=expected_value,
            failure_pattern=failure_pattern,
            timeout_minutes=step.timeout_minutes,
            poll_interval_seconds=step.poll_interval_seconds,
        )
        references = []
        for value in (
            resource_id,
            tag_key,
            expected_value,
            failure_pattern,
        ):
            if value is None:
                continue
            for reference in collect_data_references(value):
                if reference not in references:
                    references.append(reference)
        return details, tuple(references)

    def _prepare_plan_targets(
        self,
        manifest: Manifest,
        sites: list[Site],
        targets: list[PreparedTarget],
    ) -> tuple[list[PreparedTarget], list[PlanDiagnostic]]:
        site_by_name = {site.name: site for site in sites}
        subscription_targets = {
            target.subscription: target.name
            for target in targets
            if target.kind is TargetKind.SUBSCRIPTION
        }
        operations = {
            operation.identity: operation
            for target in targets
            for operation in target.operations
        }
        prepared_targets: dict[str, PreparedTarget] = {}
        diagnostics: list[PlanDiagnostic] = []

        ordered_targets = [
            target
            for target in targets
            if target.kind is TargetKind.SUBSCRIPTION
        ] + [
            target
            for target in targets
            if target.kind is not TargetKind.SUBSCRIPTION
        ]
        for target in ordered_targets:
            site = site_by_name[target.name]
            target_diagnostics = list(target.diagnostics)
            target_failed = bool(target_diagnostics)
            prepared_operations: list[PreparedOperation] = []
            for index, (source_step, operation) in enumerate(
                zip(
                    manifest.steps,
                    target.operations,
                    strict=True,
                )
            ):
                if operation.disposition is not PlanDisposition.EXECUTE:
                    prepared_operations.append(operation)
                    operations[operation.identity] = operation
                    continue
                if target_failed:
                    blocked = replace(
                        operation,
                        disposition=PlanDisposition.BLOCKED,
                        skip_reason=PlanSkipReason(
                            code=SkipReasonCode.TARGET_PREPARATION_FAILED,
                            detail="Target preparation failed.",
                        ),
                    )
                    prepared_operations.append(blocked)
                    operations[operation.identity] = blocked
                    continue

                available_sources = self._available_plan_sources(
                    site,
                    manifest.steps[:index],
                    operations,
                    subscription_targets,
                )
                try:
                    details, references = self._prepare_operation_details(
                        source_step,
                        site,
                        manifest,
                        available_sources,
                    )
                    prepared = replace(
                        operation,
                        details=details,
                        data_references=references,
                    )
                except (
                    CompositionError,
                    ParameterSelectionError,
                    TypeError,
                    ValueError,
                    FileNotFoundError,
                    OSError,
                    yaml.YAMLError,
                ) as error:
                    diagnostic = PlanDiagnostic(
                        code="operation-preparation.invalid",
                        severity=DiagnosticSeverity.ERROR,
                        summary="Operation preparation failed.",
                        detail=str(error),
                    )
                    target_diagnostics.append(diagnostic)
                    diagnostics.append(diagnostic)
                    target_failed = True
                    prepared = replace(
                        operation,
                        disposition=PlanDisposition.BLOCKED,
                        skip_reason=PlanSkipReason(
                            code=SkipReasonCode.TARGET_PREPARATION_FAILED,
                            detail="Target preparation failed.",
                        ),
                    )
                prepared_operations.append(prepared)
                operations[operation.identity] = prepared

            prepared_targets[target.name] = replace(
                target,
                operations=tuple(prepared_operations),
                diagnostics=tuple(target_diagnostics),
            )

        return (
            [prepared_targets[target.name] for target in targets],
            diagnostics,
        )

    def build_plan(
        self,
        manifest_path: Path,
        selector: str | None = None,
        *,
        intent: PlanIntent = PlanIntent.DESCRIBE,
        manifest: Manifest | None = None,
        sites: list[Site] | None = None,
        parallel_override: int | None = None,
    ) -> PlanBuildResult:
        """Build one immutable plan without printing or deploying.

        Describe intent reads workspace inputs. Executable intent also reads
        template schemas.
        """
        if manifest is None:
            manifest = Manifest.from_file(
                manifest_path,
                workspace_root=self.workspace,
            )
        if sites is None:
            sites = self.resolve_sites(manifest, selector)
        preexisting_target_diagnostics: dict[
            str,
            list[PlanDiagnostic],
        ] = {}
        if intent is PlanIntent.EXECUTABLE:
            site_groups = self._group_sites_by_subscription(sites)
            subscription_steps = [
                step
                for step in manifest.steps
                if isinstance(step, DeploymentStep)
                and step.scope == "subscription"
            ]
            for subscription, (
                subscription_sites,
                resource_group_sites,
            ) in site_groups.items():
                if len(subscription_sites) > 1:
                    names = ", ".join(
                        site.name for site in subscription_sites
                    )
                    raise MultipleSubscriptionSitesError(
                        f"Subscription "
                        f"'{_reportable_subscription(subscription)}' has "
                        f"multiple subscription-level sites: {names}. Only "
                        "one subscription-level site per subscription is "
                        "allowed."
                    )
                if (
                    not subscription_sites
                    and resource_group_sites
                    and self._any_subscription_step_would_execute(
                        subscription_steps,
                        resource_group_sites,
                    )
                ):
                    names = ", ".join(
                        site.name for site in resource_group_sites[:3]
                    )
                    if len(resource_group_sites) > 3:
                        names += (
                            f"... and {len(resource_group_sites) - 3} more"
                        )
                    for site in resource_group_sites:
                        preexisting_target_diagnostics.setdefault(
                            site.name,
                            [],
                        ).append(
                            PlanDiagnostic(
                                code="subscription-target.missing",
                                severity=DiagnosticSeverity.ERROR,
                                summary=(
                                    "A required subscription target is "
                                    "missing."
                                ),
                                detail=(
                                    f"Subscription "
                                    f"'{_reportable_subscription(subscription)}' "
                                    f"has RG-level sites ({names}) but no "
                                    "subscription-level site for "
                                    "subscription-scoped steps."
                                ),
                                serialized_detail=(
                                    "Add one subscription-level site for "
                                    "each selected subscription that needs "
                                    "subscription-scoped steps."
                                ),
                            )
                        )
        plan_steps = tuple(
            self._build_plan_step(step, sequence)
            for sequence, step in enumerate(manifest.steps, 1)
        )

        targets: list[PreparedTarget] = []
        diagnostics: list[PlanDiagnostic] = []
        for site in sites:
            target_diagnostics = list(
                preexisting_target_diagnostics.get(site.name, ())
            )
            plan_composition: PlanComposition | None = None
            if manifest.parameter_compositions:
                try:
                    _, composition, contract = (
                        self._resolve_manifest_parameters(
                            manifest,
                            site,
                            validate_step_coverage=(
                                intent is PlanIntent.EXECUTABLE
                            ),
                        )
                    )
                    if composition is not None and contract is not None:
                        plan_composition = self._build_plan_composition(
                            manifest,
                            site,
                            composition,
                            contract,
                        )
                except CompositionError as error:
                    target_diagnostics.append(
                        PlanDiagnostic(
                            code="composition.invalid",
                            severity=DiagnosticSeverity.ERROR,
                            summary=(
                                "Resource composition failed. Set "
                                "SITEOPS_REDACT_OUTPUT=0, then rerun the "
                                "command locally for source and identity "
                                "details."
                            ),
                            detail=str(error),
                        )
                    )
                except ParameterSelectionError as error:
                    target_diagnostics.append(
                        PlanDiagnostic(
                            code="parameter-selection.invalid",
                            severity=DiagnosticSeverity.ERROR,
                            summary=(
                                "Parameter file selection failed. Set "
                                "SITEOPS_REDACT_OUTPUT=0, then rerun the "
                                "command locally for site and path details."
                            ),
                            detail=str(error),
                        )
                    )

            operations: list[PreparedOperation] = []
            target_failed = bool(target_diagnostics)
            for source_step, plan_step in zip(
                manifest.steps,
                plan_steps,
                strict=True,
            ):
                skip_reason: PlanSkipReason | None = None
                if target_failed:
                    disposition = PlanDisposition.BLOCKED
                    skip_reason = PlanSkipReason(
                        code=SkipReasonCode.TARGET_PREPARATION_FAILED,
                        detail="Target preparation failed.",
                    )
                else:
                    compatibility = self._check_step_site_compatibility(
                        source_step,
                        site,
                    )
                    if compatibility is not None:
                        disposition = PlanDisposition.SKIP
                        skip_reason = PlanSkipReason(
                            code=SkipReasonCode.SCOPE_MISMATCH,
                            detail=compatibility,
                        )
                    elif not self._evaluate_condition(
                        source_step.when,
                        site,
                    ):
                        disposition = PlanDisposition.SKIP
                        skip_reason = PlanSkipReason(
                            code=SkipReasonCode.CONDITION_FALSE,
                            detail=(
                                "Condition not met: "
                                f"{format_when_condition(source_step.when)}"
                            ),
                        )
                    else:
                        disposition = PlanDisposition.EXECUTE
                operations.append(
                    PreparedOperation(
                        identity=OperationIdentity(
                            target=site.name,
                            step=source_step.name,
                        ),
                        step=plan_step,
                        disposition=disposition,
                        details=plan_step.details,
                        skip_reason=skip_reason,
                    )
                )

            target = PreparedTarget(
                name=site.name,
                kind=(
                    TargetKind.SUBSCRIPTION
                    if site.is_subscription_level
                    else TargetKind.RESOURCE_GROUP
                ),
                subscription=site.subscription,
                resource_group=site.resource_group or None,
                location=site.location,
                operations=tuple(operations),
                composition=plan_composition,
                diagnostics=tuple(target_diagnostics),
            )
            targets.append(target)
            diagnostics.extend(target_diagnostics)

        if intent is PlanIntent.EXECUTABLE:
            targets, preparation_diagnostics = (
                self._prepare_plan_targets(
                    manifest,
                    sites,
                    targets,
                )
            )
            diagnostics.extend(preparation_diagnostics)

        effective_parallel = (
            parallel_override
            if parallel_override is not None
            else manifest.parallel.sites
        )
        plan = DeploymentPlan(
            manifest_name=manifest.name,
            source_path=manifest.source_path or manifest_path,
            intent=intent,
            description=manifest.description,
            max_parallel_sites=effective_parallel,
            steps=plan_steps,
            targets=tuple(targets),
            cli_selector=selector,
            manifest_selector=manifest.site_selector,
            composition_enabled=bool(manifest.parameter_compositions),
        )
        return PlanBuildResult(
            status=(
                PlanStatus.INVALID
                if diagnostics
                else PlanStatus.PLANNED
            ),
            executable=(
                intent is PlanIntent.EXECUTABLE
                and not diagnostics
            ),
            plan=plan,
            diagnostics=tuple(diagnostics),
        )

    def show_plan(
        self,
        manifest_path: Path,
        selector: str | None = None,
    ) -> None:
        """Print the plain plan without compiling templates or deploying."""
        result = self.build_plan(manifest_path, selector)
        print(
            render_plain_plan(
                result,
                redacted=is_redaction_enabled(),
            ),
            end="",
        )

    @staticmethod
    def _prepared_deployment_name(
        manifest_name: str,
        target_name: str,
        step_name: str,
        timestamp: str,
    ) -> str:
        base_name = f"{manifest_name}-{target_name}-{step_name}"
        max_length = 64
        timestamp_length = 14
        hash_length = 10
        max_base = max_length - timestamp_length - 1
        if len(base_name) > max_base:
            name_hash = hashlib.sha256(
                base_name.encode()
            ).hexdigest()[:hash_length]
            base_name = (
                f"{base_name[:max_base - hash_length - 1]}-{name_hash}"
            )
        return f"{base_name}-{timestamp}"

    @staticmethod
    def _resolved_plan_string(
        value: Any,
        label: str,
        *,
        allow_deferred: bool = False,
    ) -> str:
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError(
                f"{label} must resolve to a scalar value, got "
                f"{type(value).__name__}."
            )
        resolved = str(value)
        if not resolved.strip():
            raise ValueError(f"{label} must resolve to a non-empty value.")
        if not allow_deferred and _carries_template(resolved):
            raise PlanValueResolutionError(
                detail=f"{label} remains unresolved: {resolved!r}.",
                public_message=(
                    "A required runtime value remains unresolved."
                ),
            )
        return resolved

    @staticmethod
    def _validate_prepared_parameter_names(
        details: DeploymentOperation,
        parameters: dict[Any, Any],
    ) -> None:
        invalid_names = [
            name
            for name in parameters
            if not isinstance(name, str)
            or name not in details.accepted_parameters
        ]
        if invalid_names:
            raise PlanValueResolutionError(
                detail=(
                    "The deployment template does not accept the resolved "
                    f"parameter name(s): {invalid_names}."
                ),
                public_message=(
                    "A deferred parameter name is not accepted by the "
                    "deployment template."
                ),
            )

    def _execute_prepared_operation(
        self,
        plan: DeploymentPlan,
        target: PreparedTarget,
        operation: PreparedOperation,
        timestamp: str,
        outputs: dict[OperationIdentity, dict[str, Any]],
        execution_mode: PlanExecutionMode,
    ) -> StepResult:
        details = operation.details
        if isinstance(details, DeploymentOperation):
            if (
                details.input_status is not InputStatus.PREPARED
                or details.parameters is None
            ):
                raise ValueError(
                    f"Deployment step '{operation.identity.step}' on site "
                    f"'{operation.identity.target}' has no prepared "
                    "parameters."
                )
            parameters = resolve_plan_value(
                details.parameters,
                outputs,
                mode=execution_mode,
            )
            if not isinstance(parameters, dict):
                raise TypeError(
                    "Prepared deployment parameters did not resolve to a "
                    "mapping."
                )
            if execution_mode is PlanExecutionMode.APPLY:
                self._validate_prepared_parameter_names(
                    details,
                    parameters,
                )
            deployment_name = self._prepared_deployment_name(
                plan.manifest_name,
                target.name,
                operation.identity.step,
                timestamp,
            )
            if operation.scope is OperationScope.SUBSCRIPTION:
                return self.executor.deploy_subscription(
                    subscription=target.subscription,
                    location=target.location,
                    template_path=details.template,
                    parameters=parameters,
                    deployment_name=deployment_name,
                    step_name=operation.identity.step,
                    site_name=target.name,
                )
            return self.executor.deploy_resource_group(
                subscription=target.subscription,
                resource_group=target.resource_group or "",
                template_path=details.template,
                parameters=parameters,
                deployment_name=deployment_name,
                step_name=operation.identity.step,
                site_name=target.name,
            )

        if isinstance(details, KubectlOperation):
            if details.input_status is not InputStatus.PREPARED:
                raise ValueError(
                    f"Kubectl step '{operation.identity.step}' on site "
                    f"'{operation.identity.target}' has no prepared inputs."
                )
            cluster_name = self._resolved_plan_string(
                resolve_plan_value(
                    details.cluster_name,
                    outputs,
                    mode=execution_mode,
                ),
                "Kubectl cluster name",
                allow_deferred=(
                    execution_mode is PlanExecutionMode.PREVIEW
                ),
            )
            resource_group = self._resolved_plan_string(
                resolve_plan_value(
                    details.cluster_resource_group,
                    outputs,
                    mode=execution_mode,
                ),
                "Kubectl cluster resource group",
                allow_deferred=(
                    execution_mode is PlanExecutionMode.PREVIEW
                ),
            )
            files = [
                self._resolved_plan_string(
                    resolve_plan_value(
                        file_value,
                        outputs,
                        mode=execution_mode,
                    ),
                    "Kubectl file",
                    allow_deferred=(
                        execution_mode is PlanExecutionMode.PREVIEW
                    ),
                )
                for file_value in details.files
            ]
            if details.operation == "apply":
                return self.executor.kubectl_apply(
                    cluster_name=cluster_name,
                    resource_group=resource_group,
                    subscription=target.subscription,
                    files=files,
                    step_name=operation.identity.step,
                    site_name=target.name,
                )
            return KubectlResult(
                success=False,
                step_name=operation.identity.step,
                site_name=target.name,
                error=(
                    "Unsupported kubectl operation: "
                    f"{details.operation}"
                ),
            )

        if details.input_status is not InputStatus.PREPARED:
            raise ValueError(
                f"Wait step '{operation.identity.step}' on site "
                f"'{operation.identity.target}' has no prepared inputs."
            )
        resource_id = self._resolved_plan_string(
            resolve_plan_value(
                details.resource_id,
                outputs,
                mode=execution_mode,
            ),
            "Wait resource ID",
            allow_deferred=(
                execution_mode is PlanExecutionMode.PREVIEW
            ),
        )
        tag_key = self._resolved_plan_string(
            resolve_plan_value(
                details.tag_key,
                outputs,
                mode=execution_mode,
            ),
            "Wait tag key",
            allow_deferred=(
                execution_mode is PlanExecutionMode.PREVIEW
            ),
        )
        expected_value = self._resolved_plan_string(
            resolve_plan_value(
                details.expected_value,
                outputs,
                mode=execution_mode,
            ),
            "Wait expected value",
            allow_deferred=(
                execution_mode is PlanExecutionMode.PREVIEW
            ),
        )
        failure_pattern = (
            self._resolved_plan_string(
                resolve_plan_value(
                    details.failure_pattern,
                    outputs,
                    mode=execution_mode,
                ),
                "Wait failure pattern",
                allow_deferred=(
                    execution_mode is PlanExecutionMode.PREVIEW
                ),
            )
            if details.failure_pattern is not None
            else None
        )
        try:
            condition = ArmTagCondition(
                type="arm-tag",
                resource_id=resource_id,
                tag_key=tag_key,
                expected_value=expected_value,
                failure_pattern=failure_pattern,
            )
        except ValueError as error:
            raise PlanValueResolutionError(
                detail=(
                    f"Wait step '{operation.identity.step}' condition "
                    f"invalid after resolution: {error}"
                ),
                public_message=(
                    "The resolved wait condition is invalid."
                ),
            ) from error
        return self.executor.wait_for_condition(
            condition=condition,
            timeout_minutes=details.timeout_minutes,
            poll_interval_seconds=details.poll_interval_seconds,
            subscription=target.subscription,
            step_name=operation.identity.step,
            site_name=target.name,
        )

    def _execute_prepared_target(
        self,
        plan: DeploymentPlan,
        target: PreparedTarget,
        timestamp: str,
        inherited_outputs: dict[OperationIdentity, dict[str, Any]],
        *,
        parallel_mode: bool,
        execution_mode: PlanExecutionMode,
    ) -> tuple[dict[str, Any], dict[OperationIdentity, dict[str, Any]]]:
        target_start = time.time()
        outputs = dict(inherited_outputs)
        produced_outputs: dict[
            OperationIdentity,
            dict[str, Any],
        ] = {}
        log = _thread_safe_print if parallel_mode else print
        target_label = site_name_for_output(target.name)

        steps_completed = 0
        steps_skipped = 0
        status = "success"
        error_message: str | None = None
        step_results: list[dict[str, Any]] = []

        for operation in target.operations:
            if operation.disposition is PlanDisposition.SKIP:
                if operation.skip_reason is None:
                    raise ValueError(
                        f"Skipped step '{operation.identity.step}' on site "
                        f"'{operation.identity.target}' has no reason."
                    )
                reason = operation.skip_reason.detail
                shown_reason = (
                    "condition not met"
                    if operation.skip_reason.code
                    is SkipReasonCode.CONDITION_FALSE
                    else reason
                )
                log(
                    f"[{target_label}] - {operation.identity.step} "
                    f"(skipped: {shown_reason})"
                )
                steps_skipped += 1
                step_results.append(
                    {
                        "step": operation.identity.step,
                        "status": "skipped",
                        "reason": reason,
                    }
                )
                continue
            if operation.disposition is PlanDisposition.BLOCKED:
                raise ValueError(
                    f"Step '{operation.identity.step}' on site "
                    f"'{operation.identity.target}' is blocked."
                )

            details = operation.details
            if isinstance(details, KubectlOperation):
                step_type = f"kubectl:{details.operation}"
            elif isinstance(details, ArmTagWaitOperation):
                step_type = "wait"
            else:
                step_type = operation.scope.value
            log(
                f"[{target_label}] > {operation.identity.step} "
                f"({step_type})..."
            )

            try:
                result = self._execute_prepared_operation(
                    plan,
                    target,
                    operation,
                    timestamp,
                    outputs,
                    execution_mode,
                )
            except (
                PlanValueResolutionError,
                TypeError,
                ValueError,
            ) as error:
                reportable = self._reportable_deploy_error(
                    error,
                    target.name,
                )
                if operation.kind is OperationKind.DEPLOYMENT:
                    result = DeploymentResult(
                        success=False,
                        step_name=operation.identity.step,
                        site_name=target.name,
                        deployment_name=self._prepared_deployment_name(
                            plan.manifest_name,
                            target.name,
                            operation.identity.step,
                            timestamp,
                        ),
                        error=reportable,
                    )
                elif operation.kind is OperationKind.KUBECTL:
                    result = KubectlResult(
                        success=False,
                        step_name=operation.identity.step,
                        site_name=target.name,
                        error=reportable,
                    )
                else:
                    result = WaitResult(
                        success=False,
                        step_name=operation.identity.step,
                        site_name=target.name,
                        error=reportable,
                    )
            if result.success:
                step_outputs = (
                    result.outputs or {}
                    if isinstance(result, DeploymentResult)
                    else {}
                )
                if step_outputs:
                    outputs[operation.identity] = step_outputs
                    produced_outputs[operation.identity] = step_outputs
                log(f"[{target_label}] + {operation.identity.step}")
                steps_completed += 1
                step_results.append(
                    {
                        "step": operation.identity.step,
                        "status": "success",
                        "outputs": step_outputs,
                    }
                )
                continue

            reportable_error = scrub_site_for_output(
                result.error,
                target.name,
            )
            log(
                f"[{target_label}] x {operation.identity.step}: "
                f"{reportable_error}"
            )
            status = "failed"
            error_message = reportable_error
            step_results.append(
                {
                    "step": operation.identity.step,
                    "status": "failed",
                    "error": reportable_error,
                }
            )
            break

        elapsed = time.time() - target_start
        total_steps = len(plan.steps)
        skip_info = (
            f", {steps_skipped} skipped"
            if steps_skipped > 0
            else ""
        )
        status_symbol = "+" if status == "success" else "x"
        log(
            f"[{target_label}] {status_symbol} completed in {elapsed:.1f}s "
            f"({steps_completed}/{total_steps - steps_skipped} steps"
            f"{skip_info})"
        )

        if (
            steps_completed == 0
            and steps_skipped == total_steps
            and total_steps > 0
        ):
            reasons = sorted(
                {
                    step_result["reason"]
                    for step_result in step_results
                    if step_result.get("status") == "skipped"
                    and step_result.get("reason")
                }
            )
            detail = f" ({' | '.join(reasons)})" if reasons else ""
            log(
                f"[{target_label}] ! nothing deployed: every step was "
                f"skipped{detail}."
            )

        return (
            {
                "site": target.name,
                "status": status,
                "error": error_message,
                "steps_completed": steps_completed,
                "steps_skipped": steps_skipped,
                "steps_total": total_steps,
                "elapsed": elapsed,
                "steps": step_results,
            },
            produced_outputs,
        )

    @staticmethod
    def _prepared_target_failure_result(
        target: PreparedTarget,
        plan: DeploymentPlan,
        error: str,
    ) -> dict[str, Any]:
        return {
            "site": target.name,
            "status": "failed",
            "error": scrub_site_for_output(error, target.name),
            "steps_completed": 0,
            "steps_skipped": 0,
            "steps_total": len(plan.steps),
            "elapsed": 0.0,
            "steps": [],
        }

    def _run_prepared_targets(
        self,
        plan: DeploymentPlan,
        targets: list[PreparedTarget],
        timestamp: str,
        inherited_outputs: dict[OperationIdentity, dict[str, Any]],
        execution_mode: PlanExecutionMode,
    ) -> tuple[
        list[dict[str, Any]],
        dict[OperationIdentity, dict[str, Any]],
    ]:
        if not targets:
            return [], {}

        parallel = ParallelConfig(sites=plan.max_parallel_sites)
        if parallel.is_sequential or len(targets) == 1:
            results: list[dict[str, Any]] = []
            produced: dict[OperationIdentity, dict[str, Any]] = {}
            for target in targets:
                try:
                    result, target_outputs = self._execute_prepared_target(
                        plan,
                        target,
                        timestamp,
                        inherited_outputs,
                        parallel_mode=False,
                        execution_mode=execution_mode,
                    )
                except Exception as error:
                    reportable = self._reportable_deploy_error(
                        error,
                        target.name,
                    )
                    logger.error(
                        "Unexpected error deploying to "
                        f"{site_name_for_output(target.name)}: {reportable}"
                    )
                    result = self._prepared_target_failure_result(
                        target,
                        plan,
                        f"Unexpected error: {reportable}",
                    )
                    target_outputs = {}
                results.append(result)
                produced.update(target_outputs)
            return results, produced

        max_workers = parallel.max_workers
        worker_count = (
            len(targets)
            if max_workers is None
            else min(len(targets), max_workers)
        )
        print(
            f"\n  [Parallel] Deploying to {len(targets)} sites "
            f"({worker_count} concurrent)"
        )
        results = []
        produced = {}
        result_lock = threading.Lock()
        future_to_target = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            try:
                for target in targets:
                    future = executor.submit(
                        self._execute_prepared_target,
                        plan,
                        target,
                        timestamp,
                        inherited_outputs,
                        parallel_mode=True,
                        execution_mode=execution_mode,
                    )
                    future_to_target[future] = target
                for future in as_completed(future_to_target):
                    target = future_to_target[future]
                    try:
                        result, target_outputs = future.result()
                    except Exception as error:
                        reportable = self._reportable_deploy_error(
                            error,
                            target.name,
                        )
                        logger.error(
                            "Unexpected error deploying to "
                            f"{site_name_for_output(target.name)}: "
                            f"{reportable}"
                        )
                        result = self._prepared_target_failure_result(
                            target,
                            plan,
                            f"Unexpected error: {reportable}",
                        )
                        target_outputs = {}
                    with result_lock:
                        results.append(result)
                        produced.update(target_outputs)
            except BaseException:
                cancelled = sum(
                    1
                    for pending in future_to_target
                    if pending.cancel()
                )
                logger.error(
                    f"Deployment stopped. {len(results)} site(s) reported, "
                    f"{cancelled} not started."
                )
                raise
        return results, produced

    @staticmethod
    def _target_has_unavailable_source_reference(
        target: PreparedTarget,
        source_target: str,
        outputs: dict[OperationIdentity, dict[str, Any]],
    ) -> bool:
        for operation in target.operations:
            for reference in operation.data_references:
                if reference.source.target != source_target:
                    continue
                try:
                    resolve_plan_value(OutputValue(reference), outputs)
                except PlanValueResolutionError:
                    return True
        return False

    def execute_plan(
        self,
        result: PlanBuildResult,
        *,
        mode: PlanExecutionMode = PlanExecutionMode.APPLY,
    ) -> dict[str, Any]:
        """Execute one previously prepared plan without replanning."""
        if not result.executable or result.plan is None:
            raise PlanNotExecutableError(result)
        plan = result.plan
        if not plan.targets:
            logger.warning("No sites to deploy to")
            return {
                "sites": {},
                "summary": {
                    "total": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "elapsed": 0.0,
                },
            }

        logger.info(
            f"Deploying '{plan.manifest_name}' to "
            f"{len(plan.targets)} site(s) "
            f"(parallel: {ParallelConfig(plan.max_parallel_sites)})"
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        start_time = time.time()
        results: list[dict[str, Any]] = []
        operation_outputs: dict[
            OperationIdentity,
            dict[str, Any],
        ] = {}

        subscription_targets: dict[str, PreparedTarget] = {}
        resource_group_targets: list[PreparedTarget] = []
        for target in plan.targets:
            if target.kind is TargetKind.SUBSCRIPTION:
                existing = subscription_targets.get(target.subscription)
                if existing is not None:
                    names = f"{existing.name}, {target.name}"
                    raise MultipleSubscriptionSitesError(
                        f"Subscription "
                        f"'{_reportable_subscription(target.subscription)}' "
                        f"has multiple subscription-level sites: {names}. "
                        "Only one subscription-level site per subscription "
                        "is allowed."
                    )
                subscription_targets[target.subscription] = target
            else:
                resource_group_targets.append(target)

        has_subscription_steps = any(
            step.scope is OperationScope.SUBSCRIPTION
            for step in plan.steps
        )
        if has_subscription_steps:
            if subscription_targets:
                print(
                    "\n  [Phase 1] Subscription-scoped steps: "
                    f"{len(subscription_targets)} subscription(s)"
                )
            subscription_results, subscription_outputs = (
                self._run_prepared_targets(
                    plan,
                    list(subscription_targets.values()),
                    timestamp,
                    operation_outputs,
                    mode,
                )
            )
            results.extend(subscription_results)
            operation_outputs.update(subscription_outputs)
            failed_subscription_targets = {
                row["site"]
                for row in subscription_results
                if row["status"] == "failed"
            }

            proceeding_targets: list[PreparedTarget] = []
            for target in resource_group_targets:
                subscription_target = subscription_targets.get(
                    target.subscription
                )
                if (
                    subscription_target is not None
                    and subscription_target.name
                    in failed_subscription_targets
                    and self._target_has_unavailable_source_reference(
                        target,
                        subscription_target.name,
                        operation_outputs,
                    )
                ):
                    _thread_safe_print(
                        f"[{site_name_for_output(target.name)}] - blocked "
                        "(subscription deployment failed, site depends on "
                        "its outputs)"
                    )
                    results.append(
                        {
                            "site": target.name,
                            "status": "blocked",
                            "error": (
                                "Subscription deployment failed and site "
                                "depends on its outputs"
                            ),
                            "steps_completed": 0,
                            "steps_skipped": len(plan.steps),
                            "steps_total": len(plan.steps),
                            "elapsed": 0.0,
                            "steps": [],
                        }
                    )
                else:
                    proceeding_targets.append(target)

            if proceeding_targets:
                print(
                    "\n  [Phase 2] Resource group-scoped steps: "
                    f"{len(proceeding_targets)} site(s)"
                )
                resource_results, _ = self._run_prepared_targets(
                    plan,
                    proceeding_targets,
                    timestamp,
                    operation_outputs,
                    mode,
                )
                results.extend(resource_results)
        else:
            all_results, _ = self._run_prepared_targets(
                plan,
                list(plan.targets),
                timestamp,
                operation_outputs,
                mode,
            )
            results.extend(all_results)

        total_elapsed = time.time() - start_time
        summary = {
            "total": len(results),
            "succeeded": sum(
                row["status"] == "success"
                for row in results
            ),
            "failed": sum(
                row["status"] == "failed"
                for row in results
            ),
            "elapsed": total_elapsed,
        }
        self._print_deployment_summary(results, total_elapsed)
        return {
            "sites": {row["site"]: row for row in results},
            "summary": summary,
        }

    def deploy(
        self,
        manifest_path: Path,
        selector: str | None = None,
        parallel_override: int | None = None,
        manifest: Manifest | None = None,
        sites: list[Site] | None = None,
        plan_result: PlanBuildResult | None = None,
    ) -> dict[str, Any]:
        """Prepare one deployment plan and execute it."""
        result = plan_result or self.build_plan(
            manifest_path,
            selector,
            intent=PlanIntent.EXECUTABLE,
            manifest=manifest,
            sites=sites,
            parallel_override=parallel_override,
        )
        return self.execute_plan(
            result,
            mode=(
                PlanExecutionMode.PREVIEW
                if self.dry_run
                else PlanExecutionMode.APPLY
            ),
        )
