#!/usr/bin/env python3
"""Register override strings with the selected CI host before consuming them."""

import argparse
import json
import os
import sys


def mask_commands(raw: str, platform: str) -> list[str]:
    """Encode complete values and their individual lines as data, not commands."""
    if len(raw) > 1024 * 1024:
        raise ValueError("Override input exceeds its supported size.")
    document = json.loads(raw) if raw.strip() else {}
    if not isinstance(document, dict) or any(
        not isinstance(value, dict) for value in document.values()
    ):
        raise ValueError("Overrides must map Site names to field mappings.")
    values = set(document)

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(document)
    values.update(line for value in list(values) for line in value.splitlines())
    if any("\x00" in value for value in values):
        raise ValueError("Override strings cannot contain NUL.")
    if platform == "github":
        prefix, percent = "::add-mask::", "%25"
    elif platform == "azure-pipelines":
        prefix, percent = "##vso[task.setsecret]", "%AZP25"
    else:
        raise ValueError("Select a supported CI host.")
    return [
        prefix + value.replace("%", percent).replace("\r", "%0D").replace("\n", "%0A")
        for value in sorted(values) if value
    ]


def main() -> int:
    """Read private input from the environment and publish only encoded masks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("github", "azure-pipelines"))
    args = parser.parse_args()
    try:
        commands = mask_commands(os.environ.get("SITE_OVERRIDES", ""), args.platform)
    except (ValueError, RecursionError):
        print("Site overrides could not be masked. Check the JSON locally.", file=sys.stderr)
        return 1
    for command in commands:
        print(command, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
