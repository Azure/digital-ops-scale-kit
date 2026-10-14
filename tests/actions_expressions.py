"""Evaluate GitHub Actions conditions in tests without running a workflow."""

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterator

_SYNTAX = "Invalid Actions expression."
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<string>'(?:''|[^'])*')|"
    r"(?P<number>-?(?:0[xX][0-9a-fA-F]+|(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?))|"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9-]*)|"
    r"(?P<operator>&&|\|\||==|!=|<=|>=|[()[\].,!<>])"
)
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")
_PRECEDENCE = {"||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, "<=": 4, ">": 4, ">=": 4}
_FUNCTIONS = {
    "contains", "startswith", "endswith", "format", "join", "tojson", "fromjson",
    "success", "failure", "cancelled", "always",
}
_STATUS = {"success", "failure", "cancelled", "always"}


class ExpressionError(ValueError):
    """Report an expression that cannot be parsed or evaluated."""


@dataclass(frozen=True)
class Node:
    """Hold an expression operation, its value, and its operands."""

    kind: str
    value: Any = None
    children: tuple["Node", ...] = ()


class _Parser:
    def __init__(self, text: str):
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = _TOKEN.match(text, position)
            if not match:
                raise ExpressionError(_SYNTAX)
            if match.lastgroup != "space":
                self.tokens.append((match.lastgroup or "", match.group()))
            position = match.end()
        self.tokens.append(("end", ""))
        self.position = 0

    def peek(self) -> str:
        return self.tokens[self.position][1]

    def take(self, expected: str | None = None) -> tuple[str, str]:
        token = self.tokens[self.position]
        if expected is not None and token[1] != expected:
            raise ExpressionError(_SYNTAX)
        self.position += 1
        return token

    def expression(self, minimum: int = 1) -> Node:
        if self.peek() == "!":
            self.take("!")
            left = Node("not", children=(self.expression(5),))
        else:
            left = self.primary()
        while _PRECEDENCE.get(self.peek(), 0) >= minimum:
            operator = self.peek()
            self.take(operator)
            left = Node("binary", operator, (left, self.expression(_PRECEDENCE[operator] + 1)))
        return left

    def primary(self) -> Node:
        kind, value = self.take()
        if value == "(":
            node = self.expression()
            self.take(")")
        elif kind == "string":
            node = Node("literal", value[1:-1].replace("''", "'"))
        elif kind == "number":
            node = Node("literal", int(value, 16) if "0x" in value.lower() else json.loads(value))
        elif kind == "name":
            lower = value.lower()
            if lower in {"true", "false", "null"}:
                node = Node("literal", {"true": True, "false": False, "null": None}[lower])
            elif self.peek() == "(":
                self.take("(")
                args = []
                if self.peek() != ")":
                    while True:
                        args.append(self.expression())
                        if self.peek() != ",":
                            break
                        self.take(",")
                self.take(")")
                node = Node("call", lower, tuple(args))
            else:
                node = Node("name", lower)
        else:
            raise ExpressionError(_SYNTAX)
        while self.peek() in (".", "["):
            if self.peek() == ".":
                self.take(".")
                kind, key = self.take()
                if kind != "name":
                    raise ExpressionError(_SYNTAX)
                node = Node("index", children=(node, Node("literal", key)))
            else:
                self.take("[")
                key = self.expression()
                self.take("]")
                node = Node("index", children=(node, key))
        return node


def parse(text: str) -> Node:
    """Parse one bare condition or one complete expression wrapper."""
    if not isinstance(text, str):
        raise ExpressionError(_SYNTAX)
    text = text.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    parser = _Parser(text)
    node = parser.expression()
    if parser.peek() != "":
        raise ExpressionError(_SYNTAX)
    return node


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def truthy(value: Any) -> bool:
    """Apply the truth rules used by Actions conditionals."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (int, float)) and (value == 0 or
                                             isinstance(value, float) and math.isnan(value)):
        return False
    return True


def _fold(value: str) -> str:
    return "".join(lower if len(lower := char.lower()) == 1 else char for char in value)


def _number(value: Any) -> float | int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0
        if _JSON_NUMBER.fullmatch(value):
            return float(value)
    return math.nan


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left is right
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        if isinstance(right, (int, float)) and not isinstance(right, bool):
            return left == right
    elif type(left) is type(right):
        return _fold(left) == _fold(right) if isinstance(left, str) else left == right
    return _number(left) == _number(right)


def _compare(left: Any, right: Any, operator: str) -> bool:
    if operator in ("==", "!="):
        equal = _equal(left, right)
        return equal if operator == "==" else not equal
    if isinstance(left, str) and isinstance(right, str):
        left, right = _fold(left), _fold(right)
    else:
        left, right = _number(left), _number(right)
    if isinstance(left, float) and math.isnan(left) or isinstance(right, float) and math.isnan(right):
        return False
    return {"<": lambda: left < right, "<=": lambda: left <= right,
            ">": lambda: left > right, ">=": lambda: left >= right}[operator]()


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        raise ExpressionError("Cannot convert an object or array to a string.")
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if value == 0:
            return "0"
        value = str(value).removesuffix(".0")
        return re.sub(r"e([+-])0+(\d+)", r"e\1\2", value)
    return str(value)


def _index(container: Any, key: Any) -> Any:
    if isinstance(container, dict):
        name = _string(key)
        return next((value for field, value in container.items()
                     if isinstance(field, str) and _fold(field) == _fold(name)), None)
    if isinstance(container, (list, tuple)):
        number = _number(key)
        if (not isinstance(number, float) or math.isfinite(number)) and (
            0 <= number < len(container) and int(number) == number
        ):
            return container[int(number)]
    return None


def _format(template: str, values: list[Any]) -> str:
    parts = re.split(r"(\{\{|\}\}|\{\d+\})", template)
    for index in range(0, len(parts), 2):
        if "{" in parts[index] or "}" in parts[index]:
            raise ExpressionError("Invalid format string.")
    for index in range(1, len(parts), 2):
        token = parts[index]
        if token == "{{" or token == "}}":
            parts[index] = token[0]
        else:
            position = int(token[1:-1])
            if position >= len(values):
                raise ExpressionError("Invalid format placeholder.")
            parts[index] = _string(values[position])
    return "".join(parts)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}.")


def _call(name: str, args: list[Any], needs: dict, cancelled: bool) -> Any:
    expected = {"contains": (2, 2), "startswith": (2, 2), "endswith": (2, 2),
                "format": (2, None), "join": (1, 2), "tojson": (1, 1),
                "fromjson": (1, 1), "success": (0, 0), "failure": (0, 0),
                "cancelled": (0, 0), "always": (0, 0)}[name]
    if len(args) < expected[0] or expected[1] is not None and len(args) > expected[1]:
        raise ExpressionError(f"Invalid argument count for {name}.")
    if name == "success":
        return not cancelled and all(job["result"] == "success" for job in needs.values())
    if name == "failure":
        return any(job["result"] == "failure" for job in needs.values())
    if name == "cancelled":
        return cancelled
    if name == "always":
        return True
    if name == "contains":
        return (any(_equal(item, args[1]) for item in args[0]) if isinstance(args[0], list)
                else _fold(_string(args[1])) in _fold(_string(args[0])))
    if name == "startswith":
        return _fold(_string(args[0])).startswith(_fold(_string(args[1])))
    if name == "endswith":
        return _fold(_string(args[0])).endswith(_fold(_string(args[1])))
    if name == "format":
        return _format(_string(args[0]), args[1:])
    if name == "join":
        separator = _string(args[1]) if len(args) == 2 else ","
        return (separator.join(_string(item) for item in args[0])
                if isinstance(args[0], list) else _string(args[0]))
    if name == "tojson":
        return json.dumps(args[0], indent=2, ensure_ascii=False, allow_nan=False)
    try:
        return json.loads(_string(args[0]), parse_constant=_reject_json_constant)
    except ValueError as error:
        raise ExpressionError("Invalid JSON value.") from error


def _evaluate(node: Node, contexts: dict, needs: dict, cancelled: bool) -> Any:
    if node.kind == "literal":
        return node.value
    if node.kind == "name":
        return contexts[node.value]
    if node.kind == "not":
        return not truthy(_evaluate(node.children[0], contexts, needs, cancelled))
    if node.kind == "index":
        return _index(*(_evaluate(child, contexts, needs, cancelled) for child in node.children))
    if node.kind == "call":
        return _call(node.value, [_evaluate(child, contexts, needs, cancelled)
                                  for child in node.children], needs, cancelled)
    left = _evaluate(node.children[0], contexts, needs, cancelled)
    if node.value == "&&":
        return _evaluate(node.children[1], contexts, needs, cancelled) if truthy(left) else left
    if node.value == "||":
        return left if truthy(left) else _evaluate(node.children[1], contexts, needs, cancelled)
    return _compare(left, _evaluate(node.children[1], contexts, needs, cancelled), node.value)


def _run(node: Node, contexts: dict, needs: dict | None, cancelled: bool) -> Any:
    names = {name.lower(): value for name, value in contexts.items()}
    for part in _walk(node):
        if part.kind == "name" and part.value not in names:
            raise ExpressionError(f"Unknown context: {part.value}.")
        if part.kind == "call" and part.value not in _FUNCTIONS:
            raise ExpressionError(f"Unsupported function: {part.value}.")
    return _evaluate(node, names, needs if needs is not None else names.get("needs", {}), cancelled)


def evaluate(
    text: str, contexts: dict, *, needs: dict | None = None, cancelled: bool = False,
) -> Any:
    """Evaluate an expression and return its value without an implicit status check."""
    return _run(parse(text), contexts, needs, cancelled)


def _input_value(value: Any, kind: str) -> Any:
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
    elif kind == "number":
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and _JSON_NUMBER.fullmatch(value):
            return json.loads(value)
    else:
        return _string(value)
    raise ExpressionError(f"Invalid {kind} input value.")


def dispatch_inputs(
    workflow: dict, overrides: dict | None = None, **selected: Any,
) -> dict:
    """Return typed dispatch defaults with validated overrides."""
    definitions = workflow.get("on", workflow.get(True, {})).get(
        "workflow_dispatch", {},
    ).get("inputs", {})
    values = {}
    for name, specification in definitions.items():
        kind = specification.get("type", "string")
        default = specification.get("default", False if kind == "boolean" else 0
                                    if kind == "number" else "")
        values[name] = _input_value(default, kind)
    for key, value in {**(overrides or {}), **selected}.items():
        name = key if key in definitions else key.replace("_", "-")
        if name not in definitions:
            raise ExpressionError(f"Unknown dispatch input: {key}.")
        values[name] = _input_value(value, definitions[name].get("type", "string"))
    return values


def job_runs(
    workflow: dict, job_id: str, *, inputs: dict, needs: dict[str, str] | None = None,
    outputs: dict[str, dict] | None = None, cancelled: bool = False,
    github: dict | None = None, secrets: dict | None = None, vars: dict | None = None,
) -> bool:
    """Evaluate a job condition with its direct dependency results."""
    job = workflow["jobs"][job_id]
    declared = job.get("needs", [])
    declared = {declared} if isinstance(declared, str) else set(declared)
    results = needs or {}
    if set(results) != declared:
        raise ExpressionError("Need results must match the job dependencies.")
    if set(outputs or {}) - declared:
        raise ExpressionError("Outputs must belong to a job dependency.")
    if set(results.values()) - {"success", "failure", "cancelled", "skipped"}:
        raise ExpressionError("Invalid job dependency result.")
    records = {key: {"result": result, "outputs": (outputs or {}).get(key, {})}
               for key, result in results.items()}
    node = parse(job.get("if", "success()"))
    if not any(part.kind == "call" and part.value in _STATUS for part in _walk(node)):
        node = Node("binary", "&&", (Node("call", "success"), node))
    contexts = {"inputs": inputs, "needs": records, "github": github or {},
                "secrets": secrets or {}, "vars": vars or {}}
    return truthy(_run(node, contexts, records, cancelled))


def iter_conditions(workflow: dict) -> Iterator[tuple[str, str]]:
    """Yield job and step conditions with their locations."""
    for job_id, job in (workflow.get("jobs") or {}).items():
        if "if" in job:
            yield f"jobs.{job_id}", job["if"]
        for index, step in enumerate(job.get("steps") or []):
            if "if" in step:
                yield f"jobs.{job_id}.steps[{index}]", step["if"]
    for index, step in enumerate((workflow.get("runs") or {}).get("steps") or []):
        if "if" in step:
            yield f"runs.steps[{index}]", step["if"]
