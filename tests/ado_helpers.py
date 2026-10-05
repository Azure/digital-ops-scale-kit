"""Read fixed local template references without evaluating Azure expressions."""

from pathlib import Path

import yaml

TEMPLATES = Path(__file__).resolve().parents[1] / ".pipelines" / "templates"


def nodes(path):
    def walk(value):
        if isinstance(value, dict):
            yield value
            reference = value.get("template")
            if isinstance(reference, str) and reference.endswith(".yaml") and "/" not in reference:
                yield from nodes(TEMPLATES / reference)
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
    yield from walk(yaml.safe_load(path.read_text(encoding="utf-8")))


def step(path, name):
    matches = [node for node in nodes(path) if node.get("displayName", node.get("name")) == name]
    assert len(matches) == 1
    return matches[0]
