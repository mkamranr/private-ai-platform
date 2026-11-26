"""Branded HTML notification emails (expansions E48, E49).

One layout for every kind of email the platform sends: an alert being raised, escalated
or resolved, a digest of what quiet hours held (E47), a channel test, and a reminder to
someone who can decide an approval (E39).

**Branding** comes from :class:`Brand`, built from the ``notifications`` settings: a name,
an accent colour, an optional tagline and footer (all runtime settings, so the Settings
page can change them), and an optional logo file named in ``.env``. Without a logo the
header shows a monogram, the brand's initials on the accent colour.

**Rules every template keeps:**

* Self-contained. Inline styles, plus a small ``<style>`` block for dark mode and narrow
  screens. No web fonts, scripts or remote images. A logo travels inside the email as a
  ``cid:`` part, so opening a message loads nothing and works the same on an air-gapped
  network.
* Every value is HTML-escaped, so an alert's title or message cannot add markup.
* The only link is the console (its Alerts or Approvals page), and only when
  ``NOTIFICATIONS__CONSOLE_URL`` is set.
* Layout tables (``role="presentation"``), because Outlook ignores most modern CSS.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.notification_senders import Message

DEFAULT_COLOR = "#4f46e5"

#: Badge colours (background, text) by what a message reports.
_BADGE = {
    "CRITICAL": ("#fee4e2", "#b42318"),
    "WARNING": ("#fef0c7", "#b54708"),
    "INFO": ("#d1e9ff", "#175cd3"),
    "RESOLVED": ("#dcfae6", "#067647"),
    "NEUTRAL": ("#f2f4f7", "#344054"),
}
_SEVERITY_ORDER = ("CRITICAL", "WARNING", "INFO")


@dataclass(frozen=True, slots=True)
class Brand:
    name: str = "Private AI Platform"
    color: str = DEFAULT_COLOR
    tagline: str = ""
    footer: str = ""
    #: The ``Content-ID`` of an embedded logo, or a ``data:`` URI for a browser preview.
    logo_src: str | None = None

    @property
    def initials(self) -> str:
        words = [w for w in self.name.replace("-", " ").split() if w[:1].isalnum()]
        letters = "".join(w[0] for w in words[:2]).upper()
        return letters or "AI"

    @property
    def on_color(self) -> str:
        """Readable text on the accent colour: white on dark, near-black on light."""
        return "#ffffff" if _luminance(self.color) < 0.55 else "#101828"


def _luminance(color: str) -> float:
    try:
        r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    except ValueError:
        return 0.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _badge(label: str, tone: str) -> str:
    bg, fg = _BADGE.get(tone, _BADGE["NEUTRAL"])
    return (
        f'<span class="badge" style="display:inline-block;padding:3px 10px;border-radius:999px;'
        f"background:{bg};color:{fg};font-size:12px;font-weight:600;letter-spacing:.02em;"
        f'text-transform:uppercase">{_e(label)}</span>'
    )


def _header(brand: Brand) -> str:
    if brand.logo_src:
        mark = (
            f'<img src="{_e(brand.logo_src)}" alt="{_e(brand.name)}" height="32" '
            'style="display:block;height:32px;max-width:160px;border:0">'
        )
    else:
        mark = (
            f'<div style="width:32px;height:32px;line-height:32px;border-radius:8px;'
            f"background:{brand.color};color:{brand.on_color};text-align:center;"
            f'font-size:13px;font-weight:700">{_e(brand.initials)}</div>'
        )
    tagline = (
        f'<div class="muted" style="font-size:12px;color:#667085">{_e(brand.tagline)}</div>'
        if brand.tagline
        else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse"><tr>'
        f'<td width="44" style="padding:0 12px 0 0;vertical-align:middle">{mark}</td>'
        '<td style="vertical-align:middle">'
        f'<div style="font-size:15px;font-weight:700;color:#101828" class="ink">'
        f"{_e(brand.name)}</div>{tagline}</td></tr></table>"
    )


def _button(label: str, href: str, brand: Brand) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0 0">'
        f'<tr><td style="border-radius:8px;background:{brand.color}">'
        f'<a href="{_e(href)}" style="display:inline-block;padding:11px 20px;'
        f"font-size:14px;font-weight:600;color:{brand.on_color};text-decoration:none;"
        f'border-radius:8px">{_e(label)}</a></td></tr></table>'
    )


def _facts(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td class="muted" style="padding:6px 16px 6px 0;color:#667085;'
        f'white-space:nowrap;vertical-align:top">{_e(k)}</td>'
        f'<td class="ink" style="padding:6px 0;color:#101828">{_e(v)}</td></tr>'
        for k, v in rows
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" class="facts" '
        'style="border-collapse:collapse;margin:20px 0 0;width:100%;'
        f'border-top:1px solid #eaecf0;font-size:13px">{cells}</table>'
    )


def _tone(msg: Message) -> tuple[str, str]:
    """The badge's label and tone."""
    from app.services.notification_senders import DIGEST_EVENT

    if msg.event == "resolved":
        return "Resolved", "RESOLVED"
    if msg.event == "test":
        return "Test", "NEUTRAL"
    if msg.kind == "approval":
        return "Approval waiting", "WARNING"
    if msg.event == DIGEST_EVENT:
        return "Digest", "NEUTRAL"
    if msg.event == "summary":
        return "Summary", "NEUTRAL"
    return msg.severity.title(), msg.severity


def _intro(msg: Message, brand: Brand) -> str:
    from app.services.notification_senders import DIGEST_EVENT

    severity = msg.severity.lower()
    if msg.kind == "approval":
        return "You can decide this approval. Until someone does, the run waits."
    if msg.event == "resolved":
        return "This alert has been resolved. No action is needed."
    if msg.event == "escalated":
        return f"An open alert has escalated to {severity}."
    if msg.event == "test":
        return f"This is a test from {brand.name}. If you can read it, this channel works."
    if msg.event == DIGEST_EVENT:
        return "These were held while the channel's quiet hours were on, and are sent together."
    if msg.event == "summary":
        return f"What this channel's alerts did. {msg.message or ''}".strip()
    article = "An" if severity[:1] in "aeiou" else "A"
    return f"{article} {severity} alert was raised."


def _digest(msg: Message) -> str:
    from app.services.notification_senders import DIGEST_MAX_LINES, _when

    counts: dict[str, int] = {}
    for item in msg.items:
        key = "RESOLVED" if item.event == "resolved" else item.severity
        counts[key] = counts.get(key, 0) + 1
    summary = " ".join(
        _badge(f"{counts[k]} {k.lower()}", k)
        for k in (*_SEVERITY_ORDER, "RESOLVED")
        if counts.get(k)
    )
    rows = []
    for item in msg.items[:DIGEST_MAX_LINES]:
        resolved = item.event == "resolved"
        tone = "RESOLVED" if resolved else item.severity
        fg = _BADGE.get(tone, _BADGE["NEUTRAL"])[1]
        at = item.resolved_at if resolved else item.raised_at
        what = "resolved" if resolved else item.event
        rows.append(
            '<tr><td style="padding:10px 0;border-top:1px solid #eaecf0;vertical-align:top">'
            f'<div style="font-size:11px;font-weight:700;color:{fg};text-transform:uppercase">'
            f"{_e(tone)}</div>"
            f'<div class="ink" style="font-size:14px;color:#101828">{_e(item.title)}</div>'
            f'<div class="muted" style="font-size:12px;color:#667085">{_e(what)} '
            f"{_e(_when(at))} &middot; {_e(item.source)}</div></td></tr>"
        )
    more = (
        f'<p class="muted" style="margin:12px 0 0;font-size:13px;color:#667085">... and '
        f"{len(msg.items) - DIGEST_MAX_LINES} more on the Alerts page.</p>"
        if len(msg.items) > DIGEST_MAX_LINES
        else ""
    )
    return (
        f'<p style="margin:16px 0 0">{summary}</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;margin:16px 0 0">{"".join(rows)}</table>{more}'
    )


def _details(msg: Message) -> str:
    from app.services.notification_senders import _EVENT_LABEL, _when

    text = (
        '<div class="panel" style="margin:20px 0 0;padding:14px 16px;border-radius:8px;'
        "background:#f9fafb;border:1px solid #eaecf0;font-size:14px;color:#344054;"
        f'white-space:pre-wrap">{_e(msg.message)}</div>'
        if msg.message
        else ""
    )
    if msg.event == "test":
        return text
    when = msg.resolved_at if msg.event == "resolved" else msg.raised_at
    rows = [("Source", msg.source), (_EVENT_LABEL.get(msg.event, msg.event), _when(when))]
    if msg.kind != "approval" and msg.occurrences > 1:
        rows.append(("Seen", f"{msg.occurrences} times"))
    return text + _facts(rows)


def render(msg: Message, brand: Brand | None = None) -> str:
    """The HTML part of the email for ``msg``."""
    from app.services.notification_senders import DIGEST_EVENT

    brand = brand or Brand()
    label, tone = _tone(msg)
    accent = _BADGE.get(tone, _BADGE["NEUTRAL"])[1] if tone != "NEUTRAL" else brand.color
    body = _digest(msg) if msg.event in (DIGEST_EVENT, "summary") else _details(msg)
    button = ""
    if msg.link:
        page = "Approvals" if msg.kind == "approval" else "Alerts"
        button = _button(f"Open the {page} page", msg.link, brand)
    footer = _e(brand.footer) + "<br>" if brand.footer else ""
    why = (
        "You received this because you can decide approvals on this platform. Turn these "
        "emails off under your account (click your name in the console)."
        if msg.kind == "approval"
        else "You received this because this address is on a notification channel. "
        "Administrators change where alerts go on the Alerts page."
    )
    preheader = _intro(msg, brand)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{_e(msg.heading)}</title>"
        "<style>"
        "@media (max-width:600px){.card{padding:20px !important}.outer{padding:12px !important}}"
        "@media (prefers-color-scheme:dark){body,.outer{background:#0c111d !important}"
        ".card{background:#161b26 !important;border-color:#333741 !important}"
        ".ink,h1{color:#f5f5f6 !important}.muted{color:#94969c !important}"
        ".panel{background:#1f242f !important;border-color:#333741 !important;"
        "color:#cecfd2 !important}}"
        "</style></head>"
        '<body style="margin:0;padding:0;background:#f2f4f7">'
        # Preview text that mail clients show next to the subject, hidden in the body.
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">'
        f"{_e(preheader)}</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="outer" '
        'style="background:#f2f4f7;padding:24px 12px;'
        "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif\">"
        '<tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px">'
        f'<tr><td style="padding:0 4px 16px">{_header(brand)}</td></tr>'
        '<tr><td class="card" style="background:#ffffff;border:1px solid #eaecf0;'
        f'border-top:4px solid {accent};border-radius:12px;padding:28px">'
        f"{_badge(label, tone)}"
        f'<h1 style="margin:14px 0 6px;font-size:20px;line-height:1.35;color:#101828">'
        f"{_e(msg.title)}</h1>"
        f'<p class="muted" style="margin:0;font-size:14px;color:#475467">{_e(preheader)}</p>'
        f"{body}{button}</td></tr>"
        '<tr><td class="muted" style="padding:16px 4px 0;font-size:12px;line-height:1.5;'
        f'color:#667085">{footer}{_e(why)}</td></tr>'
        "</table></td></tr></table></body></html>"
    )
