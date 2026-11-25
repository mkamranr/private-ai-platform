"""Typed node schemas (E69): validation driven by each node type's declared settings and
outputs, including templates that can never resolve."""

from __future__ import annotations

import itertools
from typing import Any, ClassVar

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


class TestTypedPaths:
    """E84: a template's path is checked through the output's type, not only its name."""

    SEARCH = node(
        "search",
        "retrieval",
        knowledge_base_ids=["5f0c6d0e-0000-4000-8000-000000000001"],
        query="q",
    )

    def test_deeper_paths(self) -> None:
        def found(prompt: str) -> set:
            return problems(START, self.SEARCH, node("ask", "llm", model="m", prompt=prompt), END)

        assert found("{{ nodes.search.passages.0.text }} {{ nodes.search.count }}") == set()
        assert found("{{ nodes.search.passages.0.document }}") == set()
        assert any(
            "'first' is not an index" in m for _, m in found("{{ nodes.search.passages.first }}")
        )
        assert any("no field 'title'" in m for _, m in found("{{ nodes.search.passages.0.title }}"))
        assert any(
            "'context' is text, which has no 'size'" in m
            for _, m in found("{{ nodes.search.context.size }}")
        )

    def test_model_outputs_and_any(self) -> None:
        later = node(
            "use",
            "transform",
            output={
                "t": "{{ nodes.ask.usage.total_tokens }}",
                "j": "{{ nodes.ask.json.anything.deep.0 }}",
                "e": "{{ nodes.ask.error }}",
            },
        )
        assert problems(START, node("ask", "llm", model="m", prompt="hi"), later, END) == set()
        bad = node("use", "transform", output={"t": "{{ nodes.ask.usage.cost }}"})
        found = problems(START, node("ask", "llm", model="m", prompt="hi"), bad, END)
        assert any("no field 'cost'" in m for _, m in found)

    def test_catalogue_names_the_types(self) -> None:
        retrieval = wd.NODE_SCHEMAS["retrieval"].as_dict()
        assert retrieval["output_types"]["passages"] == "list of object"
        assert retrieval["output_types"]["count"] == "number"
        assert wd.NODE_SCHEMAS["transform"].as_dict()["output_types"] == {}


def looping(*, when: str = "false", max_loops: Any = 3, to: str = "draft") -> dict:
    return {
        "nodes": [
            START,
            node("draft", "transform", output={"text": "x"}),
            node("check", "condition", left="{{ nodes.draft.text }}", operator="eq", right="y"),
            END,
        ],
        "edges": [
            {"from": "start", "to": "draft"},
            {"from": "draft", "to": "check"},
            {"from": "check", "to": "end", "when": "true"},
            {"from": "check", "to": to, "when": when, "back": True, "max_loops": max_loops},
        ],
    }


class TestBackEdges:
    """E84: an edge back repeats earlier steps a bounded number of times."""

    def test_a_loop_back_is_valid(self) -> None:
        assert wd.validate(looping()) == []
        assert wd.loop_region(looping(), looping()["edges"][-1]) == ["draft", "check"]

    def test_a_plain_cycle_says_how_to_loop(self) -> None:
        definition = looping()
        del definition["edges"][-1]["back"]
        (problem,) = wd.validate(definition)
        assert "edge back" in problem.message

    def test_it_must_go_back(self) -> None:
        definition = looping()
        definition["edges"][-1] = {"from": "draft", "to": "check", "back": True}
        assert any("runs before this one" in p.message for p in wd.validate(definition))

    def test_branch_and_bounds(self) -> None:
        assert any("which branch" in p.message for p in wd.validate(looping(when=None)))
        assert any("max_loops is 1 to" in p.message for p in wd.validate(looping(max_loops=0)))
        assert any("max_loops is 1 to" in p.message for p in wd.validate(looping(max_loops=99)))
        clash = looping(when="true")
        assert any("give the edge back its own branch" in p.message for p in wd.validate(clash))

    def test_previous_is_checked(self) -> None:
        definition = looping()
        definition["nodes"][1] = node(
            "draft", "transform", output={"text": "{{ previous.draft.text }}"}
        )
        assert wd.validate(definition) == []
        definition["nodes"][1] = node("draft", "transform", output={"t": "{{ previous.nope.x }}"})
        assert any("previous reads" in p.message for p in wd.validate(definition))


def field(node_type: str, key: str) -> wd.ConfigField:
    return next(f for f in wd.NODE_SCHEMAS[node_type].fields if f.key == key)


class TestJsonSchema:
    """E89: a model step's json_schema types its .json, so paths under it are checked."""

    SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "total": {"type": "number"},
            "lines": {
                "type": "array",
                "items": {"type": "object", "properties": {"sku": {"type": "string"}}},
            },
            "note": {"type": ["string", "null"]},
        },
    }

    def test_schema_type(self) -> None:
        assert wd.schema_type(self.SCHEMA) == {
            "total": "number",
            "lines": [{"sku": "text"}],
            "note": "text",
        }
        assert wd.schema_type({"type": "array"}) == ["any"]
        assert wd.schema_type("nonsense") == "any"
        assert wd.schema_type({"type": "object"}) == "any"  # no properties: unknown fields

    def test_paths_under_json(self) -> None:
        calc = node("calc", "llm", model="m", prompt="p", parse_json=True, json_schema=self.SCHEMA)

        def found(output: dict) -> set:
            return problems(START, calc, node("use", "transform", output=output), END)

        assert (
            found({"a": "{{ nodes.calc.json.total }}", "b": "{{ nodes.calc.json.lines.0.sku }}"})
            == set()
        )
        assert any("no field 'totl'" in m for _, m in found({"a": "{{ nodes.calc.json.totl }}"}))
        assert any(
            "no field 'price'" in m for _, m in found({"a": "{{ nodes.calc.json.lines.0.price }}"})
        )
        # Without a schema the json stays any, and every path is allowed.
        plain = node("calc", "llm", model="m", prompt="p", parse_json=True)
        assert (
            problems(
                START,
                plain,
                node("use", "transform", output={"a": "{{ nodes.calc.json.x.y }}"}),
                END,
            )
            == set()
        )


class TestTypedSettings:
    """E89: a typed setting filled by a whole template is checked against what it reads."""

    SEARCH = node(
        "search",
        "retrieval",
        knowledge_base_ids=["5f0c6d0e-0000-4000-8000-000000000001"],
        query="q",
    )

    def found(self, **config: Any) -> set:
        ask = node("ask", "llm", model="m", prompt="p", **config)
        return {m for n, m in problems(START, self.SEARCH, ask, END) if n == "ask"}

    def test_fits_and_misfits(self) -> None:
        assert self.found(temperature="{{ nodes.search.count }}") == set()
        assert self.found(temperature="{{ input.t }}") == set()  # only the run knows
        assert self.found(max_tokens="{{ run.version }}") == set()
        (message,) = self.found(temperature="{{ nodes.search.passages }}")
        assert "temperature takes a number, but {{ nodes.search.passages }}" in message
        (message,) = self.found(parse_json="{{ nodes.search.passages.0 }}")
        assert "parse_json is true or false" in message  # not templatable: the plain check

    def test_list_settings(self) -> None:
        spec = field("retrieval", "knowledge_base_ids")
        by_id = {"search": self.SEARCH}
        assert wd.setting_fit_problem(spec, "nodes.search.count", by_id) is not None
        assert "list of text values" in (
            wd.setting_fit_problem(spec, "nodes.search.passages", by_id) or ""
        )

    def test_coerce(self) -> None:
        temperature, tokens = field("llm", "temperature"), field("llm", "max_tokens")
        assert wd.coerce_setting(temperature, "0.7") == 0.7
        assert wd.coerce_setting(tokens, "300") == 300 and wd.coerce_setting(tokens, 300.0) == 300
        assert wd.coerce_setting(field("llm", "parse_json"), "TRUE") is True
        assert wd.coerce_setting(field("retrieval", "knowledge_base_ids"), "kb") == ["kb"]
        assert wd.coerce_setting(temperature, None) is None
        for spec, value, words in (
            (temperature, "hot", "temperature is a number, not 'hot'"),
            (temperature, True, "is a number"),
            (temperature, "3", "temperature is 0 to 2, not 3"),
            (tokens, "2.5", "max_tokens is a whole number"),
            (field("llm", "parse_json"), "maybe", "true or false"),
        ):
            try:
                wd.coerce_setting(spec, value)
            except wd.SettingError as exc:
                assert words in str(exc), (value, str(exc))
            else:
                raise AssertionError(f"{value!r} was accepted")

    def test_render_config(self) -> None:
        config = {"model": "m", "prompt": "{{ input.q }}", "temperature": "{{ input.t }}"}
        rendered = wd.render_config("llm", config, {"input": {"q": "hi", "t": "0.2"}})
        assert rendered["temperature"] == 0.2 and rendered["prompt"] == "hi"
        try:
            wd.render_config("llm", config, {"input": {"q": "hi", "t": "warm"}})
        except wd.SettingError as exc:
            assert "temperature" in str(exc)
        else:
            raise AssertionError("a mis-typed temperature was accepted")
        # A plain value or a template inside text isn't converted here.
        assert wd.render_config("llm", {"temperature": 0.5}, {})["temperature"] == 0.5
