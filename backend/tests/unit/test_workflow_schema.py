"""Typed node schemas (E69): validation driven by each node type's declared settings and
outputs, including templates that can never resolve."""

from __future__ import annotations

import itertools
from typing import Any

from app.services import workflow_definition as wd
from app.services.workflows import template_definition


def node(node_id: str, kind: str, **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": kind, "config": config}


def problems(*nodes: dict[str, Any], edges: list[tuple[str, str]] | None = None) -> set:
    ids = [n["id"] for n in nodes]
    pairs = edges if edges is not None else list(itertools.pairwise(ids))
    definition = {"nodes": list(nodes), "edges": [{"from": a, "to": b} for a, b in pairs]}
    return {(p.node, p.message) for p in wd.validate(definition)}


START = node("start", "start")
END = node("end", "end")


class TestSettings:
    def test_unknown_settings_are_named(self) -> None:
        found = problems(START, node("ask", "llm", model="m", prompt="hi", temprature=0.2), END)
        ((_, message),) = [p for p in found if p[0] == "ask"]
        assert "no setting 'temprature'" in message and "temperature" in message

    def test_types_and_ranges(self) -> None:
        found = problems(
            START,
            node("ask", "llm", model="m", prompt="hi", temperature="hot", max_tokens=0),
            node("call", "http", url="http://erp.internal/x", method="FETCH", headers="x"),
            node("search", "retrieval", knowledge_base_ids="kb", query="q", top_k=99),
            node("gate", "approval", message="ok?", allow_self_approval="yes"),
            END,
        )
        assert ("ask", "temperature is a number.") in found
        assert ("ask", "max_tokens is 1 to 1000000.") in found
        assert ("call", "method is one of DELETE, GET, PATCH, POST, PUT.") in found
        assert ("call", "headers must be an object.") in found
        assert ("search", "knowledge_base_ids is a list of text values.") in found
        assert ("search", "top_k is 1 to 50.") in found
        assert ("gate", "allow_self_approval is true or false.") in found

    def test_a_template_stands_in_where_the_schema_allows_it(self) -> None:
        found = problems(
            START,
            node("ask", "llm", model="{{ input.model }}", prompt="hi", temperature="{{ input.t }}"),
            node(
                "search", "retrieval", knowledge_base_ids=["kb"], query="q", top_k="{{ input.k }}"
            ),
            END,
        )
        # temperature may be templated; top_k may not (it was never allowed).
        assert not any(n == "ask" for n, _ in found)
        assert ("search", "top_k is a whole number.") in found

    def test_lowercase_method_is_accepted(self) -> None:
        assert (
            problems(START, node("call", "http", url="http://erp.internal/x", method="post"), END)
            == set()
        )

    def test_the_templates_still_validate_as_before(self) -> None:
        assert wd.validate(template_definition("blank")) == []
        positions = [n["position"] for n in template_definition("approval_http")["nodes"]]
        # E69: positions are canvas pixels, laid out on the auto-arrange grid.
        assert {"x": 16, "y": 16} in positions and all(p["x"] >= 16 for p in positions)
        assert len({(p["x"], p["y"]) for p in positions}) == len(positions)


class TestReferences:
    def test_an_unknown_step_or_root(self) -> None:
        found = problems(
            START,
            node("ask", "llm", model="m", prompt="{{ nodes.serch.context }} {{ inputs.q }}"),
            END,
        )
        assert ("ask", "{{ nodes.serch.context }}: there is no step 'serch'.") in found
        assert any(n == "ask" and "{{ inputs.q }} reads nothing" in m for n, m in found)

    def test_a_step_that_runs_later(self) -> None:
        found = problems(
            START,
            node("first", "transform", output={"x": "{{ nodes.second.y }}"}),
            node("second", "transform", output={"y": 1}),
            END,
        )
        assert (
            "first",
            "{{ nodes.second.y }}: 'second' runs after this step, so it has no output yet.",
        ) in found

    def test_an_output_the_step_does_not_have(self) -> None:
        found = problems(
            START,
            node("search", "retrieval", knowledge_base_ids=["kb"], query="q"),
            node("calc", "transform", output={"total": 3}),
            node(
                "ask",
                "llm",
                model="m",
                prompt="{{ nodes.search.contxt }} {{ nodes.calc.total }} {{ nodes.calc.sum }}",
            ),
            END,
        )
        messages = {m for n, m in found if n == "ask"}
        assert (
            "{{ nodes.search.contxt }}: a retrieval step has no output 'contxt' "
            "(it has passages, count, context, warnings)."
        ) in messages
        assert any("nodes.calc.sum" in m and "(it has total)" in m for m in messages)
        assert not any("nodes.calc.total" in m for m in messages)

    def test_what_is_always_readable(self) -> None:
        found = problems(
            START,
            node(
                "search", "retrieval", knowledge_base_ids=["kb"], query="{{ input.anything.deep }}"
            ),
            node(
                "ask",
                "llm",
                model="m",
                prompt="{{ nodes.start.whatever }} {{ nodes.search.error }} {{ run.id }} "
                "{{ nodes.search.passages.0.text }}",
            ),
            node("end", "end", output="{{ input }}"),
        )
        assert found == set()

    def test_run_fields(self) -> None:
        found = problems(START, node("t", "transform", output={"v": "{{ run.owner }}"}), END)
        assert any(
            n == "t" and "a run has id, workflow_id, trigger, version" in m for n, m in found
        )


class TestNodeTypes:
    def test_every_type_has_a_schema(self) -> None:
        assert set(wd.NODE_SCHEMAS) == set(wd.NODE_TYPES)
        llm = wd.NODE_SCHEMAS["llm"].as_dict()
        assert llm["timed"] and llm["outputs"] == ["text", "model", "usage", "json"]
        temperature = next(f for f in llm["fields"] if f["key"] == "temperature")
        assert temperature == {
            "key": "temperature",
            "kind": "number",
            "label": "Temperature",
            "required": False,
            "templated": True,
            "minimum": 0,
            "maximum": 2,
        }
        assert wd.NODE_SCHEMAS["condition"].as_dict()["branches"] == ["false", "true"]
        assert wd.NODE_SCHEMAS["start"].as_dict()["outputs"] is None
