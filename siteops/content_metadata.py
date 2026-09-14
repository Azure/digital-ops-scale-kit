# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Pure shape validation shared by authored guidance and generated indexes."""

from typing import Any

API_VERSION = "siteops/v1alpha1"


def require_mapping(
    value: Any, allowed: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Require string keys and, when supplied, a closed set of fields."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected a string-keyed mapping.")
    if allowed is not None and value.keys() - allowed:
        raise ValueError("Unknown metadata field.")
    return value


def require_text(value: Any, *, empty: bool = False) -> str:
    """Require text without coercing numbers, nulls or containers."""
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError("Expected text.")
    return value


def require_text_list(value: Any) -> tuple[str, ...]:
    """Require unique nonblank text values while preserving declared order."""
    if not isinstance(value, list):
        raise ValueError("Expected a list.")
    result = tuple(require_text(item) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("Duplicate list item.")
    return result


def validate_envelope(
    value: Any, kind: str, allowed: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Require the content metadata version and expected document kind."""
    data = require_mapping(value, allowed)
    if data.get("apiVersion") != API_VERSION or data.get("kind") != kind:
        raise ValueError("Unsupported metadata version or kind.")
    return data
