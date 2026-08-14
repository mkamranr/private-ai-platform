"""The internal tool handlers (M12).

Most of this file is about the calculator's whitelist, because that whitelist is a
security boundary rather than a convenience. The expression reaching it was composed by a
language model, and that model is steered by whatever text the user put in front of it —
so "evaluate this string" is reachable by anyone who can talk to an agent. `eval` would
make it arbitrary code execution; the tests below are what keep the parser honest.

The rest checks the module's central rule: a bad call returns an error the agent can read
and reason about, never an exception that ends the run.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.services.tool_executors import (
    _calculator,
    _date_calculator,
    _text_statistics,
    build_internal_handlers,
)


async def calc(expression: object) -> dict:
    return json.loads(await _calculator({"expression": expression}))


async def dates(**arguments: object) -> dict:
    return json.loads(await _date_calculator(arguments))


# -- the whitelist ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        "__import__('os').system('id')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "eval('1+1')",
        "exec('x=1')",
        "globals()",
        "[x for x in range(10)]",
        "lambda: 1",
        "x := 5",
        "1 if True else 2",
        "'a' * 100",
        "print(1)",
        "1; import os",
        "getattr(1, 'real')",
    ],
)
async def test_calculator_refuses_anything_that_is_not_arithmetic(attack: str) -> None:
    """Nothing but numbers and operators gets through.

    Each of these is a real shape of Python injection, and every one must come back as a
    refusal rather than a value — a whitelist that leaks one node type leaks all of them,
    because `__class__` is enough to reach everything else.
    """
    result = await calc(attack)
    assert "error" in result, f"{attack!r} was evaluated instead of refused"
    assert "result" not in result


async def test_calculator_refuses_a_bomb_rather_than_hanging() -> None:
    """`9**9**9` is eight characters and exhausts memory. Bounded, not timed out."""
    result = await calc("9**9**9")
    assert "error" in result
    assert "exponent" in result["error"].lower()


async def test_calculator_refuses_an_over_long_expression() -> None:
    result = await calc("1+" * 400 + "1")
    assert "error" in result


# -- arithmetic it must get right ------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2", 4),
        ("1250 * 0.15", 187.5),
        ("(4820 - 3915) / 3915 * 100", 23.1162),
        ("10 // 3", 3),
        ("10 % 3", 1),
        ("2 ** 10", 1024),
        ("-5 + 3", -2),
        ("sqrt(16)", 4),
        ("round(3.14159, 2)", 3.14),
        ("max(3, 7, 2)", 7),
        ("abs(-42)", 42),
    ],
)
async def test_calculator_computes(expression: str, expected: float) -> None:
    result = await calc(expression)
    assert result["result"] == pytest.approx(expected, abs=1e-4)
    # Echoed back so the trace records what was computed, not just what was claimed.
    assert result["expression"] == expression


async def test_calculator_hides_binary_float_noise() -> None:
    """0.1 + 0.2 must not come back as 0.30000000000000004.

    A model handed that quotes all seventeen digits into the answer, which reads as
    precision the calculation never had.
    """
    assert (await calc("0.1 + 0.2"))["result"] == 0.3


async def test_calculator_reports_division_by_zero_as_an_error() -> None:
    result = await calc("1 / 0")
    assert "error" in result and "zero" in result["error"].lower()


async def test_calculator_handles_an_empty_or_missing_expression() -> None:
    assert "error" in await calc("")
    assert "error" in json.loads(await _calculator({}))


# -- dates -----------------------------------------------------------------------------


async def test_date_difference_counts_across_month_boundaries() -> None:
    """The case the tool exists for: a model counting February by hand."""
    result = await dates(operation="difference", start_date="2026-01-15", end_date="2026-03-01")
    assert result["days"] == 45


async def test_date_difference_is_signed_and_says_which_way() -> None:
    """A bare -30 is read as a magnitude about as often as as a direction."""
    result = await dates(operation="difference", start_date="2026-03-01", end_date="2026-01-30")
    assert result["days"] == -30
    assert "before" in result["description"]


async def test_date_add_handles_a_leap_year() -> None:
    result = await dates(operation="add", start_date="2028-02-28", days=1)
    assert result["result_date"] == "2028-02-29"


async def test_date_add_counts_backwards() -> None:
    result = await dates(operation="add", start_date="2026-03-01", days=-1)
    assert result["result_date"] == "2026-02-28"


async def test_date_weekday() -> None:
    result = await dates(operation="weekday", start_date="2026-08-11")
    assert result["weekday"] == "Tuesday"


async def test_today_is_accepted_wherever_a_date_is() -> None:
    """So the agent can ask "how long until X" without a current_datetime round trip."""
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert (await dates(operation="weekday", start_date="today"))["date"] == today
    assert (await dates(operation="difference", start_date="today", end_date="today"))["days"] == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "difference", "start_date": "not-a-date", "end_date": "2026-01-01"},
        {"operation": "nonsense", "start_date": "2026-01-01"},
        {"operation": "add", "start_date": "2026-01-01", "days": 40000},
        {},
    ],
)
async def test_bad_date_input_returns_an_error_not_an_exception(arguments: dict) -> None:
    """This module's central rule: a failed call is an outcome the agent reasons about."""
    assert "error" in await dates(**arguments)


# -- text statistics -------------------------------------------------------------------


async def stats(**arguments: object) -> dict:
    return json.loads(await _text_statistics(arguments))


async def test_text_statistics_counts_words_characters_and_sentences() -> None:
    """The counts a length limit is actually written in terms of.

    A model asked to stay under 200 words cannot count them, so the figure has to come
    from here rather than from its own estimate of its own output.
    """
    result = await stats(text="One two three. Four five!")
    assert result["words"] == 5
    assert result["sentences"] == 2
    assert result["characters"] == len("One two three. Four five!")
    assert result["characters_no_spaces"] == len("Onetwothree.Fourfive!")


async def test_text_statistics_counts_arabic() -> None:
    """Arabic is not a special case to be handled later.

    Half this platform's correspondence is Arabic, and a word counter that only splits
    ASCII would silently report 0 for it — a length check that always passes.
    """
    result = await stats(text="هذه جملة قصيرة. وهذه جملة أخرى.")
    assert result["words"] == 6
    assert result["sentences"] == 2


async def test_text_statistics_ignores_repeated_whitespace() -> None:
    result = await stats(text="  one\n\ntwo\t\tthree  ")
    assert result["words"] == 3


async def test_text_statistics_does_not_split_decimals_into_sentences() -> None:
    """A full stop inside a number does not end a sentence.

    Counting "3.5" as two sentences makes the figure wrong in exactly the documents that
    contain figures, which is most of them.
    """
    result = await stats(text="Growth was 3.5 percent. That is all.")
    assert result["sentences"] == 2


async def test_text_statistics_handles_empty_and_missing_text() -> None:
    """Zero, not an exception. An empty draft is a normal thing to measure."""
    for result in (await stats(text=""), await stats(), await stats(text=None)):
        assert result["words"] == 0
        assert result["sentences"] == 0
        assert result["characters"] == 0


async def test_text_statistics_refuses_text_it_would_choke_on() -> None:
    """Bounded like the calculator, and for the same reason: the input is model-supplied."""
    result = await stats(text="word " * 200_000)
    assert "error" in result


# -- the registry ----------------------------------------------------------------------


def test_pure_handlers_need_no_database() -> None:
    """The calculator and the date tool must work before a session factory exists.

    They are pure functions; requiring a database to add two numbers would make them
    unavailable in exactly the contexts where nothing else is available either.
    """
    handlers = build_internal_handlers(None)
    assert set(handlers) == {
        "current_datetime",
        "calculator",
        "date_calculator",
        "text_statistics",
    }


def test_database_backed_handlers_appear_only_with_a_session_factory() -> None:
    handlers = build_internal_handlers(object())
    assert "model_catalog" in handlers
    assert "platform_status" in handlers
