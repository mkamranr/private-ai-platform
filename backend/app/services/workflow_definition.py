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

**Sub-workflows and loops (E71) keep it that way.** A ``workflow`` step runs another
published workflow as a run of its own; a ``foreach`` step runs one once per item of a
list (``{{ item }}`` and ``{{ index }}`` in its input), one item at a time, up to
`MAX_ITEMS`. Repetition is a bounded step, not an edge back, and nesting stops at
`MAX_DEPTH`, so a workflow that calls itself fails rather than runs forever.

**Each node type has a schema** (E69, `NODE_SCHEMAS`): its config fields with their kind,
range and options, and the outputs later steps can read. Validation is driven by it
(unknown settings, wrong types, values out of range, and templates that read a step that
doesn't exist, runs later, or has no such output), and the console builds its forms from
the same schema, served by ``GET /workflows/node-types``.

**Templates read values; they cannot compute.** ``{{ input.ticket }}`` or
``{{ nodes.search.passages.0.text }}`` resolves a path in the run's context. There are no
expressions, filters or function calls: a template language that can execute code turns
every workflow editor into someone who can run code on the control plane.
"""

from __future__ import annotations

import json
import re
import uuid
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
    "workflow": "Run another published workflow and use its output.",
    "foreach": "Run a published workflow once for each item of a list.",
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
#: E71: items one foreach step may run, and the default when it doesn't say.
MAX_ITEMS: Final = 100
DEFAULT_MAX_ITEMS: Final = 20
#: E72: items of one foreach step that may run at once.
MAX_CONCURRENCY: Final = 10
#: E72: what a foreach does when an item's run fails: fail the step, or carry on.
ON_ERROR: Final = ("stop", "continue")
#: E71: how deeply sub-workflows nest. A run started by a trigger is depth 0.
MAX_DEPTH: Final = 3
#: E71: step types that start child runs. Not retried or timed out as a whole: a retry
#: would start the children again, and their own steps have their own timeouts.
CHILD_RUN_TYPES: Final = frozenset({"workflow", "foreach"})
#: What a foreach step's input can read besides the usual roots.
LOOP_ROOTS: Final = ("item", "index")

#: What a run's ``{{ run.* }}`` templates can read.
RUN_FIELDS: Final = ("id", "workflow_id", "trigger", "version")


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One setting in a node's ``config`` (E69).

    ``kind`` is one of ``text`` (one line), ``template`` (longer text), ``integer``,
    ``number``, ``boolean``, ``enum`` (one of ``options``), ``string_list``, ``object``,
    ``json`` (any value), ``model``, ``tool``, ``knowledge_bases`` and (E71)
    ``workflow``. A text-like field
    always takes templates; a typed field takes a value that is only a template when
    ``templated`` (it is then checked when the run renders it).
    """

    key: str
    kind: str
    label: str
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()
    help: str = ""
    placeholder: str = ""
    templated: bool = True
    #: The wording of the "is required" problem; the key by default.
    required_label: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "required": self.required,
            "templated": self.templated,
        }
        for name in ("minimum", "maximum", "help", "placeholder"):
            if getattr(self, name) not in (None, ""):
                out[name] = getattr(self, name)
        if self.options:
            out["options"] = list(self.options)
        return out


@dataclass(frozen=True, slots=True)
class NodeSchema:
    type: str
    label: str
    description: str
    fields: tuple[ConfigField, ...] = ()
    #: What ``{{ nodes.<id>.<output> }}`` can read. None: decided by the run (the start
    #: node's input) or by the node's own config (a transform's keys).
    outputs: tuple[str, ...] | None = ()
    branches: tuple[str, ...] = ()
    #: Takes ``retry`` and ``timeout_seconds``.
    timed: bool = False

    def field(self, key: str) -> ConfigField | None:
        return next((f for f in self.fields if f.key == key), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "description": self.description,
            "fields": [f.as_dict() for f in self.fields],
            "outputs": list(self.outputs) if self.outputs is not None else None,
            "branches": sorted(self.branches),
            "timed": self.timed,
        }


_TEMPLATE_HELP = "Refer to earlier values with {{ input.question }} or {{ nodes.search.context }}."

NODE_SCHEMAS: Final[dict[str, NodeSchema]] = {
    s.type: s
    for s in (
        NodeSchema(
            "start",
            "Start",
            NODE_TYPES["start"],
            (
                ConfigField(
                    "required_inputs",
                    "string_list",
                    "Required inputs",
                    help="Input keys a run must carry. A run without them fails at the start.",
                    templated=False,
                ),
            ),
            outputs=None,
        ),
        NodeSchema(
            "llm",
            "Model",
            NODE_TYPES["llm"],
            (
                ConfigField("model", "model", "Model or alias", required=True),
                ConfigField("system", "template", "System prompt"),
                ConfigField("prompt", "template", "Prompt", required=True, help=_TEMPLATE_HELP),
                ConfigField("temperature", "number", "Temperature", minimum=0, maximum=2),
                ConfigField("max_tokens", "integer", "Max tokens", minimum=1, maximum=1_000_000),
                ConfigField(
                    "parse_json",
                    "boolean",
                    "Parse the reply as JSON (available as .json)",
                    templated=False,
                ),
            ),
            outputs=("text", "model", "usage", "json"),
            timed=True,
        ),
        NodeSchema(
            "retrieval",
            "Retrieval",
            NODE_TYPES["retrieval"],
            (
                ConfigField(
                    "knowledge_base_ids",
                    "knowledge_bases",
                    "Knowledge bases",
                    required=True,
                    required_label="At least one knowledge base",
                ),
                ConfigField(
                    "query", "text", "Query", required=True, placeholder="{{ input.question }}"
                ),
                ConfigField(
                    "top_k", "integer", "Passages (top k)", minimum=1, maximum=50, templated=False
                ),
            ),
            outputs=("passages", "count", "context", "warnings"),
            timed=True,
        ),
        NodeSchema(
            "tool",
            "Tool",
            NODE_TYPES["tool"],
            (
                ConfigField(
                    "tool_id",
                    "tool",
                    "Tool",
                    required=True,
                    required_label="A tool",
                    templated=False,
                ),
                ConfigField(
                    "arguments",
                    "object",
                    "Arguments",
                    help="String values may be templates. HIGH and CRITICAL tools wait for "
                    "approval.",
                    templated=False,
                ),
            ),
            outputs=("content", "data", "truncated"),
            timed=True,
        ),
        NodeSchema(
            "http",
            "HTTP",
            NODE_TYPES["http"],
            (
                ConfigField(
                    "method",
                    "enum",
                    "Method",
                    options=tuple(sorted(HTTP_METHODS)),
                    templated=False,
                ),
                ConfigField(
                    "url",
                    "text",
                    "URL",
                    required=True,
                    placeholder="http://erp.internal/api/orders",
                ),
                ConfigField(
                    "headers",
                    "object",
                    "Headers",
                    help="Stored in the definition, visible to anyone who can view it. Put "
                    "credentials in a REST tool instead.",
                    templated=False,
                ),
                ConfigField(
                    "body", "json", "Body", help="JSON is sent as JSON; anything else as text."
                ),
            ),
            outputs=("status", "body", "truncated"),
            timed=True,
        ),
        NodeSchema(
            "condition",
            "Condition",
            NODE_TYPES["condition"],
            (
                ConfigField("left", "json", "Left", placeholder="{{ input.amount }}"),
                ConfigField(
                    "operator",
                    "enum",
                    "Operator",
                    required=True,
                    options=tuple(sorted(CONDITION_OPERATORS)),
                    templated=False,
                ),
                ConfigField("right", "json", "Right", help="A number, text, or a template."),
            ),
            outputs=("result", "left", "right"),
            branches=("true", "false"),
        ),
        NodeSchema(
            "transform",
            "Transform",
            NODE_TYPES["transform"],
            (
                ConfigField(
                    "output",
                    "json",
                    "Output",
                    help="Build a new object from earlier values, e.g. "
                    '{"total": "{{ nodes.calc.json.total }}"}.',
                ),
            ),
            outputs=None,
        ),
        NodeSchema(
            "approval",
            "Approval",
            NODE_TYPES["approval"],
            (
                ConfigField(
                    "message",
                    "text",
                    "Message for the approver",
                    required=True,
                    placeholder="Refund {{ input.amount }}?",
                ),
                ConfigField("details", "object", "Details shown", templated=False),
                ConfigField(
                    "allow_self_approval",
                    "boolean",
                    "Whoever started the run may approve it",
                    templated=False,
                ),
            ),
            outputs=("approved", "reason", "decided_by", "message"),
            branches=("approved", "rejected"),
        ),
        NodeSchema(
            "delay",
            "Delay",
            NODE_TYPES["delay"],
            (
                ConfigField(
                    "seconds",
                    "integer",
                    "Wait (seconds)",
                    required=True,
                    minimum=1,
                    maximum=MAX_DELAY_SECONDS,
                    help="Up to a week.",
                    templated=False,
                ),
            ),
            outputs=("waited_seconds",),
        ),
        NodeSchema(
            "workflow",
            "Sub-workflow",
            NODE_TYPES["workflow"],
            (
                ConfigField(
                    "workflow_id",
                    "workflow",
                    "Workflow",
                    required=True,
                    required_label="A workflow",
                    help="Runs its published version, as a run of its own.",
                    templated=False,
                ),
                ConfigField(
                    "input",
                    "object",
                    "Input",
                    help="The sub-workflow's run input. Values may be templates.",
                    templated=False,
                ),
            ),
            outputs=("output", "run_id"),
        ),
        NodeSchema(
            "foreach",
            "For each",
            NODE_TYPES["foreach"],
            (
                ConfigField(
                    "items",
                    "json",
                    "Items",
                    required=True,
                    placeholder="{{ nodes.search.passages }}",
                    help="A list, usually a template that reads one.",
                ),
                ConfigField(
                    "workflow_id",
                    "workflow",
                    "Workflow",
                    required=True,
                    required_label="A workflow",
                    help="Runs its published version once per item.",
                    templated=False,
                ),
                ConfigField(
                    "input",
                    "object",
                    "Input for each item",
                    help="Values may use {{ item }} and {{ index }}, e.g. "
                    '{"text": "{{ item.text }}"}.',
                    templated=False,
                ),
                ConfigField(
                    "max_items",
                    "integer",
                    "Most items",
                    minimum=1,
                    maximum=MAX_ITEMS,
                    help=f"A longer list fails the step. {DEFAULT_MAX_ITEMS} when empty.",
                    templated=False,
                ),
                ConfigField(
                    "concurrency",
                    "integer",
                    "At once",
                    minimum=1,
                    maximum=MAX_CONCURRENCY,
                    help="How many items run at the same time. 1 (one after another) when empty.",
                    templated=False,
                ),
                ConfigField(
                    "on_error",
                    "enum",
                    "When an item fails",
                    options=ON_ERROR,
                    help="stop: the step fails and the items still running are cancelled. "
                    "continue: the step succeeds, with the failures in errors.",
                    templated=False,
                ),
            ),
            outputs=("results", "count", "run_ids", "failed", "errors"),
        ),
        NodeSchema(
            "end",
            "End",
            NODE_TYPES["end"],
            (
                ConfigField(
                    "output",
                    "json",
                    "Run output",
                    help="Leave empty to return the last step's output.",
                ),
            ),
        ),
    )
}

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


def node_outputs(node: Mapping[str, Any]) -> tuple[str, ...] | None:
    """What a later step can read from ``node``; None when any key may exist."""
    schema = NODE_SCHEMAS.get(str(node.get("type")))
    if schema is None:
        return None
    if node.get("type") == "transform":
        output = (node.get("config") or {}).get("output")
        if isinstance(output, Mapping):
            return tuple(str(k) for k in output)
        return ("value",) if output is not None and not _is_whole_template(output) else None
    return schema.outputs


def _is_whole_template(value: Any) -> bool:
    return isinstance(value, str) and _WHOLE_TEMPLATE.match(value) is not None


def reads(node: Mapping[str, Any]) -> set[str]:
    """The node ids ``node``'s templates read (``{{ nodes.<id>... }}``) (E69: a step
    waits for the steps it reads, as well as for the steps leading into it)."""
    found: set[str] = set()
    for path in template_paths(node.get("config") or {}):
        parts = path.split(".")
        if parts[0] == "nodes" and len(parts) > 1:
            found.add(parts[1])
    return found


def descendants(definition: Mapping[str, Any]) -> dict[str, set[str]]:
    """Every node reachable from each node by edges (the steps that run after it)."""
    children: dict[str, set[str]] = {str(n.get("id")): set() for n in nodes_of(definition)}
    for edge in edges_of(definition):
        if str(edge.get("from")) in children:
            children[str(edge.get("from"))].add(str(edge.get("to")))
    out: dict[str, set[str]] = {}

    def walk(node_id: str) -> set[str]:
        if node_id not in out:
            out[node_id] = set()  # cycle guard; validate reports cycles first
            found: set[str] = set()
            for child in children.get(node_id, ()):
                found |= {child, *walk(child)}
            out[node_id] = found
        return out[node_id]

    for node_id in children:
        walk(node_id)
    return out


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
    problems.extend(_reference_problems(definition))
    return problems


def _reference_problems(definition: Mapping[str, Any]) -> list[Problem]:
    """E69: templates that can never resolve: an unknown root, a step that doesn't
    exist or runs later, or an output that step doesn't have."""
    by_id = node_map(definition)
    after = descendants(definition)
    out: list[Problem] = []
    # E71: {{ item }} and {{ index }} exist only in a foreach step's input.
    loop_paths = {
        str(n.get("id")): set(template_paths((n.get("config") or {}).get("input")))
        for n in nodes_of(definition)
        if n.get("type") == "foreach"
    }
    for node in nodes_of(definition):
        node_id = str(node.get("id"))
        for path in sorted(set(template_paths(node.get("config") or {}))):
            parts = path.split(".")
            root = parts[0]
            if root == "input":
                continue
            if root in LOOP_ROOTS and path in loop_paths.get(node_id, set()):
                continue
            if root == "run":
                if len(parts) < 2 or parts[1] not in RUN_FIELDS:
                    out.append(
                        Problem(f"{{{{ {path} }}}}: a run has {', '.join(RUN_FIELDS)}.", node_id)
                    )
                continue
            if root != "nodes" or len(parts) < 2:
                where = (
                    " ({{ item }} and {{ index }} only in a foreach step's input)"
                    if root in LOOP_ROOTS
                    else ""
                )
                out.append(
                    Problem(
                        f"{{{{ {path} }}}} reads nothing: templates start with input, nodes "
                        f"or run{where}.",
                        node_id,
                    )
                )
                continue
            target = parts[1]
            if target not in by_id:
                out.append(Problem(f"{{{{ {path} }}}}: there is no step {target!r}.", node_id))
                continue
            if target == node_id or target in after.get(node_id, set()):
                out.append(
                    Problem(
                        f"{{{{ {path} }}}}: {target!r} runs after this step, so it has no "
                        "output yet.",
                        node_id,
                    )
                )
                continue
            outputs = node_outputs(by_id[target])
            if len(parts) > 2 and outputs is not None and parts[2] not in (*outputs, "error"):
                have = ", ".join(outputs) if outputs else "nothing"
                out.append(
                    Problem(
                        f"{{{{ {path} }}}}: a {by_id[target].get('type')} step has no output "
                        f"{parts[2]!r} (it has {have}).",
                        node_id,
                    )
                )
    return out


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

    if kind not in NODE_TYPES:
        return [Problem(f"Unknown node type {kind!r}.", node_id)]
    if not isinstance(config, Mapping):
        return [Problem("config must be an object.", node_id)]
    schema = NODE_SCHEMAS[str(kind)]
    out.extend(_field_problems(schema, config, node_id))

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

    if kind == "http":
        url = config.get("url")
        # A templated URL is checked when it is known, at run time; a literal one now.
        if isinstance(url, str) and url and "{{" not in url:
            try:
                check_outbound_url(url, field="url")
            except ValidationError as exc:
                out.append(Problem(exc.message, node_id))
    elif kind == "condition" and "left" not in config:
        out.append(Problem("left is required.", node_id))
    return out


def _field_problems(schema: NodeSchema, config: Mapping[str, Any], node_id: str) -> list[Problem]:
    """E69: each setting against the node type's schema."""
    out: list[Problem] = []
    for key in config:
        if schema.field(str(key)) is None:
            known = ", ".join(f.key for f in schema.fields) or "none"
            out.append(
                Problem(f"A {schema.type} node has no setting {key!r} (it has {known}).", node_id)
            )
    for spec in schema.fields:
        value = config.get(spec.key)
        if value is None or (isinstance(value, str | list | dict) and not value):
            if spec.required:
                out.append(Problem(f"{spec.required_label or spec.key} is required.", node_id))
            continue
        problem = _type_problem(spec, value)
        if problem:
            out.append(Problem(problem, node_id))
    return out


def _type_problem(spec: ConfigField, value: Any) -> str | None:
    kind = spec.kind
    if kind in ("text", "template", "model", "json"):
        return None if kind == "json" or isinstance(value, str) else f"{spec.key} is text."
    if spec.templated and _is_whole_template(value):
        return None  # checked once the run renders it
    if kind in ("integer", "number"):
        numeric = int if kind == "integer" else (int, float)
        if isinstance(value, bool) or not isinstance(value, numeric):
            return f"{spec.key} is {'a whole number' if kind == 'integer' else 'a number'}."
        low, high = spec.minimum, spec.maximum
        if (low is not None and value < low) or (high is not None and value > high):
            fmt = (lambda v: f"{v:g}") if kind == "number" else (lambda v: f"{int(v)}")
            if spec.key == "seconds":
                return f"seconds is {fmt(low)} to {fmt(high)} (a week)."
            return f"{spec.key} is {fmt(low)} to {fmt(high)}."
        return None
    if kind == "boolean":
        return None if isinstance(value, bool) else f"{spec.key} is true or false."
    if kind == "enum":
        choice = value.upper() if spec.key == "method" and isinstance(value, str) else value
        if choice not in spec.options:
            return f"{spec.key} is one of {', '.join(spec.options)}."
        return None
    if kind in ("string_list", "knowledge_bases"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return f"{spec.key} is a list of text values."
        return None
    if kind == "object":
        return None if isinstance(value, Mapping) else f"{spec.key} must be an object."
    if kind == "tool":
        return None if isinstance(value, str) else f"{spec.key} is a tool id."
    if kind == "workflow":
        try:
            uuid.UUID(str(value))
        except ValueError:
            return f"{spec.key} is a workflow id."
        return None
    return None


def referenced_workflows(definition: Mapping[str, Any]) -> list[str]:
    """The workflows a definition's sub-workflow and foreach steps run (E71)."""
    return [
        ident
        for kind in sorted(CHILD_RUN_TYPES)
        for ident in referenced_ids(definition, kind, "workflow_id")
    ]


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
