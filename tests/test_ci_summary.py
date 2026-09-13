"""Exercise the CI summary against JUnit failure, error and success records."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def render_summary(tmp_path, monkeypatch):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8"))
    program = next(
        step["run"] for step in workflow["jobs"]["test"]["steps"] if step["name"] == "Test Summary"
    )
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def render(document):
        if document is not None:
            (tmp_path / "test-results.xml").write_text(document, encoding="utf-8")
        try:
            exec(compile(program, "ci-test-summary", "exec"), {})
        except SystemExit as error:
            assert error.code == 0
        return summary.read_text(encoding="utf-8")

    return render


@pytest.mark.parametrize("kind", ["failure", "error"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_summary_reports_leaf_failure_and_error_elements(render_summary, kind, wrapped):
    document = (
        f'<testsuite tests="1" failures="{int(kind == "failure")}" errors="{int(kind == "error")}" time="1.5">'
        '<testcase classname="tests.example" name="test_case">'
        f'<{kind} message="expected diagnostic" />'
        "</testcase></testsuite>"
    )
    if wrapped:
        document = "<testsuites>" + document + "</testsuites>"
    summary = render_summary(document)
    assert "### Failed Tests" in summary
    assert "`tests.example::test_case`: expected diagnostic" in summary
    assert f'| 0 | {int(kind == "failure")} | {int(kind == "error")} | 0 | 1 | 1.5s |' in summary


def test_summary_preserves_success_and_skip_counts(render_summary):
    summary = render_summary(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1" time="2">'
        '<testcase name="passed" /><testcase name="skipped"><skipped /></testcase>'
        "</testsuite></testsuites>",
    )
    assert "| 1 | 0 | 0 | 1 | 2 | 2.0s |" in summary
    assert "Failed Tests" not in summary


def test_summary_reports_missing_results(render_summary):
    assert "No test results found." in render_summary(None)
