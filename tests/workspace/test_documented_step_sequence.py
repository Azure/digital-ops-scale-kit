"""Tests that a manifest's documented step sequence matches what it deploys.

Several manifests list their post-flatten step sequence in the `description:`
field, so a reader can see the order without running `siteops validate -v`. That
listing is hand-maintained and has no other guard, which makes it the first thing
to go stale when includes are added or removed.

The check is opt-in by convention: a manifest participates by writing a
"Post-flatten step sequence" block of numbered lines. A manifest without one is
skipped, so this never forces the documentation on anyone.
"""

import re

from siteops.models import Manifest
from tests.workspace.test_manifest_validation import _all_manifest_files

# `   10. opc-plc-simulator      (gated by enableSecretSync)` -> "opc-plc-simulator"
_SEQUENCE_HEADING = re.compile(r"post-flatten step sequence", re.IGNORECASE)
_NUMBERED_STEP = re.compile(r"^\s*(\d+)\.\s+(\S+)")

# Manifests documenting a post-flatten sequence today. A heading that changed
# would otherwise remove that manifest from this check while the remaining ones
# keep the test green, which is how a documented sequence goes stale unnoticed.
# Raise this when a manifest starts documenting one.
_DOCUMENTED_SEQUENCE_FLOOR = 2


def _documented_sequence(description: str) -> list[str] | None:
    """Extract the numbered step names from a description, or None if absent."""
    if not description or not _SEQUENCE_HEADING.search(description):
        return None

    names: list[str] = []
    started = False
    for line in description.splitlines():
        if _SEQUENCE_HEADING.search(line):
            started = True
            continue
        if not started:
            continue
        match = _NUMBERED_STEP.match(line)
        if match:
            names.append(match.group(2))
        elif names and line.strip():
            # A non-numbered, non-blank line after the list ends the block.
            break
    return names


class TestDocumentedStepSequence:
    """A documented sequence names the steps the manifest actually flattens to."""

    def test_documented_sequences_match_the_flattened_steps(self, workspace):
        checked = 0
        failures: list[str] = []

        for path in _all_manifest_files(workspace):
            manifest = Manifest.from_file(path, workspace_root=workspace)
            documented = _documented_sequence(manifest.description or "")
            if documented is None:
                continue
            checked += 1
            actual = [step.name for step in manifest.steps]
            if documented != actual:
                failures.append(
                    f"{path.relative_to(workspace)} documents a step sequence that "
                    f"does not match what it deploys.\n"
                    f"    documented: {documented}\n"
                    f"    actual:     {actual}"
                )

        assert checked >= _DOCUMENTED_SEQUENCE_FLOOR, (
            f"Only {checked} manifest(s) documented a post-flatten step "
            f"sequence, down from {_DOCUMENTED_SEQUENCE_FLOOR}. A manifest whose "
            f"heading changed stops being checked here while the others keep "
            f"this test green. Restore the heading, or lower the floor "
            f"deliberately if a sequence was removed on purpose."
        )
        assert not failures, "\n\n".join(failures)
