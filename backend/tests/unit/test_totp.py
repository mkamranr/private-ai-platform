"""TOTP and recovery codes (E15), against the RFC test vectors."""

from __future__ import annotations

import base64

from app.core import totp

# RFC 6238 appendix B: the SHA-1 seed is the ASCII string "12345678901234567890".
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


class TestRfcVectors:
    def test_rfc6238_sha1_vectors(self) -> None:
        # The RFC lists 8-digit codes; the 6-digit code is their last six digits.
        for unix_time, eight_digits in (
            (59, "94287082"),
            (1111111109, "07081804"),
            (1111111111, "14050471"),
            (1234567890, "89005924"),
            (2000000000, "69279037"),
        ):
            step = totp.current_step(unix_time)
            assert totp.code_at(RFC_SECRET, step) == eight_digits[-6:]


class TestVerify:
    def test_accepts_the_current_code_and_one_step_of_drift(self) -> None:
        now = 1_700_000_000.0
        step = totp.current_step(now)
        for drift in (-1, 0, 1):
            code = totp.code_at(RFC_SECRET, step + drift)
            assert totp.verify(RFC_SECRET, code, now=now) == step + drift

    def test_refuses_codes_outside_the_window(self) -> None:
        now = 1_700_000_000.0
        step = totp.current_step(now)
        assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + 2), now=now) is None
        assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 2), now=now) is None

    def test_a_used_step_is_refused(self) -> None:
        now = 1_700_000_000.0
        step = totp.current_step(now)
        code = totp.code_at(RFC_SECRET, step)
        assert totp.verify(RFC_SECRET, code, now=now, last_step=step) is None
        assert totp.verify(RFC_SECRET, code, now=now, last_step=step - 1) == step

    def test_spaces_are_ignored_and_junk_refused(self) -> None:
        now = 1_700_000_000.0
        code = totp.code_at(RFC_SECRET, totp.current_step(now))
        assert totp.verify(RFC_SECRET, f"{code[:3]} {code[3:]}", now=now) is not None
        for junk in ("", "12345", "1234567", "abcdef"):
            assert totp.verify(RFC_SECRET, junk, now=now) is None


class TestSecretsAndUris:
    def test_new_secret_is_160_bits_of_base32(self) -> None:
        secret = totp.new_secret()
        assert len(base64.b32decode(secret + "=" * (-len(secret) % 8))) == 20
        assert secret != totp.new_secret()

    def test_provisioning_uri_names_issuer_and_account(self) -> None:
        uri = totp.provisioning_uri("ABC", account="ana", issuer="Private AI Platform")
        assert uri.startswith("otpauth://totp/Private%20AI%20Platform%3Aana?")
        assert "secret=ABC" in uri and "issuer=Private+AI+Platform" in uri
        assert "digits=6" in uri and "period=30" in uri

    def test_qr_svg_is_an_inline_svg(self) -> None:
        svg = totp.qr_svg("otpauth://totp/x?secret=ABC")
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
        assert "<?xml" not in svg and "http://" not in svg.replace("http://www.w3.org", "")


class TestRecoveryCodes:
    def test_codes_are_unique_and_recognised(self) -> None:
        codes = totp.new_recovery_codes()
        assert len(codes) == 10 and len(set(codes)) == 10
        for code in codes:
            assert totp.looks_like_recovery_code(code)
            assert totp.looks_like_recovery_code(code.upper().replace("-", " "))

    def test_hash_ignores_case_and_separators(self) -> None:
        code = totp.new_recovery_codes(1)[0]
        assert totp.hash_recovery_code(code) == totp.hash_recovery_code(code.upper())
        assert totp.hash_recovery_code(code) == totp.hash_recovery_code(code.replace("-", ""))

    def test_six_digit_codes_are_not_recovery_codes(self) -> None:
        assert not totp.looks_like_recovery_code("123456")
