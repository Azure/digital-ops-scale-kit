"""Parse repository YAML documents once per test session."""

import copy
import functools
from typing import Any

import yaml


@functools.lru_cache(maxsize=64)
def _parsed(text: str) -> Any:
    return yaml.safe_load(text)


def cached_safe_load(text: str) -> Any:
    """Return `yaml.safe_load(text)` as a copy that the caller may change.

    Each distinct text is parsed once, so tests that read the same workflow
    many times avoid parsing it again. Every call returns its own copy.
    """
    return copy.deepcopy(_parsed(text))
