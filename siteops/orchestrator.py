# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Main orchestration engine.

This module provides the Orchestrator class which handles:
- Loading sites and manifests from the workspace
- Resolving parameters with template variable substitution
- Executing deployment steps (Bicep/ARM and kubectl) across sites
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from siteops.executor import (
    AzCliExecutor,
    DeploymentResult,
    KubectlResult,
    filter_parameters,
)
from siteops.models import (
    CONDITION_PATTERN,
    DeploymentStep,
    KubectlStep,
    Manifest,
    ManifestStep,
    NoTargetingError,
    ParallelConfig,
    SelectorParseError,
    Site,
    _normalize_site_identifier,
    _validate_resource,
    parse_selector,
)

logger = logging.getLogger(__name__)

# Pattern for {{ steps.<step_name>.outputs.<output_path> }}
# Supports nested paths like: steps.X.outputs.Y.Z.A
STEP_OUTPUT_PATTERN = re.compile(r"\{\{\s*steps\.([a-zA-Z0-9_-]+)\.outputs\.([a-zA-Z0-9_.-]+)\s*\}\}")

# Pattern for {{ site.properties.<path> }}
# Supports nested paths and array indices like: site.properties.endpoints[0].host
SITE_PROPERTIES_PATTERN = re.compile(r"\{\{\s*site\.properties\.([a-zA-Z0-9_.\[\]]+)\s*\}\}")

# Pattern for {{ site.parameters.<path> }}
# Supports nested paths like: site.parameters.brokerConfig.memoryProfile
SITE_PARAMETERS_PATTERN = re.compile(r"\{\{\s*site\.parameters\.([a-zA-Z0-9_.\[\]]+)\s*\}\}")

# Result type that can be either a deployment or kubectl result
StepResult = DeploymentResult | KubectlResult

# Type alias for subscription-scoped outputs: subscription_id -> step_name -> outputs
SubscriptionOutputs = dict[str, dict[str, dict[str, Any]]]


def _resolve_output_path(obj: Any, path: str) -> Any:
    """Resolve a dot-separated path into an object.

    Handles Azure ARM output format which wraps values in {"value": X, "type": "..."}

    Args:
        obj: The object to traverse (dict or value)
        path: Dot-separated path like "adrNamespace.id"

    Returns:
        The value at the path, or None if not found
    """
    parts = path.split(".")
    current = obj

    for part in parts:
        if current is None:
            return None
        # Unwrap Azure output format at each level
        if isinstance(current, dict) and "value" in current and "type" in current:
            current = current["value"]
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    # Final unwrap if needed
    if isinstance(current, dict) and "value" in current and "type" in current:
        current = current["value"]

    return current


# Lock for thread-safe console output
_print_lock = threading.Lock()


def _thread_safe_print(*args: Any, **kwargs: Any) -> None:
    """Print with lock to avoid interleaved output from multiple threads."""
    with _print_lock:
        print(*args, **kwargs)


class Orchestrator:
    """Orchestrates deployments across sites.

    The orchestrator is responsible for:
    - Loading and caching sites from the workspace
    - Resolving manifest steps with parameter files and template variables
    - Executing deployment steps (Bicep/ARM deployments and kubectl operations)
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
                    f"sites/ directory; already included by default."
                )
            if resolved == overlay:
                raise ValueError(
                    f"Extra site dir '{candidate}' is the workspace's "
                    f"sites.local/ directory. Registering it as trusted "
                    f"would allow overlays to inject inheritance; refused "
                    f"for security."
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
        code-reviewed or caller-vouched-for trusted location.
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
                    data = yaml.safe_load(f) or {}
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
        """
        if "spec" in data:
            metadata = data.get("metadata") or {}
            return metadata.get("name")
        return data.get("name")

    def _canonical_site_id(self, site_path: Path) -> str:
        """Return the canonical relative-path identifier for a site file.

        Used to key the overlay merge in `_load_site_data`. Falls back to
        the basename when the path is not under any trusted directory
        (defensive; should not happen in practice).
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
        result; neither input is modified.
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                # Recurse whether or not the dict subtree exists in base.
                # When base lacks the key the inner walk attributes every
                # leaf; otherwise it merges and only re-attributes leaves
                # the override actually touched.
                base_subtree = result[key] if key in result and isinstance(result[key], dict) else {}
                result[key] = self._deep_merge_provenance(
                    base_subtree, value, origin, prov, full_key
                )
            else:
                result[key] = copy.deepcopy(value)
                prov[full_key] = origin
        return result

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
        """
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
        if prov is None and normalized in self._inherited_data_cache:
            return copy.deepcopy(self._inherited_data_cache[normalized])

        if not path.exists():
            raise FileNotFoundError(f"Inherited file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # Inherits parents must be SiteTemplates. A `kind: Site` parent
        # would chain deployable sites together, where editing one would
        # silently change the other; that is almost always an authoring
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
            parent_data = self._load_inherited_data(parent_path, seen, prov=prov)
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
            if prov is not None:
                # Attribute every leaf in the leaf template to itself.
                data = self._deep_merge_provenance(
                    {}, leaf_data, self._origin_label(path), prov
                )
            else:
                data = leaf_data

        if prov is None:
            self._inherited_data_cache[normalized] = copy.deepcopy(data)

        logger.debug(f"Loaded inherited data from: {path}")
        return data

    def _load_site_data(
        self, name: str, prov: dict[str, str] | None = None
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
                        data = yaml.safe_load(f) or {}

                    # Process inheritance only on the first file found (the base)
                    if is_base_file and "inherits" in data:
                        inherits_path = self._resolve_inherits(path, data["inherits"])
                        # Initialize seen list with current file to detect self-reference
                        inherited_data = self._load_inherited_data(
                            inherits_path, seen=[path.resolve()], prov=prov
                        )
                        # Merge inherited into the working dict WITHOUT
                        # re-attribution. The per-leaf provenance for
                        # inherited keys was already set during the chain
                        # walk; the outer merge would otherwise clobber it
                        # with the parent file's label.
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
                    # base file; the workspace name indexes are built
                    # from base files, so an overlay rename produces a
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

    def load_site_with_provenance(self, name: str) -> tuple[Site, dict[str, str]]:
        """Load a site and return per-key provenance for its merged data.

        The provenance dict maps every dotted leaf key in the merged
        site to the workspace-relative path of the file whose value
        won. Used by `siteops sites <name> -v` to show where each
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
        site = self._parse_site_dict(merged_data, site_path, default_name, source_name=name)
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

        site = self._parse_site_dict(merged_data, site_path, default_name, source_name=name)

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

    def _parse_site_dict(
        self,
        merged_data: dict[str, Any],
        site_path: Path,
        default_name: str,
        source_name: str,
    ) -> Site:
        """Build a `Site` from merged data and the resolved file path.

        Single source of truth for the parsing rules `load_site` and
        `load_site_with_provenance` both depend on. Supports the flat
        shape (`name:` at top level, fields at top level) and the K8s
        envelope (`metadata:` + `spec:`). Defaults the site's `name`
        to the basename when neither shape supplies one.

        Args:
            merged_data: Output of `_load_site_data` (any shape).
            site_path: Resolved path of the base site file (used for
                error messages only).
            default_name: Default for `Site.name` when neither
                `metadata.name` nor top-level `name` is set.
            source_name: The identifier the caller passed; used in the
                "missing required field" error message.

        Raises:
            ValueError: When required fields (`subscription`,
                `location`) are missing.
        """
        if "spec" in merged_data:
            spec = merged_data["spec"]
            metadata = merged_data.get("metadata", {})
            site_name = metadata.get("name", default_name)
            labels = metadata.get("labels", {})
        else:
            spec = merged_data
            site_name = merged_data.get("name", default_name)
            labels = merged_data.get("labels", {})

        for req in ("subscription", "location"):
            if req not in spec:
                raise ValueError(f"Missing required field '{req}' in site: {source_name}")

        return Site(
            name=site_name,
            subscription=spec["subscription"],
            resource_group=spec.get("resourceGroup", ""),
            location=spec["location"],
            labels=labels,
            properties=spec.get("properties", {}),
            parameters=spec.get("parameters", {}),
        )

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
                data = yaml.safe_load(f)
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
        snapshot of every site once the workspace finishes loading;
        subsequent commands like `explain_no_match` reuse it.

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
                logger.warning(f"Failed to load site '{name}': {e}")
                skipped.append((name, str(e)))

        if skipped:
            import sys

            print(f"\n\u26a0 Skipped {len(skipped)} site(s) due to errors:", file=sys.stderr)
            for name, error in skipped:
                print(f"  \u2022 {name}: {error}", file=sys.stderr)
            print(file=sys.stderr)

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
                result = json.load(f)
            else:
                result = yaml.safe_load(f) or {}

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
            return {k: self._resolve_template_strings(v, site, step_outputs) for k, v in value.items()}
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

    def _resolve_step_outputs(
        self,
        value: Any,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
        subscription_id: str | None = None,
    ) -> Any:
        """Recursively resolve {{ steps.<name>.outputs.<path> }} templates.

        Supports output chaining between steps, including cross-scope chaining
        where RG-level sites can reference outputs from subscription-scoped steps.

        Resolution order:
        1. Per-site step_outputs (from RG-scoped steps executed for this site)
        2. Subscription outputs (from subscription-scoped steps for this subscription)

        Args:
            value: Value to resolve (string, dict, list, or other)
            step_outputs: Dict mapping step names to their outputs (per-site)
            subscription_outputs: Dict mapping subscription_id -> step_name -> outputs
            subscription_id: Current site's subscription (for cross-scope resolution)

        Returns:
            Value with all step output references resolved
        """
        if isinstance(value, str):
            # Check if entire string is a single template (for complex types like arrays)
            stripped = value.strip()
            full_match = STEP_OUTPUT_PATTERN.fullmatch(stripped)
            if full_match:
                step_name = full_match.group(1)
                output_path = full_match.group(2)

                # Try per-site outputs first, then subscription outputs
                output_value = self._resolve_output_from_sources(
                    step_name, output_path, step_outputs, subscription_outputs, subscription_id
                )
                if output_value is not None:
                    return output_value
                return value

            # For strings with embedded templates, do string substitution
            def replacer(match: re.Match) -> str:
                step_name = match.group(1)
                output_path = match.group(2)

                output_value = self._resolve_output_from_sources(
                    step_name, output_path, step_outputs, subscription_outputs, subscription_id
                )
                if output_value is None:
                    return match.group(0)

                if isinstance(output_value, (list, dict)):
                    logger.warning(f"Cannot embed complex output '{output_path}' in string: {value}")
                    return match.group(0)

                return str(output_value)

            return STEP_OUTPUT_PATTERN.sub(replacer, value)

        elif isinstance(value, dict):
            return {
                k: self._resolve_step_outputs(v, step_outputs, subscription_outputs, subscription_id)
                for k, v in value.items()
            }
        elif isinstance(value, list):
            return [
                self._resolve_step_outputs(item, step_outputs, subscription_outputs, subscription_id) for item in value
            ]
        return value

    @staticmethod
    def _resolve_output_from_sources(
        step_name: str,
        output_path: str,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None,
        subscription_id: str | None,
    ) -> Any:
        """Resolve an output reference from available sources.

        Args:
            step_name: Name of the step to get outputs from
            output_path: Dot-separated path within the outputs
            step_outputs: Per-site step outputs
            subscription_outputs: Subscription-level step outputs
            subscription_id: Current subscription ID

        Returns:
            Resolved value or None if not found
        """
        # Try per-site outputs first
        step_data = step_outputs.get(step_name)
        if step_data is not None:
            output_value = _resolve_output_path(step_data, output_path)
            if output_value is not None:
                return output_value

        # Fall back to subscription outputs
        if subscription_outputs and subscription_id:
            sub_step_data = subscription_outputs.get(subscription_id, {}).get(step_name)
            if sub_step_data is not None:
                return _resolve_output_path(sub_step_data, output_path)

        return None

    def resolve_parameters(
        self,
        step: DeploymentStep,
        site: Site,
        manifest: Manifest,
        step_outputs: dict[str, dict[str, Any]] | None = None,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> dict[str, Any]:
        """Merge and resolve parameters for a deployment step.

        Parameter merge order (later overrides earlier):
        1. Manifest-level parameter files (from manifest.parameters) - shared defaults
        2. Site-level parameters (from site definition) - site-specific overrides
        3. Step-level parameter files (from step.parameters) - step-specific overrides

        After merging, parameters are:
        - Resolved with template variable substitution ({{ site.X }}, {{ steps.X.outputs.Y }})
        - Filtered to only include parameters accepted by the template

        Args:
            step: The deployment step
            site: Target site
            manifest: The manifest being deployed
            step_outputs: Outputs from previous steps (for chaining)
            subscription_outputs: Outputs from subscription-scoped steps (for cross-scope chaining)

        Returns:
            Fully resolved and filtered parameters dict
        """
        # 1. Start with manifest-level parameter files (shared defaults)
        params: dict[str, Any] = {}
        for param_path in manifest.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                file_params = self.load_parameters(full_path)
                params = self._deep_merge(params, file_params)
            else:
                logger.warning(f"Manifest parameter file not found: {full_path}")

        # 2. Merge site-level parameters (site-specific overrides)
        params = self._deep_merge(params, site.get_all_parameters())

        # 3. Merge step-level parameter files (step-specific overrides)
        for param_path in step.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                file_params = self.load_parameters(full_path)
                params = self._deep_merge(params, file_params)
            else:
                logger.warning(f"Step parameter file not found: {full_path}")

        # 4. Resolve template variables ({{ site.X }})
        params = self._resolve_template_strings(params, site)

        # 5. Resolve step output references ({{ steps.X.outputs.Y }})
        # Includes cross-scope resolution from subscription outputs
        if step_outputs or subscription_outputs:
            params = self._resolve_step_outputs(
                params,
                step_outputs or {},
                subscription_outputs,
                site.subscription,
            )

        # 6. Filter to template-accepted parameters before the unresolved-check
        # so that defaults injected for steps that don't consume them (e.g.
        # `siteAddress.{country,city}` from common.yaml) don't trip the check.
        template_path = (self.workspace / step.template).resolve()
        filter_succeeded = False
        if template_path.exists():
            try:
                params = filter_parameters(params, str(template_path), step.name)
                filter_succeeded = True
            except (ValueError, FileNotFoundError) as e:
                logger.warning(
                    f"Could not filter parameters for step '{step.name}': {e}; "
                    f"skipping unresolved-template precheck so the original error "
                    f"is not masked by a follow-on 'unresolved templates' failure"
                )

        # 7. Fail fast on any unresolved {{ ... }} templates. In dry-run mode
        # downgrade to warning since `{{ steps.X.outputs.Y }}` cannot be
        # resolved without real deployment outputs. Skipped when filtering
        # failed: an unfiltered param set may carry tokens for params the
        # template doesn't accept (which filtering would have stripped), and
        # raising here would hide the upstream filter failure.
        if filter_succeeded:
            self._check_unresolved_templates(params, site.name, step.name)

        return params

    def _check_unresolved_templates(
        self, params: dict[str, Any], site_name: str, step_name: str
    ) -> None:
        """Fail (or warn in dry-run) if any {{ ... }} templates remain.

        Unresolved templates at this stage mean a parameter source did not
        produce the expected output, a step reference points at a non-existent
        step/output, or a `{{ site.X }}` path is wrong. Letting the deployment
        proceed would silently send literal `{{ ... }}` strings to ARM.

        In `--dry-run` mode `{{ steps.X.outputs.Y }}` references cannot be
        resolved (no real outputs exist), so we log a warning instead of
        failing.
        """
        unresolved: list[tuple[str, str]] = []

        def collect(v: Any, path: str = "") -> None:
            if isinstance(v, str) and "{{" in v and "}}" in v:
                unresolved.append((path, v))
            elif isinstance(v, dict):
                for k, val in v.items():
                    collect(val, f"{path}.{k}" if path else k)
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    collect(item, f"{path}[{i}]")

        collect(params)

        if not unresolved:
            return

        details = "; ".join(f"{path}={value}" for path, value in unresolved)
        message = (
            f"Unresolved template(s) for step '{step_name}' (site: {site_name}): "
            f"{details}"
        )
        if self.dry_run:
            logger.warning(message)
            return
        raise ValueError(message)

    def _evaluate_condition(self, condition: str | None, site: Site) -> bool:
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

        # Check scope/site level compatibility
        is_sub_level = site.is_subscription_level
        if step.scope == "subscription" and not is_sub_level:
            return "subscription-scoped step, site has resource group"
        if step.scope == "resourceGroup" and is_sub_level:
            return "resourceGroup-scoped step, site has no resource group"

        return None

    @staticmethod
    def _get_step_type_label(step: ManifestStep) -> str:
        """Get a display label for the step type.

        Args:
            step: The manifest step

        Returns:
            Display string like 'resourceGroup', 'subscription', or 'kubectl:apply'
        """
        if isinstance(step, KubectlStep):
            return f"kubectl:{step.operation}"
        return step.scope

    @staticmethod
    def _get_subscription_step_names(manifest: Manifest) -> set[str]:
        """Get names of all subscription-scoped steps in a manifest.

        Args:
            manifest: The manifest to inspect

        Returns:
            Set of step names that have scope: subscription
        """
        return {
            step.name for step in manifest.steps if isinstance(step, DeploymentStep) and step.scope == "subscription"
        }

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
    def _references_any_step(value: Any, step_names: set[str]) -> bool:
        """Check if a value contains output references to any of the given steps.

        Recursively searches dict/list/str for {{ steps.<name>.outputs.* }} patterns.

        Args:
            value: Parameter value to check
            step_names: Set of step names to look for

        Returns:
            True if value references any step in step_names
        """
        if isinstance(value, dict):
            return any(Orchestrator._references_any_step(v, step_names) for v in value.values())
        elif isinstance(value, list):
            return any(Orchestrator._references_any_step(item, step_names) for item in value)
        elif isinstance(value, str):
            # Quick check before regex
            if "steps." not in value:
                return False
            for match in STEP_OUTPUT_PATTERN.finditer(value):
                if match.group(1) in step_names:
                    return True
        return False

    def _site_depends_on_subscription_outputs(
        self,
        manifest: Manifest,
        site: Site,
        subscription_step_names: set[str],
    ) -> bool:
        """Check if a site's RG-scoped steps reference subscription-scoped outputs.

        Scans manifest-level and step-level parameter files for references to
        subscription-scoped step outputs.

        Args:
            manifest: The manifest being deployed
            site: The site to check
            subscription_step_names: Names of subscription-scoped steps

        Returns:
            True if site has steps that depend on subscription-scoped outputs
        """
        if not subscription_step_names:
            return False

        # Check manifest-level parameters (apply to all steps)
        for param_path in manifest.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                try:
                    params = self.load_parameters(full_path)
                    if self._references_any_step(params, subscription_step_names):
                        return True
                except (ValueError, yaml.YAMLError, OSError) as e:
                    logger.debug(f"Could not read parameter file {full_path}: {e}")

        # Check step-level parameters for RG-scoped steps
        for step in manifest.steps:
            if isinstance(step, DeploymentStep) and step.scope == "resourceGroup":
                for param_path in step.parameters:
                    resolved_path = manifest.resolve_parameter_path(param_path, site)
                    full_path = (self.workspace / resolved_path).resolve()
                    if full_path.exists():
                        try:
                            params = self.load_parameters(full_path)
                            if self._references_any_step(params, subscription_step_names):
                                return True
                        except (ValueError, yaml.YAMLError, OSError) as e:
                            logger.debug(f"Could not read parameter file {full_path}: {e}")

        return False

    def _deploy_bicep_step(
        self,
        site: Site,
        step: DeploymentStep,
        manifest: Manifest,
        timestamp: str,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> DeploymentResult:
        """Execute a Bicep/ARM deployment step.

        Args:
            site: Target site
            step: The deployment step
            manifest: The manifest being deployed
            timestamp: Shared timestamp for deployment naming
            step_outputs: Outputs from previous steps (per-site)
            subscription_outputs: Outputs from subscription-scoped steps (for cross-scope chaining)

        Returns:
            DeploymentResult with success status and outputs
        """
        params = self.resolve_parameters(step, site, manifest, step_outputs, subscription_outputs)
        template_path = (self.workspace / step.template).resolve()

        # Azure deployment names have a 64 char limit
        # Format: {base_name}-{timestamp} where timestamp is 14 chars (YYYYMMDDHHmmss)
        base_name = f"{manifest.name}-{site.name}-{step.name}"
        MAX_LEN = 64
        TIMESTAMP_LEN = 14
        max_base = MAX_LEN - TIMESTAMP_LEN - 1  # -1 for the separator

        if len(base_name) > max_base:
            # Use hash suffix to ensure uniqueness when truncating
            name_hash = hashlib.md5(base_name.encode()).hexdigest()[:6]
            base_name = f"{base_name[:max_base - 7]}-{name_hash}"

        deployment_name = f"{base_name}-{timestamp}"

        if step.scope == "subscription":
            return self.executor.deploy_subscription(
                subscription=site.subscription,
                location=site.location,
                template_path=template_path,
                parameters=params,
                deployment_name=deployment_name,
                step_name=step.name,
                site_name=site.name,
            )
        else:
            return self.executor.deploy_resource_group(
                subscription=site.subscription,
                resource_group=site.resource_group,
                template_path=template_path,
                parameters=params,
                deployment_name=deployment_name,
                step_name=step.name,
                site_name=site.name,
            )

    def _execute_kubectl_step(
        self,
        site: Site,
        step: KubectlStep,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> KubectlResult:
        """Execute a kubectl step against an Arc-connected cluster.

        Args:
            site: Target site
            step: The kubectl step
            step_outputs: Outputs from previous steps (per-site)
            subscription_outputs: Outputs from subscription-scoped steps

        Returns:
            KubectlResult with success status
        """
        # Resolve template variables in Arc config
        cluster_name = self._resolve_template_strings(step.arc.name, site)
        resource_group = self._resolve_template_strings(step.arc.resource_group, site)

        if step_outputs or subscription_outputs:
            cluster_name = self._resolve_step_outputs(
                cluster_name, step_outputs, subscription_outputs, site.subscription
            )
            resource_group = self._resolve_step_outputs(
                resource_group, step_outputs, subscription_outputs, site.subscription
            )

        # Resolve template variables in files list
        resolved_files = []
        for f in step.files:
            resolved = self._resolve_template_strings(f, site)
            if step_outputs or subscription_outputs:
                resolved = self._resolve_step_outputs(resolved, step_outputs, subscription_outputs, site.subscription)
            resolved_files.append(resolved)

        if step.operation == "apply":
            return self.executor.kubectl_apply(
                cluster_name=cluster_name,
                resource_group=resource_group,
                subscription=site.subscription,
                files=resolved_files,
                step_name=step.name,
                site_name=site.name,
            )
        else:
            # Should not happen due to model validation
            return KubectlResult(
                success=False,
                step_name=step.name,
                site_name=site.name,
                error=f"Unsupported kubectl operation: {step.operation}",
            )

    def _execute_step(
        self,
        site: Site,
        step: ManifestStep,
        manifest: Manifest,
        timestamp: str,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> StepResult:
        """Execute a single step (deployment or kubectl).

        Args:
            site: Target site
            step: The step to execute
            manifest: The manifest being deployed
            timestamp: Shared timestamp for deployment naming
            step_outputs: Outputs from previous steps (per-site)
            subscription_outputs: Outputs from subscription-scoped steps

        Returns:
            StepResult (DeploymentResult or KubectlResult)
        """
        if isinstance(step, KubectlStep):
            return self._execute_kubectl_step(site, step, step_outputs, subscription_outputs)
        else:
            return self._deploy_bicep_step(site, step, manifest, timestamp, step_outputs, subscription_outputs)

    def _deploy_site(
        self,
        manifest: Manifest,
        site: Site,
        timestamp: str,
        parallel_mode: bool = False,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> dict[str, Any]:
        """Deploy all applicable steps to a single site.

        Steps are executed sequentially. If a step fails, remaining steps
        are skipped for that site.

        Step applicability based on site type:
        - Subscription-level sites: Only execute subscription-scoped steps
        - RG-level sites: Only execute RG-scoped steps (can reference subscription outputs)

        Args:
            manifest: The manifest being deployed
            site: Target site
            timestamp: Shared timestamp for deployment naming
            parallel_mode: If True, use thread-safe printing
            subscription_outputs: Outputs from subscription-scoped steps (for cross-scope chaining)

        Returns:
            Dict with site deployment result including status, steps, and timing
        """
        site_start = time.time()
        step_outputs: dict[str, dict[str, Any]] = {}
        log = _thread_safe_print if parallel_mode else print

        steps_completed = 0
        steps_skipped = 0
        status = "success"
        error_message: str | None = None
        step_results: list[dict[str, Any]] = []

        for step in manifest.steps:
            # Check step/site scope compatibility
            skip_reason = self._check_step_site_compatibility(step, site)
            if skip_reason:
                log(f"[{site.name}] - {step.name} (skipped: {skip_reason})")
                steps_skipped += 1
                step_results.append(
                    {
                        "step": step.name,
                        "status": "skipped",
                        "reason": skip_reason,
                    }
                )
                continue

            # Evaluate condition
            if not self._evaluate_condition(step.when, site):
                log(f"[{site.name}] - {step.name} (skipped: condition not met)")
                steps_skipped += 1
                step_results.append(
                    {
                        "step": step.name,
                        "status": "skipped",
                        "reason": f"Condition not met: {step.when}",
                    }
                )
                continue

            step_type = self._get_step_type_label(step)
            log(f"[{site.name}] > {step.name} ({step_type})...")

            result = self._execute_step(site, step, manifest, timestamp, step_outputs, subscription_outputs)

            if result.success:
                # Only DeploymentResult has outputs for chaining
                outputs = result.outputs or {} if isinstance(result, DeploymentResult) else {}
                if outputs:
                    step_outputs[step.name] = outputs
                log(f"[{site.name}] + {step.name}")
                steps_completed += 1
                step_results.append(
                    {
                        "step": step.name,
                        "status": "success",
                        "outputs": outputs,
                    }
                )
            else:
                log(f"[{site.name}] x {step.name}: {result.error}")
                status = "failed"
                error_message = result.error
                step_results.append(
                    {
                        "step": step.name,
                        "status": "failed",
                        "error": result.error,
                    }
                )
                break

        elapsed = time.time() - site_start
        total_steps = len(manifest.steps)

        skip_info = f", {steps_skipped} skipped" if steps_skipped > 0 else ""
        status_symbol = "+" if status == "success" else "x"
        log(
            f"[{site.name}] {status_symbol} completed in {elapsed:.1f}s "
            f"({steps_completed}/{total_steps - steps_skipped} steps{skip_info})"
        )

        return {
            "site": site.name,
            "status": status,
            "error": error_message,
            "steps_completed": steps_completed,
            "steps_skipped": steps_skipped,
            "steps_total": total_steps,
            "elapsed": elapsed,
            "steps": step_results,
        }

    def _deploy_sequential(
        self,
        manifest: Manifest,
        sites: list[Site],
        timestamp: str,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> list[dict[str, Any]]:
        """Deploy to sites sequentially (one at a time).

        Args:
            manifest: The manifest being deployed
            sites: List of target sites
            timestamp: Shared timestamp for deployment naming
            subscription_outputs: Outputs from subscription-scoped steps (for RG-scoped steps)

        Returns:
            List of deployment results per site
        """
        results: list[dict[str, Any]] = []
        for site in sites:
            result = self._deploy_site(
                manifest,
                site,
                timestamp,
                parallel_mode=False,
                subscription_outputs=subscription_outputs,
            )
            results.append(result)
        return results

    def _deploy_parallel(
        self,
        manifest: Manifest,
        sites: list[Site],
        timestamp: str,
        parallel_config: ParallelConfig,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> list[dict[str, Any]]:
        """Deploy to sites in parallel with controlled concurrency.

        Args:
            manifest: The manifest being deployed
            sites: List of target sites
            timestamp: Shared timestamp for deployment naming
            parallel_config: Parallelism configuration
            subscription_outputs: Outputs from subscription-scoped steps (for RG-scoped steps)

        Returns:
            List of deployment results per site
        """
        max_workers = parallel_config.max_workers
        # If unlimited (None), cap at number of sites
        num_workers = len(sites) if max_workers is None else min(len(sites), max_workers)

        print(f"\n  [Parallel] Deploying to {len(sites)} sites ({num_workers} concurrent)")

        results: list[dict[str, Any]] = []
        results_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_site = {
                executor.submit(self._deploy_site, manifest, site, timestamp, True, subscription_outputs): site
                for site in sites
            }

            for future in as_completed(future_to_site):
                site = future_to_site[future]
                try:
                    result = future.result()
                    with results_lock:
                        results.append(result)
                except Exception as e:
                    logger.error(f"Unexpected error deploying to {site.name}: {e}")
                    with results_lock:
                        results.append(
                            {
                                "site": site.name,
                                "status": "failed",
                                "error": f"Unexpected error: {e}",
                                "steps_completed": 0,
                                "steps_skipped": 0,
                                "steps_total": len(manifest.steps),
                                "elapsed": 0.0,
                                "steps": [],
                            }
                        )

        return results

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

    @staticmethod
    def _has_subscription_scoped_steps(manifest: Manifest) -> bool:
        """Check if manifest has any subscription-scoped steps.

        Args:
            manifest: The manifest to check

        Returns:
            True if any step has scope: subscription
        """
        for step in manifest.steps:
            if isinstance(step, DeploymentStep) and step.scope == "subscription":
                return True
        return False

    def _collect_subscription_outputs(
        self,
        manifest: Manifest,
        subscription_sites: dict[str, Site],
        timestamp: str,
        parallel_config: ParallelConfig,
    ) -> tuple[SubscriptionOutputs, list[dict[str, Any]]]:
        """Execute subscription-scoped steps and collect outputs.

        Args:
            manifest: The manifest being deployed
            subscription_sites: Dict mapping subscription_id to subscription-level site
            timestamp: Shared timestamp for deployment naming
            parallel_config: Parallelism configuration

        Returns:
            Tuple of (subscription_outputs, results)
        """
        subscription_outputs: SubscriptionOutputs = {}
        results: list[dict[str, Any]] = []

        # Get subscription-level sites as a list
        sub_level_sites = list(subscription_sites.values())

        if not sub_level_sites:
            return subscription_outputs, results

        print(f"\n  [Phase 1] Subscription-scoped steps: {len(subscription_sites)} subscription(s)")

        # Deploy to subscription-level sites (they'll skip RG-scoped steps)
        if parallel_config.is_sequential or len(sub_level_sites) == 1:
            for site in sub_level_sites:
                result = self._deploy_site(
                    manifest,
                    site,
                    timestamp,
                    parallel_mode=False,
                    subscription_outputs=subscription_outputs,
                )
                results.append(result)
                # Collect outputs into subscription_outputs keyed by subscription
                self._extract_subscription_outputs(result, site.subscription, subscription_outputs)
        else:
            # Parallel deployment across subscriptions
            max_workers = parallel_config.max_workers
            num_workers = len(sub_level_sites) if max_workers is None else min(len(sub_level_sites), max_workers)

            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                future_to_site = {
                    executor.submit(self._deploy_site, manifest, site, timestamp, True, subscription_outputs): site
                    for site in sub_level_sites
                }

                for future in as_completed(future_to_site):
                    site = future_to_site[future]
                    try:
                        result = future.result()
                        results.append(result)
                        self._extract_subscription_outputs(result, site.subscription, subscription_outputs)
                    except Exception as e:
                        logger.error(f"Error deploying subscription-level site {site.name}: {e}")
                        results.append(
                            {
                                "site": site.name,
                                "status": "failed",
                                "error": str(e),
                                "steps_completed": 0,
                                "steps_skipped": 0,
                                "steps_total": len(manifest.steps),
                                "elapsed": 0.0,
                                "steps": [],
                            }
                        )

        return subscription_outputs, results

    @staticmethod
    def _extract_subscription_outputs(
        result: dict[str, Any],
        subscription_id: str,
        subscription_outputs: SubscriptionOutputs,
    ) -> None:
        """Extract step outputs from a deployment result into subscription_outputs.

        Args:
            result: Deployment result from _deploy_site
            subscription_id: The subscription ID to key outputs by
            subscription_outputs: Dict to populate (mutated in place)
        """
        sub_outputs = subscription_outputs.setdefault(subscription_id, {})
        for step_result in result.get("steps", []):
            outputs = step_result.get("outputs")
            if step_result.get("status") == "success" and outputs:
                sub_outputs[step_result["step"]] = outputs

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
                print(f"    [{result['site']}] {error}")
            print()

        # Show blocked sites
        blocked_results = [r for r in results if r["status"] == "blocked"]
        if blocked_results:
            print("  Blocked Sites:")
            for result in blocked_results:
                error = result.get("error", "Blocked due to subscription failure")
                print(f"    [{result['site']}] {error}")
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
        # empty set and cause a confusing "nothing to deploy" exit; surface
        # the missing-targeting case loudly so the operator can either add
        # targeting to the manifest or pass it on the CLI.
        if not cli_selector and not manifest.sites and not manifest.site_selector:
            raise NoTargetingError(
                f"Manifest '{manifest.name}' declares no `sites:` or `selector:`, "
                f"and no `-l/--selector` was provided on the CLI. Either add "
                f"targeting to the manifest, or pass `-l <key>=<value>` to "
                f"choose sites at deploy time."
            )

        # CLI selector requires loading all sites for filtering
        if cli_selector:
            selector = parse_selector(cli_selector)
            return self.filter_sites(selector)

        # Explicit sites list - load only the named sites (most common case)
        if manifest.sites:
            missing = []
            sites = []
            for name in manifest.sites:
                try:
                    sites.append(self.load_site(name))
                except FileNotFoundError:
                    missing.append(name)
            if missing:
                names = ", ".join(missing)
                raise FileNotFoundError(
                    f"Site files not found for manifest '{manifest.name}': {names}. "
                    f"Check that matching YAML files exist in the sites/ directory."
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
            return (
                f"No sites in workspace; CLI selector `-l {cli_selector}` "
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
                    # Names matched; another selector key must have
                    # filtered them out. Surface the matched names so
                    # the operator does not get a generic "no match".
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
                    parts.append(
                        f"`{key}={requested_str}` requested. Workspace "
                        f"`{key}` values: {', '.join(values_in_ws)}."
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
            errors.append(str(e))
            sites = []
            selector_parse_failed = True
        except ValueError as e:
            # Site-resolution failure (cycle, overlay-rename, missing
            # field, etc.). Append and continue so other manifest
            # issues still surface in this pass.
            errors.append(str(e))
            sites = []
            selector_parse_failed = False
        except FileNotFoundError as e:
            # Manifest `sites:` entry without a workspace file.
            errors.append(str(e))
            sites = []
            selector_parse_failed = False
        if not sites and (manifest.sites or manifest.site_selector or selector):
            if selector and not selector_parse_failed:
                # Rich diagnostic when CLI selector knocked everything
                # out and the selector itself parsed cleanly.
                errors.append(self.explain_no_match(selector))
            elif not selector:
                errors.append("No sites matched the specified criteria")

        # Validate manifest-level parameter files
        for param_path in manifest.parameters:
            if "{{" in param_path:
                # Dynamic path: validate resolved path for each site
                for site in sites:
                    resolved = manifest.resolve_parameter_path(param_path, site)
                    full_path = (self.workspace / resolved).resolve()
                    if not full_path.exists():
                        errors.append(
                            f"Manifest parameter file not found: {resolved} "
                            f"(resolved from '{param_path}' for site '{site.name}')"
                        )
                    else:
                        try:
                            self.load_parameters(full_path)
                        except Exception as e:
                            errors.append(f"Invalid manifest parameter file {resolved}: {e}")
            else:
                full_path = (self.workspace / param_path).resolve()
                if not full_path.exists():
                    errors.append(f"Manifest parameter file not found: {param_path}")
                else:
                    try:
                        self.load_parameters(full_path)
                    except Exception as e:
                        errors.append(f"Invalid manifest parameter file {param_path}: {e}")

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
                            full_path = (self.workspace / resolved).resolve()
                            if not full_path.exists():
                                errors.append(
                                    f"Parameter file not found: {resolved} "
                                    f"(step: {step.name}, resolved from '{param_path}' for site '{site.name}')"
                                )
                            else:
                                try:
                                    params = self.load_parameters(full_path)
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
            if step.when:
                if not CONDITION_PATTERN.fullmatch(step.when.strip()):
                    errors.append(f"Invalid 'when' condition in step '{step.name}': {step.when}")

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
                            f"Subscription '{sub_id[:8]}...' has RG-level sites ({site_names}) "
                            f"but no subscription-level site for subscription-scoped steps"
                        )
                elif len(sub_level_sites) > 1:
                    # Multiple subscription-level sites for same subscription
                    site_names = ", ".join(s.name for s in sub_level_sites)
                    errors.append(
                        f"Subscription '{sub_id[:8]}...' has multiple subscription-level sites: {site_names}. "
                        f"Only one subscription-level site per subscription is allowed."
                    )

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

    def show_plan(
        self,
        manifest_path: Path,
        selector: str | None = None,
    ) -> None:
        """Display deployment plan without executing.

        Shows which sites will be deployed to and what steps will run.
        Called by 'validate -v' to show the plan after validation passes.

        Args:
            manifest_path: Path to manifest file
            selector: Optional site selector
        """
        manifest = Manifest.from_file(manifest_path, workspace_root=self.workspace)
        sites = self.resolve_sites(manifest, selector)

        if not sites:
            print(f"⚠ No sites matched for manifest '{manifest.name}'")
            if selector:
                print(f"  Selector: {selector}")
            elif manifest.site_selector:
                print(f"  Manifest selector: {manifest.site_selector}")
            print()
            return

        print(f"{'═'*60}")
        print(f"  DEPLOYMENT PLAN: {manifest.name}")
        if selector:
            print(f"  (filtered by: {selector})")
        print(f"{'═'*60}")

        if manifest.description:
            print(f"\n  {manifest.description}")

        print(f"\n  Sites ({len(sites)}):")
        for site in sites:
            print(f"    • {site.name} ({site.location})")

        print(f"\n  Parallel: {manifest.parallel}")

        print(f"\n  Steps ({len(manifest.steps)}):")
        for i, step in enumerate(manifest.steps, 1):
            condition_info = f" [when: {step.when}]" if step.when else ""

            if isinstance(step, KubectlStep):
                print(f"    {i}. {step.name} (kubectl:{step.operation}){condition_info}")
                print(f"       ├─ cluster: {step.arc.name}")
                for j, f in enumerate(step.files):
                    prefix = "└─" if j == len(step.files) - 1 else "├─"
                    print(f"       {prefix} {f}")
            else:
                print(f"    {i}. {step.name} ({step.scope}){condition_info}")
                print(f"       └─ {step.template}")

        print(f"\n{'═'*60}")
        total = sum(
            1
            for site in sites
            for step in manifest.steps
            if self._check_step_site_compatibility(step, site) is None
            and self._evaluate_condition(step.when, site)
        )
        print(f"  Total: {total} operation(s)")

        if len(sites) > 1:
            if manifest.parallel.is_sequential:
                print("  Execution: Sequential (one site at a time)")
            elif manifest.parallel.is_unlimited:
                print("  Execution: Parallel (all sites concurrently)")
            else:
                print(f"  Execution: Parallel (max {manifest.parallel.sites} concurrent)")
        print(f"{'═'*60}\n")

    def deploy(
        self,
        manifest_path: Path,
        selector: str | None = None,
        parallel_override: int | None = None,
        manifest: Manifest | None = None,
        sites: list[Site] | None = None,
    ) -> dict[str, Any]:
        """Execute deployment from manifest.

        Args:
            manifest_path: Path to manifest file
            selector: Optional site selector
            parallel_override: Override manifest's parallel.sites setting.
                              None = use manifest setting.
            manifest: Pre-loaded manifest (avoids re-parsing)
            sites: Pre-resolved sites (avoids re-resolving)

        Returns:
            Dict with deployment results keyed by site name and summary
        """
        if manifest is None:
            manifest = Manifest.from_file(manifest_path, workspace_root=self.workspace)
        if sites is None:
            sites = self.resolve_sites(manifest, selector)

        if not sites:
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

        # Determine effective parallelism
        if parallel_override is not None:
            effective_parallel = ParallelConfig(sites=parallel_override)
        else:
            effective_parallel = manifest.parallel

        logger.info(f"Deploying '{manifest.name}' to {len(sites)} site(s) " f"(parallel: {effective_parallel})")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        start_time = time.time()

        # Group sites by subscription
        site_groups = self._group_sites_by_subscription(sites)

        # Check if we have subscription-scoped steps
        has_sub_steps = self._has_subscription_scoped_steps(manifest)

        results: list[dict[str, Any]] = []
        subscription_outputs: SubscriptionOutputs = {}

        if has_sub_steps:
            # Build map of subscription_id -> subscription-level site
            subscription_sites: dict[str, Site] = {}
            rg_sites: list[Site] = []
            for sub_id, (sub_level, rg_level) in site_groups.items():
                if sub_level:
                    # Use first subscription-level site (validation ensures only one)
                    subscription_sites[sub_id] = sub_level[0]
                rg_sites.extend(rg_level)

            # Phase 1: Execute subscription-scoped steps
            subscription_outputs, sub_results = self._collect_subscription_outputs(
                manifest, subscription_sites, timestamp, effective_parallel
            )
            results.extend(sub_results)

            # Identify failed subscriptions and filter blocked sites
            failed_subscriptions = {
                sub_id
                for sub_id, site in subscription_sites.items()
                if any(r["site"] == site.name and r["status"] == "failed" for r in sub_results)
            }

            # Filter RG-level sites: block those with dependencies on failed subscriptions
            if failed_subscriptions and rg_sites:
                sub_step_names = self._get_subscription_step_names(manifest)
                proceeding_sites = []
                for site in rg_sites:
                    if site.subscription in failed_subscriptions:
                        if self._site_depends_on_subscription_outputs(manifest, site, sub_step_names):
                            # Site depends on failed subscription outputs - block it
                            _thread_safe_print(
                                f"[{site.name}] - blocked "
                                "(subscription deployment failed, site depends on its outputs)"
                            )
                            results.append(
                                {
                                    "site": site.name,
                                    "status": "blocked",
                                    "error": "Subscription deployment failed and site depends on its outputs",
                                    "steps_completed": 0,
                                    "steps_skipped": len(manifest.steps),
                                    "steps_total": len(manifest.steps),
                                    "elapsed": 0.0,
                                    "steps": [],
                                }
                            )
                        else:
                            # Site doesn't depend on subscription outputs - let it proceed
                            proceeding_sites.append(site)
                    else:
                        # Site is in a different subscription - unaffected
                        proceeding_sites.append(site)
                rg_sites = proceeding_sites

            # Phase 2: Execute RG-scoped steps for all RG-level sites
            if rg_sites:
                print(f"\n  [Phase 2] Resource group-scoped steps: {len(rg_sites)} site(s)")
                if effective_parallel.is_sequential or len(rg_sites) == 1:
                    rg_results = self._deploy_sequential(manifest, rg_sites, timestamp, subscription_outputs)
                else:
                    rg_results = self._deploy_parallel(
                        manifest, rg_sites, timestamp, effective_parallel, subscription_outputs
                    )
                results.extend(rg_results)
        else:
            # No subscription-scoped steps - simple execution
            if effective_parallel.is_sequential or len(sites) == 1:
                results = self._deploy_sequential(manifest, sites, timestamp)
            else:
                results = self._deploy_parallel(manifest, sites, timestamp, effective_parallel)

        total_elapsed = time.time() - start_time

        # Build summary
        succeeded = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")

        summary = {
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "elapsed": total_elapsed,
        }

        # Print summary
        self._print_deployment_summary(results, total_elapsed)

        return {
            "sites": {r["site"]: r for r in results},
            "summary": summary,
        }
