# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Validate the engine version range and the features shared by workspace authors and consumers."""

import unicodedata
from collections.abc import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from siteops.artifacts import ArtifactError

COMPILED_TEMPLATE_FEATURE = "compiled-templates/v1"
TYPED_INPUT_MAPS_FEATURE = "typed-input-maps/v1"
STEP_REPORTS_FEATURE = "step-reports/v1"
SUPPORTED_FEATURES = frozenset({
    "manifest/v1",
    "composition/v1",
    "manifest-selection/v1",
    COMPILED_TEMPLATE_FEATURE,
    TYPED_INPUT_MAPS_FEATURE,
    STEP_REPORTS_FEATURE,
})
MAX_REQUIRED_FEATURES = 64
MAX_FEATURE_LENGTH = 128


def feature_inventory_problem(features: object) -> str | None:
    """Return why a required-feature list is malformed, or None when it is valid.

    Packages and Manifests share these limits: at most 64 unique, nonblank,
    printable names of at most 128 characters.
    """
    if not isinstance(features, list) or len(features) > MAX_REQUIRED_FEATURES:
        return "must be a list of at most 64 feature names"
    for item in features:
        if (
            not isinstance(item, str) or not item.strip() or len(item) > MAX_FEATURE_LENGTH
            or any(
                unicodedata.category(character).startswith("C")
                or unicodedata.category(character) in {"Zl", "Zp"}
                for character in item
            )
        ):
            return "must hold nonblank printable names of at most 128 characters"
    if len(set(features)) != len(features):
        return "must not repeat a feature"
    return None


def unsupported_features(
    required: Iterable[str],
    supported: frozenset[str] = SUPPORTED_FEATURES,
) -> tuple[str, ...]:
    """Return the required features this engine does not support, sorted."""
    return tuple(sorted(set(required) - supported))


def validate_engine_range(value: str) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 256
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ArtifactError("Workspace package text fields must be bounded and printable.")
    try:
        specifiers = SpecifierSet(value)
    except InvalidSpecifier:
        raise ArtifactError("The Site Ops compatibility range is invalid.") from None
    operators = {item.operator for item in specifiers}
    if "===" in operators or not (
        operators & {"==", "~="}
        or (operators & {">", ">="} and operators & {"<", "<="})
    ):
        raise ArtifactError("The Site Ops compatibility range must have lower and upper bounds.")
    return value


def require_engine_version(siteops_range: str, engine_version: str) -> None:
    """Require the target engine version to satisfy the bounded workspace declaration."""
    validate_engine_range(siteops_range)
    try:
        compatible = SpecifierSet(siteops_range).contains(Version(engine_version), prereleases=True)
    except (InvalidVersion, InvalidSpecifier):
        raise ArtifactError("The Site Ops compatibility declaration is invalid.") from None
    if not compatible:
        raise ArtifactError("This package requires a different Site Ops version.")
