"""HTML notification emails (E48): multipart/alternative, escaped, self-contained."""

from __future__ import annotations

import datetime as dt
import re

from app.config.settings import NotificationsSettings
from app.services.notification_senders import (
    EmailSender,
    Message,
    digest_message,
    html_body,
)

WHEN = dt.datetime(2026, 11, 18, 22, 5, tzinfo=dt.UTC)


def _msg(**kw: object) -> Message:
    fields: dict[str, object] = {
        "event": "raised",
        "severity": "CRITICAL",
        "title": "Backend gpu-01 is not answering",
        "message": "Connection refused",
        "source": "backend_health",
        "delivery_id": "d-1",
        "raised_at": WHEN,
        "occurrences": 3,
        "console_url": "https://ai.internal",
    }
    fields.update(kw)
    return Message(**fields)  # type: ignore[arg-type]


def _sender(**kw: object) -> EmailSender:
    return EmailSender(NotificationsSettings(smtp_host="mail.internal", **kw))  # type: ignore[arg-type]


def test_an_email_has_a_text_and_an_html_part() -> None:
    email = _sender()._build(_msg(), ["ops@site.internal"])
    assert email.get_content_type() == "multipart/alternative"
    text, page = email.get_body(("plain",)), email.get_body(("html",))
    assert text is not None and page is not None
    assert "Connection refused" in text.get_content()
    body = page.get_content()
    assert "[CRITICAL] Backend gpu-01 is not answering" in body
    assert "Connection refused" in body and "3 times" in body
    assert 'href="https://ai.internal/#alerts"' in body
    assert "#b42318" in body  # the critical colour


def test_turned_off_it_is_plain_text_only() -> None:
    email = _sender(email_html=False)._build(_msg(), ["ops@site.internal"])
    assert email.get_content_type() == "text/plain"


def test_alert_text_cannot_inject_markup() -> None:
    body = html_body(_msg(title='<script>alert("x")</script>', message="<b>bold</b> & co"))
    assert "<script>" not in body and "<b>bold</b>" not in body
    assert "&lt;script&gt;" in body and "&lt;b&gt;bold&lt;/b&gt; &amp; co" in body


def test_nothing_is_loaded_from_elsewhere() -> None:
    body = html_body(_msg())
    for forbidden in ("<img", "<script", "<link", "@import", "url(", "src="):
        assert forbidden not in body
    # The one address in it is the console's Alerts page.
    assert re.findall(r"https?://[^\"'<\s]+", body) == ["https://ai.internal/#alerts"]


def test_without_a_console_url_there_is_no_link() -> None:
    body = html_body(_msg(console_url=None))
    assert "href=" not in body and "http" not in body


def test_resolved_and_digest_messages() -> None:
    resolved = html_body(_msg(event="resolved", resolved_at=WHEN))
    assert "[RESOLVED]" in resolved and "#067647" in resolved

    items = [_msg(title=f"thing {n}", severity="WARNING") for n in range(3)]
    digest = html_body(digest_message(items, channel_name="ops", delivery_id="g", console_url=None))
    assert "[DIGEST] 3 notifications held during quiet hours for ops" in digest
    assert digest.count("<li") == 3 and "[WARNING] thing 2 (raised 2026-11-18 22:05 UTC)" in digest
