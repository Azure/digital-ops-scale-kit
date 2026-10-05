"""Keep the pinned download action's singleton layout aligned with declared inventories."""

import fnmatch
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/_release-candidate.yaml").read_text())


def step(job, name):
    return next(row for row in WORKFLOW["jobs"][job]["steps"] if row.get("name") == name)


@pytest.mark.parametrize("kind", ["workspace", "qualification"])
@pytest.mark.parametrize("count", [1, 2])
def test_declared_download_inventory_keeps_names_for_single_and_multiple_artifacts(
    tmp_path, monkeypatch, capsys, kind, count,
):
    for name, value in {
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "3", "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    if kind == "workspace":
        plan = {
            "active": True, "release": {"tag": "v0.0.0.dev0"},
            "siteops": {"bundle": True, "versionMode": "build"},
            "intent": {"path": ".github/release-examples/workspace-preview/release.json"},
            "workspaces": [{"workspace": f"workspace-{index}"} for index in range(count)],
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        monkeypatch.setattr(sys, "argv", ["summary", str(path)])
        body = step("prepare", "Prepare the immutable declaration")["run"]
        producer, output, output_step = "prepare", "single-workspace-artifact", "plan"
        download = step("workspace-assets", "Download attested workspace subjects")["with"]
        directory = "workspace-staging"
        expected = [f"workspace-attested-42-3-{index}" for index in range(1, count + 1)]
    else:
        targets = [
            {"python": "3.11", "platform": platform}
            for platform in ("linux-x86_64", "windows-x86_64")[:count]
        ]
        selection = {
            "selectionSha256": "a" * 64, "matrix": {"include": targets},
            "engineReferenceSha256": "b" * 64, "engineRevision": "c" * 40,
            "engineVersion": "1.0.0b1+build.42.3.gcccccccccccc",
        }
        (tmp_path / "engine-selection.json").write_text(json.dumps(selection))
        body = step("engine-input", "Freeze the selected engine")["run"]
        producer, output, output_step = "engine-input", "single-qualification-artifact", "select"
        download = step("workspace-qualified", "Download every qualification cell")["with"]
        directory = "qualification-results"
        expected = [f"workspace-qualification-42-3-{target['platform']}-{target['python']}" for target in targets]
    programs = re.findall(r"python3? -c '([^']*)'", body)
    assert len(programs) == 1
    exec(compile(programs[0], "<artifact-inventory>", "exec"), {})
    values = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    if kind == "qualification":
        for key, field in (
            ("selection-sha", "selectionSha256"), ("reference-sha", "engineReferenceSha256"),
            ("engine-revision", "engineRevision"), ("engine-version", "engineVersion"),
        ):
            assert values[key] == selection[field]
            assert WORKFLOW["jobs"][producer]["outputs"][key] == "${{ steps.select.outputs." + key + " }}"
        assert json.loads(values["matrix"]) == selection["matrix"]
    assert values[output] == (expected[0] if count == 1 else "")
    assert WORKFLOW["jobs"][producer]["outputs"][output] == "${{ steps." + output_step + ".outputs." + output + " }}"
    expression = "${{ needs." + producer + ".outputs." + output + " }}"
    assert download["name"] == expression
    assert download["merge-multiple"] is False
    assert download["digest-mismatch"] == "error"

    def render(value):
        for key, replacement in {
            expression: values[output], "${{ runner.temp }}": tmp_path.as_posix(),
            "${{ github.run_id }}": "42", "${{ github.run_attempt }}": "3",
        }.items():
            value = value.replace(key, replacement)
        assert "${{" not in value
        return value

    name, pattern = render(download["name"]), render(download["pattern"])

    def select(available):
        return [item for item in available if item == name] if name else fnmatch.filter(available, pattern)

    selected = select(expected)
    assert selected == expected
    if count == 1:
        assert select([expected[0] + "-unexpected"]) == []
    for artifact in selected:
        # Pinned actions/download-artifact flattens a sole match, even with merge-multiple=false.
        destination = Path(render(download["path"]))
        if not (name or download["merge-multiple"] or len(selected) == 1):
            destination /= artifact
        destination.mkdir(parents=True)
        assert destination == tmp_path / directory / artifact
    assert {path.name for path in (tmp_path / directory).iterdir()} == set(expected)
