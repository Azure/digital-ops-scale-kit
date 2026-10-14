"""Check expression behavior against workflow conditions."""

from pathlib import Path

import pytest
import yaml

from tests.actions_expressions import (
    _FUNCTIONS,
    ExpressionError,
    dispatch_inputs,
    evaluate,
    iter_conditions,
    job_runs,
    parse,
    truthy,
)

ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_UNSUPPORTED = set()


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("!true == false", True),
        ("!1 == 2", False),
        ("true || false && false", True),
        ("false || true && false", False),
        ("0 || 'fallback'", "fallback"),
        ("'first' && 42", 42),
        ("'first' || 'second'", "first"),
        ("false && 42", False),
        ("'HELLO' == 'hello'", True),
        ("'a' < 'B'", True),
        ("'1' == 1", True),
        ("null == 0", True),
        ("'' == 0", True),
        ("false == 0", True),
        ("true == 1", True),
        ("'abc' == 0", False),
        ("'abc' != 0", True),
        ("'abc' < 0", False),
        ("'abc' >= 0", False),
        ("'12' > 3", True),
        ("'2' < '10'", False),
        ("'Straße' == 'STRASSE'", False),
        ("0x10 == 16", True),
        ("-0x10 == -16", True),
        ("-1.25e2 == -125", True),
        ("'It''s ready' == 'it''s ready'", True),
        ("contains('HELLO', 'ell')", True),
        ("contains(fromJSON('[1,\"A\",null]'), 'a')", True),
        ("contains(fromJSON('[1,2]'), '1')", True),
        ("startsWith('Release', 'REL')", True),
        ("endsWith('Release', 'ASE')", True),
        ("FORMAT('{{{0}}} and {1}', 'ok', true)", "{ok} and true"),
        ("join(fromJSON('[\"a\",null,2]'), ':')", "a::2"),
        ("join('abc', ':')", "abc"),
        ("fromJSON(toJSON(fromJSON('[1,2]')))[1]", 2),
        ("fromJSON('[\"a\"]')[0]", "a"),
        ("fromJSON('null')", None),
        ("${{ true }}", True),
    ],
)
def test_expression_values(expression, expected):
    assert evaluate(expression, {}) == expected


def test_context_paths_missing_properties_and_identity():
    shared = ["item"]
    contexts = {
        "inputs": {"aio-releases": "2608"},
        "needs": {"fleet-request": {"result": "success"}},
        "data": {"a": shared, "same": shared, "other": ["item"], "items": [10],
                 "position": 0},
    }
    assert evaluate("inputs.aio-releases", contexts) == "2608"
    assert evaluate("needs['fleet-request'].result", contexts) == "success"
    assert evaluate("data['items'][0]", contexts) == 10
    assert evaluate("data.items[data.position]", contexts) == 10
    assert evaluate("data.items[2]", contexts) is None
    assert evaluate("data.missing.inner", contexts) is None
    assert evaluate("data.a == data.same", contexts) is True
    assert evaluate("data.a == data.other", contexts) is False
    assert evaluate("fromJSON('[1]') == fromJSON('[1]')", {}) is False
    assert evaluate("!data.missing == true", contexts) is True
    assert evaluate("data.missing || data.items[0] && 'selected'", contexts) == "selected"


@pytest.mark.parametrize("expression", ["typo.value", "true || typo.value", "false && typo"])
def test_unknown_top_level_context_always_raises(expression):
    with pytest.raises(ExpressionError, match=r"Unknown context: typo\."):
        evaluate(expression, {"inputs": {}})


@pytest.mark.parametrize("expression", ["hashFiles('*.py')", "false && hashFiles('*.py')"])
def test_unsupported_function_always_raises(expression):
    with pytest.raises(ExpressionError, match=r"Unsupported function: hashfiles\."):
        evaluate(expression, {})


@pytest.mark.parametrize(
    "expression",
    ["", "(", "true = false", '"double quoted"', "'unterminated", "fromJSON('[1]')[",
     "true &&", "f(1,)", "${{ true }} and false", "true || false )", "01", ".5"],
)
def test_syntax_errors_use_one_message(expression):
    with pytest.raises(ExpressionError, match=r"^Invalid Actions expression\.$"):
        parse(expression)


def test_truthiness_and_string_casts():
    for value in (None, False, 0, -0.0, "", float("nan")):
        assert not truthy(value)
    for value in ([], {}, "false", -2):
        assert truthy(value)
    assert truthy(10**400)
    with pytest.raises(ExpressionError, match="object or array"):
        evaluate("format('{0}', fromJSON('{}'))", {})
    with pytest.raises(ExpressionError, match="Invalid format string"):
        evaluate("format('{bad}', 1)", {})
    with pytest.raises(ExpressionError, match="Invalid JSON value"):
        evaluate("fromJSON('NaN')", {})


def test_status_functions_use_direct_needs():
    contexts = {"needs": {"admit": {"result": "skipped"}}}
    assert not evaluate("success()", contexts)
    assert not evaluate("failure()", contexts)
    assert evaluate("always()", contexts, cancelled=True)
    assert evaluate("cancelled()", contexts, cancelled=True)
    contexts["needs"]["admit"]["result"] = "failure"
    assert evaluate("failure()", contexts)
    assert not evaluate("success()", contexts)


def test_implicit_success_and_explicit_status_checks():
    workflow = {
        "jobs": {
            "plain": {"needs": "admit", "if": "inputs.x == 'y'"},
            "explicit": {"needs": ["admit"], "if": "!cancelled() && inputs.x == 'y'"},
            "always": {"needs": "admit", "if": "always()"},
            "default": {"needs": "admit"},
        },
    }
    options = {"inputs": {"x": "y"}, "needs": {"admit": "failure"}}
    assert not job_runs(workflow, "plain", **options)
    assert job_runs(workflow, "explicit", **options)
    assert job_runs(workflow, "always", **options, cancelled=True)
    assert not job_runs(workflow, "default", **options)
    assert not job_runs(workflow, "default", inputs={}, needs={"admit": "skipped"})
    assert job_runs(workflow, "default", inputs={}, needs={"admit": "success"})
    assert not job_runs(workflow, "explicit", **options, cancelled=True)


def test_job_needs_must_match_declared_dependencies():
    workflow = {"jobs": {"test": {"needs": ["admit", "prep"], "if": "always()"}}}
    for needs in (None, {"admit": "success"}, {"admit": "success", "prep": "success",
                                                "extra": "success"}):
        with pytest.raises(ExpressionError, match="Need results must match"):
            job_runs(workflow, "test", inputs={}, needs=needs)
    with pytest.raises(ExpressionError, match="Outputs must belong"):
        job_runs(workflow, "test", inputs={}, needs={"admit": "success", "prep": "success"},
                 outputs={"extra": {}})
    with pytest.raises(ExpressionError, match="Invalid job dependency result"):
        job_runs(workflow, "test", inputs={}, needs={"admit": "unknown", "prep": "success"})


def test_job_needs_outputs_are_available_to_conditions():
    workflow = {"jobs": {"test": {"needs": "admit", "if": "needs.admit.outputs.accepted == 'yes'"}}}
    assert job_runs(workflow, "test", inputs={}, needs={"admit": "success"},
                    outputs={"admit": {"accepted": "yes"}})
    assert not job_runs(workflow, "test", inputs={}, needs={"admit": "success"})


def test_dispatch_defaults_and_overrides():
    workflow = yaml.safe_load("""
on:
  workflow_dispatch:
    inputs:
      enabled: {type: boolean, default: 'false'}
      count: {type: number, default: '2.5'}
      aio-releases: {type: string, default: 2608}
      name: {type: choice, default: production}
""")
    assert dispatch_inputs(workflow) == {
        "enabled": False, "count": 2.5, "aio-releases": "2608", "name": "production",
    }
    assert dispatch_inputs(workflow, aio_releases="2609", enabled=True)["aio-releases"] == "2609"
    assert dispatch_inputs(workflow, {"count": "3", "aio-releases": "2610"})["count"] == 3
    for overrides in ({"unknown": 1}, {"aio_release": "2609"}):
        with pytest.raises(ExpressionError, match="Unknown dispatch input"):
            dispatch_inputs(workflow, overrides)


def _declared_inputs(document: dict) -> set[str]:
    triggers = document.get("on", document.get(True)) or {}
    if not isinstance(triggers, dict):
        return set(document.get("inputs") or {})
    return {name for trigger in ("workflow_dispatch", "workflow_call")
            for name in ((triggers.get(trigger) or {}).get("inputs") or {})}


def _condition_problems(document: dict) -> tuple[int, list[tuple[str, str]]]:
    """Return the number of conditions and each unsupported call or undeclared input."""
    declared = set(document.get("inputs") or {}) if "runs" in document else _declared_inputs(document)
    checked, problems = 0, []
    for location, condition in iter_conditions(document):
        pending = [parse(condition)]
        while pending:
            part = pending.pop()
            if part.kind == "call" and part.value not in _FUNCTIONS | _ALLOWED_UNSUPPORTED:
                problems.append((location, "function " + part.value))
            # A misspelled input reads as null on GitHub, so every referenced input must be declared.
            if (part.kind == "index" and part.children[0].kind == "name" and part.children[0].value == "inputs"
                    and part.children[1].kind == "literal" and part.children[1].value not in declared):
                problems.append((location, "input " + part.children[1].value))
            pending.extend(part.children)
        checked += 1
    return checked, problems


def test_every_workflow_and_composite_step_condition_parses():
    paths = sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
    paths += sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    paths += sorted((ROOT / ".github" / "actions").rglob("action.yaml"))
    paths += sorted((ROOT / ".github" / "actions").rglob("action.yml"))
    total = 0
    for path in paths:
        checked, problems = _condition_problems(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert problems == [], (path, problems)
        total += checked
    assert total > 0


@pytest.mark.parametrize(("condition", "expected"), [
    ("inputs.scenarios == 'fleet'", [("jobs.run", "input scenarios")]),
    ("hashFiles('x') != ''", [("jobs.run", "function hashfiles")]),
    ("inputs.scenario == 'fleet'", []),
])
def test_condition_guard_reports_undeclared_inputs_and_unsupported_calls(condition, expected):
    document = {"on": {"workflow_dispatch": {"inputs": {"scenario": {"type": "string"}}}},
                "jobs": {"run": {"if": condition}}}
    assert _condition_problems(document) == (1, expected)
