"""CSV cells a spreadsheet will not run as formulas.

Shared by every CSV export (chargeback, audit). Values such as end-user names, audit
messages and user agents come from request bodies and headers, so ``=HYPERLINK(...)`` is
a value anyone with an API key or a browser can plant in a report someone later opens
in a spreadsheet.
"""

from __future__ import annotations

from typing import Any

#: A leading character that makes a spreadsheet treat the cell as a formula.
_FORMULA_START = "=+-@\t\r"


def safe_cell(value: Any) -> str:
    """``value`` as text, with a leading apostrophe if it would start a formula.

    ``None`` becomes an empty cell, never the text ``None``.
    """
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in _FORMULA_START:
        return "'" + text
    return text
