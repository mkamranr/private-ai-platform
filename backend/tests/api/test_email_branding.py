"""Branded notification emails (E49): runtime branding settings and the email preview."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from app.models.auth import User
from tests.api.conftest import _user_with
from tests.conftest import auth_header

PREVIEW = "/api/v1/notifications/email-preview"


class TestPreview:
    @pytest.mark.parametrize("kind", ["raised", "resolved", "digest", "approval", "test"])
    async def test_every_kind_previews_without_a_mail_server(
        self, kind: str, client: AsyncClient, tokens, superuser: User
    ) -> None:
        response = await client.get(
            PREVIEW, params={"kind": kind}, headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == kind
        assert body["subject"].startswith("[AI Platform] ")
        assert body["html"].startswith("<!doctype html>") and "Private AI Platform" in body["html"]
        assert body["text"]

    async def test_an_unknown_kind_is_refused(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        response = await client.get(
            PREVIEW, params={"kind": "nope"}, headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 422

    async def test_needs_notification_manage(
        self, client: AsyncClient, tokens, session: AsyncSession, settings: Settings
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.SETTINGS_VIEW], name="e49-viewer")
        response = await client.get(PREVIEW, headers=auth_header(tokens, viewer))
        assert response.status_code == 403


class TestBrandingSettings:
    async def test_changed_on_the_settings_page_it_shows_in_the_next_email(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        headers = auth_header(tokens, superuser)
        for key, value in (
            ("notifications.email_brand_name", "Acme AI Gateway"),
            ("notifications.email_brand_color", "#0f766e"),
            ("notifications.email_brand_tagline", "IT Operations"),
            ("notifications.email_footer", "Questions? ops@acme.internal"),
            ("notifications.email_subject_prefix", "[Acme AI]"),
        ):
            changed = await client.put(
                f"/api/v1/settings/{key}", json={"value": value}, headers=headers
            )
            assert changed.status_code == 200, changed.text

        body = (await client.get(PREVIEW, headers=headers)).json()
        assert body["subject"].startswith("[Acme AI] [CRITICAL]")
        html = body["html"]
        assert "Acme AI Gateway" in html and "IT Operations" in html
        assert "background:#0f766e" in html and ">AA</div>" in html
        assert "Questions? ops@acme.internal" in body["text"]

    async def test_a_colour_that_is_not_hex_is_refused(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        response = await client.put(
            "/api/v1/settings/notifications.email_brand_color",
            json={"value": "purple"},
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 422, response.text

    async def test_the_settings_page_lists_the_branding(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        response = await client.get("/api/v1/settings", headers=auth_header(tokens, superuser))
        assert response.status_code == 200
        text = response.text
        assert "notifications.email_brand_name" in text and "Email branding" in text
        # The logo file is a deployment setting: shown, not editable.
        assert "NOTIFICATIONS__EMAIL_LOGO_PATH" in text
