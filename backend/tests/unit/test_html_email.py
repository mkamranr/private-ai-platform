"""HTML notification emails (E48) in the branded layout (E49): multipart/alternative,
escaped, self-contained, with the brand from the settings."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from app.config.settings import NotificationsSettings
from app.services.email_templates import Brand
from app.services.notification_senders import (
    LOGO_MAX_BYTES,
    EmailSender,
    Message,
    brand_from,
    digest_message,
    html_body,
    load_logo,
)

WHEN = dt.datetime(2026, 11, 18, 22, 5, tzinfo=dt.UTC)
#: The smallest valid PNG: a 1x1 transparent pixel.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


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


def _settings(**kw: object) -> NotificationsSettings:
    return NotificationsSettings(smtp_host="mail.internal", **kw)  # type: ignore[arg-type]


def _build(msg: Message | None = None, **kw: object):
    return EmailSender(_settings(**kw))._build(msg or _msg(), ["ops@site.internal"])


class TestParts:
    def test_an_email_has_a_text_and_an_html_part(self) -> None:
        email = _build()
        assert email.get_content_type() == "multipart/alternative"
        text, page = email.get_body(("plain",)), email.get_body(("html",))
        assert text is not None and page is not None
        assert "Connection refused" in text.get_content()
        body = page.get_content()
        assert "Backend gpu-01 is not answering" in body
        assert "Connection refused" in body and "3 times" in body
        assert 'href="https://ai.internal/#alerts"' in body
        assert "#b42318" in body  # the critical colour

    def test_turned_off_it_is_plain_text_only(self) -> None:
        assert _build(email_html=False).get_content_type() == "text/plain"

    def test_the_subject_prefix_can_change_or_go(self) -> None:
        assert _build()["Subject"] == "[AI Platform] [CRITICAL] Backend gpu-01 is not answering"
        assert _build(email_subject_prefix="[Acme AI]")["Subject"].startswith("[Acme AI] [CRIT")
        assert _build(email_subject_prefix="")["Subject"].startswith("[CRITICAL]")


class TestSafety:
    def test_alert_text_cannot_inject_markup(self) -> None:
        body = html_body(_msg(title='<script>alert("x")</script>', message="<b>bold</b> & co"))
        assert "<script>" not in body and "<b>bold</b>" not in body
        assert "&lt;script&gt;" in body and "&lt;b&gt;bold&lt;/b&gt; &amp; co" in body

    def test_brand_text_cannot_inject_markup_either(self) -> None:
        brand = Brand(name="<i>Acme</i>", tagline="<script>", footer='"><img src=x>')
        body = html_body(_msg(), brand)
        assert "<i>Acme</i>" not in body and "<script>" not in body and "<img" not in body

    def test_nothing_is_loaded_from_elsewhere(self) -> None:
        body = html_body(_msg())
        for forbidden in ("<img", "<script", "<link", "@import", "url(", "src="):
            assert forbidden not in body
        # The one address in it is the console's Alerts page.
        assert re.findall(r"https?://[^\"'<\s]+", body) == ["https://ai.internal/#alerts"]

    def test_without_a_console_url_there_is_no_link(self) -> None:
        body = html_body(_msg(console_url=None))
        assert "href=" not in body and "http" not in body


class TestKinds:
    def test_each_kind_says_what_happened(self) -> None:
        assert "A critical alert was raised." in html_body(_msg())
        assert "An open alert has escalated to critical." in html_body(_msg(event="escalated"))
        resolved = html_body(_msg(event="resolved", resolved_at=WHEN))
        assert "This alert has been resolved." in resolved and "#067647" in resolved
        test = html_body(_msg(event="test", severity="INFO"), Brand(name="Acme AI"))
        assert "This is a test from Acme AI." in test

    def test_an_approval_reminder_links_to_the_approvals_page(self) -> None:
        msg = _msg(kind="approval", severity="WARNING", title="Approval for create_ticket")
        body = html_body(msg)
        assert msg.link == "https://ai.internal/#approvals"
        assert 'href="https://ai.internal/#approvals"' in body
        assert "Open the Approvals page" in body and "You can decide this approval." in body
        text = _build(msg).get_body(("plain",))
        assert text is not None and "Turn these emails off under your account" in text.get_content()

    def test_a_digest_lists_its_items_with_a_summary(self) -> None:
        items = [_msg(title=f"thing {n}", severity="WARNING") for n in range(3)]
        items.append(_msg(title="fixed", event="resolved", resolved_at=WHEN))
        digest = html_body(
            digest_message(items, channel_name="ops", delivery_id="g", console_url=None)
        )
        assert "4 notifications held during quiet hours for ops" in digest
        assert "3 warning" in digest and "1 resolved" in digest
        for n in range(3):
            assert f"thing {n}" in digest
        assert "resolved 2026-11-18 22:05 UTC" in digest

    def test_the_preview_text_is_hidden_in_the_body(self) -> None:
        body = html_body(_msg())
        assert re.search(r'<div style="display:none[^>]*>A critical alert was raised\.</div>', body)


class TestBranding:
    def test_the_brand_comes_from_the_settings(self) -> None:
        settings = _settings(
            email_brand_name="Acme AI Gateway",
            email_brand_color="#0f766e",
            email_brand_tagline="IT Operations",
            email_footer="Questions? ops@acme.internal",
        )
        body = html_body(_msg(), brand_from(settings))
        assert "Acme AI Gateway" in body and "IT Operations" in body
        assert "Questions? ops@acme.internal" in body
        assert ">AA</div>" in body  # the monogram
        assert "background:#0f766e" in body  # the button
        text = _build(email_footer="Questions? ops@acme.internal").get_body(("plain",))
        assert text is not None and "Questions? ops@acme.internal" in text.get_content()

    def test_text_on_the_accent_stays_readable(self) -> None:
        assert Brand(color="#4f46e5").on_color == "#ffffff"
        assert Brand(color="#facc15").on_color == "#101828"

    def test_initials(self) -> None:
        assert Brand(name="Private AI Platform").initials == "PA"
        assert Brand(name="acme").initials == "A"
        assert Brand(name="  ").initials == "AI"

    def test_a_bad_colour_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _settings(email_brand_color="red")

    def test_dark_mode_and_small_screens_are_styled(self) -> None:
        body = html_body(_msg())
        assert "prefers-color-scheme:dark" in body and "max-width:600px" in body


class TestLogo:
    def test_a_logo_is_embedded_not_linked(self, tmp_path: Path) -> None:
        logo = tmp_path / "logo.png"
        logo.write_bytes(PNG)
        email = _build(email_logo_path=str(logo))
        page = email.get_body(("html",))
        assert page is not None and page.get_content_type() == "text/html"
        related = next(p for p in email.walk() if p.get_content_type() == "multipart/related")
        image = next(p for p in related.walk() if p.get_content_type() == "image/png")
        cid = image["Content-ID"].strip("<>")
        assert f'src="cid:{cid}"' in page.get_content()
        assert image.get_content() == PNG

    def test_a_missing_large_or_non_image_logo_is_skipped(self, tmp_path: Path) -> None:
        assert load_logo(None) is None
        assert load_logo(str(tmp_path / "missing.png")) is None
        big = tmp_path / "big.png"
        big.write_bytes(PNG + b"\0" * LOGO_MAX_BYTES)
        assert load_logo(str(big)) is None
        text = tmp_path / "logo.png"
        text.write_text("not an image")
        assert load_logo(str(text)) is None
        # The email still goes out, with the monogram.
        email = _build(email_logo_path=str(text))
        page = email.get_body(("html",))
        assert page is not None and "cid:" not in page.get_content()
