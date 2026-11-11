"""Workflow definitions: the node catalogue, validation, templates and graph order (E6).

Pure functions, no I/O, so the rules a definition must satisfy are testable on their own
and the builder can validate a draft without running it.

A definition is ``{"nodes": [...], "edges": [...]}``:

* a node is ``{"id", "type", "name", "config", "retry", "timeout_seconds", "position"}``;
* an edge is ``{"from", "to", "when"}``. ``when`` selects the branch it follows: ``true``
  or ``false`` after a condition, ``approved`` or ``rejected`` after an approval,
  ``error`` after any node that failed, or nothing for "on success".

**Graphs are acyclic.** A loop in a workflow that can call tools and models is a runaway
waiting to happen, and every step here is a persisted row; a DAG also gives the engine a
single, deterministic execution order (see `execution_order`).

**Templates read values; they cannot compute.** ``{{ input.ticket }}`` or
``{{ nodes.search.passages.0.text }}`` resolves a path in the run's context. There are no
expressions, filters or function calls: a template language that can execute code turns
every workflow editor into someone who can run code on the control plane.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.core.errors import ValidationError
from app.core.url_policy import check_outbound_url

#: Node types and what each needs in ``config``.
NODE_TYPES: Final[dict[str, str]] = {
    "start": "Where every run begins. Receives the run input.",
    "llm": "Send a prompt to a model through the gateway.",
    "retrieval": "Search knowledge bases and return passages.",
    "tool": "Call a registered tool (REST, MCP, internal).",
    "http": "Send an HTTP request to an internal service.",
    "condition": "Compare two values and branch on true or false.",
    "transform": "Build a new value from earlier outputs.",
    "approval": "Pause until someone approves or rejects.",
    "delay": "Pause for a number of seconds.",
    "end": "Finish the run and set its output.",
}

#: The branches a node can produce, and so the ``when`` values its edges may use.
#: ``error`` is allowed after any node except start and end.
BRANCHES: Final[dict[str, frozenset[str]]] = {
    "condition": frozenset({"true", "false"}),
    "approval": frozenset({"approved", "rejected"}),
}

CONDITION_OPERATORS: Final = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "empty", "not_empty"}
)
HTTP_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

MAX_NODES: Final = 50
MAX_ATTEMPTS: Final = 5
MAX_BACKOFF_SECONDS: Final = 60
MAX_TIMEOUT_SECONDS: Final = 600
DEFAULT_TIMEOUT_SECONDS: Final = 60
#: A week. A longer wait is a schedule, not a delay.
MAX_DELAY_SECONDS: Final = 7 * 24 * 3600

_NODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_WHOLE_TEMPLATE = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}\s*$")


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing wrong with a definition. ``node`` is None for graph-level problems."""

    message: str
    node: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"node": self.node, "message": self.message}


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------
def nodes_of(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = definition.get("nodes") or []
    return [n for n in nodes if isinstance(n, dict)]


def edges_of(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    edges = definition.get("edges") or []
    return [e for e in edges if isinstance(e, dict)]


def node_map(definition: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in nodes_of(definition)}


def incoming(definition: Mapping[str, Any], node_id: str) -> list[dict[str, Any]]:
    return [e for e in edges_of(definition) if e.get("to") == node_id]


def outgoing(definition: Mapping[str, Any], node_id: str) -> list[dict[str, Any]]:
    return [e for e in edges_of(definition) if e.get("from") == node_id]


def retry_policy(node: Mapping[str, Any]) -> tuple[int, float]:
    retry = node.get("retry") or {}
    attempts = int(retry.get("max_attempts") or 1)
    backoff = float(retry.get("backoff_seconds") or 0)
    return max(1, min(attempts, MAX_ATTEMPTS)), max(0.0, min(backoff, MAX_BACKOFF_SECONDS))


def timeout_of(node: Mapping[str, Any]) -> float:
    value = node.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    return float(max(1, min(int(value), MAX_TIMEOUT_SECONDS)))


# ---------------------------------------------------------------------------
# Graph order
# ---------------------------------------------------------------------------
def execution_order(definition: Mapping[str, Any]) -> list[str]:
    """Topological order, ties broken by position in the definition.

    Deterministic: the same definition always executes its nodes in the same order, so
    two runs of one version produce comparable traces. Raises on a cycle.
    """
    ids = [str(n.get("id")) for n in nodes_of(definition)]
    position = {node_id: index for index, node_id in enumerate(ids)}
    indegree = dict.fromkeys(ids, 0)
    children: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges_of(definition):
        source, target = str(edge.get("from")), str(edge.get("to"))
        if source in indegree and target in indegree:
            children[source].append(target)
            indegree[target] += 1

    ready = sorted((n for n, d in indegree.items() if d == 0), key=position.__getitem__)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=position.__getitem__)
    if len(order) != len(ids):
        raise ValidationError("The workflow graph has a cycle.")
    return order


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(definition: Any) -> list[Problem]:
    """Everything wrong with a definition. An empty list means it can run."""
    if not isinstance(definition, Mapping):
        return [Problem("A definition is an object with nodes and edges.")]
    problems: list[Problem] = []
    nodes = nodes_of(definition)
    edges = edges_of(definition)

    if not nodes:
        return [Problem("Add at least a start node.")]
    if len(nodes) > MAX_NODES:
        problems.append(Problem(f"At most {MAX_NODES} nodes per workflow."))

    seen: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not _NODE_ID.match(node_id):
            problems.append(
                Problem(
                    "Node ids are lowercase letters, digits and underscores, starting "
                    "with a letter.",
                    node_id or None,
                )
            )
            continue
        if node_id in seen:
            problems.append(Problem("Duplicate node id.", node_id))
        seen.add(node_id)
        problems.extend(_validate_node(node))

    starts = [n for n in nodes if n.get("type") == "start"]
    if len(starts) != 1:
        problems.append(Problem("A workflow has exactly one start node."))

    by_id = node_map(definition)
    for edge in edges:
        source, target, when = edge.get("from"), edge.get("to"), edge.get("when")
        if source not in by_id or target not in by_id:
            problems.append(Problem(f"Edge {source} → {target} names a node that does not exist."))
            continue
        if source == target:
            problems.append(Problem("A node cannot connect to itself.", str(source)))
            continue
        source_type = by_id[str(source)].get("type")
        if by_id[str(target)].get("type") == "start":
            problems.append(Problem("Nothing may lead into the start node.", str(target)))
        if source_type == "end":
            problems.append(Problem("An end node has no outgoing edges.", str(source)))
        allowed = BRANCHES.get(str(source_type), frozenset())
        if when is None:
            if allowed:
                problems.append(
                    Problem(
                        f"Edges from a {source_type} node must say which branch they follow "
                        f"({', '.join(sorted(allowed))}).",
                        str(source),
                    )
                )
        elif when == "error":
            if source_type == "start":
                problems.append(Problem("The start node cannot fail.", str(source)))
        elif when not in allowed:
            problems.append(
                Problem(f"{when!r} is not a branch of a {source_type} node.", str(source))
            )

    if problems:
        return problems

    try:
        order = execution_order(definition)
    except ValidationError:
        return [Problem("The graph has a cycle. Workflows must not loop.")]

    reachable = {str(starts[0]["id"])}
    for node_id in order:
        if node_id in reachable:
            reachable.update(str(e["to"]) for e in outgoing(definition, node_id))
    for node_id in order:
        if node_id not in reachable:
            problems.append(Problem("Not reachable from the start node.", node_id))
    return problems


def require_valid(definition: Any) -> None:
    problems = validate(definition)
    if problems:
        raise ValidationError(
            f"The workflow is not valid: {problems[0].message}"
            + (f" (node {problems[0].node})" if problems[0].node else ""),
            details={"problems": [p.as_dict() for p in problems]},
        )


def _validate_node(node: Mapping[str, Any]) -> list[Problem]:
    node_id = str(node.get("id"))
    kind = node.get("type")
    config = node.get("config") or {}
    out: list[Problem] = []

    def need(key: str, label: str | None = None) -> None:
        value = config.get(key)
        if value is None or (isinstance(value, str | list | dict) and not value):
            out.append(Problem(f"{label or key} is required.", node_id))

    if kind not in NODE_TYPES:
        return [Problem(f"Unknown node type {kind!r}.", node_id)]
    if not isinstance(config, Mapping):
        return [Problem("config must be an object.", node_id)]

    retry = node.get("retry") or {}
    if not isinstance(retry, Mapping):
        out.append(Problem("retry must be an object.", node_id))
    else:
        attempts = retry.get("max_attempts", 1)
        if not isinstance(attempts, int) or not 1 <= attempts <= MAX_ATTEMPTS:
            out.append(Problem(f"max_attempts is 1 to {MAX_ATTEMPTS}.", node_id))
        backoff = retry.get("backoff_seconds", 0)
        if not isinstance(backoff, int | float) or not 0 <= backoff <= MAX_BACKOFF_SECONDS:
            out.append(Problem(f"backoff_seconds is 0 to {MAX_BACKOFF_SECONDS}.", node_id))
    timeout = node.get("timeout_seconds")
    if timeout is not None and (
        not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS
    ):
        out.append(Problem(f"timeout_seconds is 1 to {MAX_TIMEOUT_SECONDS}.", node_id))

    if kind == "llm":
        need("model")
        need("prompt")
    elif kind == "retrieval":
        need("knowledge_base_ids", "At least one knowledge base")
        need("query")
        top_k = config.get("top_k")
        if top_k is not None and (not isinstance(top_k, int) or not 1 <= top_k <= 50):
            out.append(Problem("top_k is 1 to 50.", node_id))
    elif kind == "tool":
        need("tool_id", "A tool")
        if not isinstance(config.get("arguments", {}), Mapping):
            out.append(Problem("arguments must be an object.", node_id))
    elif kind == "http":
        need("url")
        method = str(config.get("method") or "GET").upper()
        if method not in HTTP_METHODS:
            out.append(Problem(f"method is one of {', '.join(sorted(HTTP_METHODS))}.", node_id))
        url = config.get("url")
        # A templated URL is checked when it is known, at run time; a literal one now.
        if isinstance(url, str) and url and "{{" not in url:
            try:
                check_outbound_url(url, field="url")
            except ValidationError as exc:
                out.append(Problem(exc.message, node_id))
        if not isinstance(config.get("headers", {}), Mapping):
            out.append(Problem("headers must be an object.", node_id))
    elif kind == "condition":
        operator = config.get("operator")
        if operator not in CONDITION_OPERATORS:
            out.append(
                Problem(f"operator is one of {', '.join(sorted(CONDITION_OPERATORS))}.", node_id)
            )
        if "left" not in config:
            out.append(Problem("left is required.", node_id))
    elif kind == "approval":
        need("message")
    elif kind == "delay":
        seconds = config.get("seconds")
        if not isinstance(seconds, int) or not 1 <= seconds <= MAX_DELAY_SECONDS:
            out.append(Problem(f"seconds is 1 to {MAX_DELAY_SECONDS} (a week).", node_id))
    return out


def referenced_ids(definition: Mapping[str, Any], kind: str, key: str) -> list[str]:
    """Every value of ``config[key]`` across nodes of ``kind`` (flattening lists): the
    knowledge bases, tools or models a definition refers to, for scope checks."""
    found: list[str] = []
    for node in nodes_of(definition):
        if node.get("type") != kind:
            continue
        value = (node.get("config") or {}).get(key)
        if isinstance(value, list):
            found.extend(str(v) for v in value)
        elif value:
            found.append(str(value))
    return found


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def lookup(context: Mapping[str, Any], path: str) -> Any:
    """Resolve ``a.b.0.c`` in nested dicts and lists. Missing anywhere: None."""
    current: Any = context
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def render(value: Any, context: Mapping[str, Any]) -> Any:
    """Fill templates in strings, recursively through dicts and lists.

    A string that is exactly one template keeps the value's type, so
    ``"{{ nodes.search.passages }}"`` passes the list itself, not its JSON text. A
    template inside other text is replaced by the value's text.
    """
    if isinstance(value, str):
        whole = _WHOLE_TEMPLATE.match(value)
        if whole:
            return lookup(context, whole.group(1))
        return _TEMPLATE.sub(lambda m: _as_text(lookup(context, m.group(1))), value)
    if isinstance(value, Mapping):
        return {str(k): render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value


def template_paths(value: Any) -> Iterable[str]:
    """Every path a value's templates read, for the builder's reference check."""
    if isinstance(value, str):
        yield from (m.group(1) for m in _TEMPLATE.finditer(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from template_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from template_paths(item)


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------
def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and len(value) == 0)


def evaluate(operator: str, left: Any, right: Any = None) -> bool:
    """Compare two already-rendered values. Numbers compare as numbers when both sides
    parse as one; otherwise as text."""
    if operator == "empty":
        return _is_empty(left)
    if operator == "not_empty":
        return not _is_empty(left)
    if operator in ("contains", "not_contains"):
        if isinstance(left, list):
            found = right in left or _as_text(right) in [_as_text(v) for v in left]
        elif isinstance(left, Mapping):
            found = _as_text(right) in left
        else:
            found = _as_text(right).lower() in _as_text(left).lower()
        return found if operator == "contains" else not found
    a, b = _number(left), _number(right)
    if operator in ("eq", "ne"):
        same = a == b if a is not None and b is not None else _as_text(left) == _as_text(right)
        return same if operator == "eq" else not same
    if a is None or b is None:
        # Ordering text against a number is meaningless; say so by being false, not by
        # guessing an order.
        if operator in ("gt", "gte", "lt", "lte") and not (a is None and b is None):
            return False
        x, y = _as_text(left), _as_text(right)
        return {"gt": x > y, "gte": x >= y, "lt": x < y, "lte": x <= y}[operator]
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[operator]


# ---------------------------------------------------------------------------
# Secrets in step traces
# ---------------------------------------------------------------------------
_SECRET_NAME = re.compile(r"auth|token|secret|password|api[-_]?key|cookie|credential", re.I)


def redact_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Header values that look like credentials, masked for the step trace."""
    return {k: ("***" if _SECRET_NAME.search(str(k)) else v) for k, v in headers.items()}
