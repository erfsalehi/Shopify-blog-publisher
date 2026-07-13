import hashlib
import hmac

import pytest

from blog_pipeline.tools.whatsapp import (
    extract_messages,
    is_allowed,
    parse_command,
    verify_challenge,
    verify_signature,
)


@pytest.fixture
def _wa_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "my-verify-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "shhh-secret")
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "15551234567")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


# ── GET verification handshake ────────────────────────────────────
def test_verify_challenge_matches(_wa_env):
    assert verify_challenge("subscribe", "my-verify-token", "CHALLENGE123") == "CHALLENGE123"


def test_verify_challenge_wrong_token(_wa_env):
    assert verify_challenge("subscribe", "nope", "CHALLENGE123") is None


# ── POST signature ────────────────────────────────────────────────
def test_verify_signature_valid(_wa_env):
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"shhh-secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig) is True


def test_verify_signature_tampered(_wa_env):
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"shhh-secret", b"different", hashlib.sha256).hexdigest()
    assert verify_signature(body, sig) is False
    assert verify_signature(body, None) is False


# ── allow-list ────────────────────────────────────────────────────
def test_is_allowed(_wa_env):
    assert is_allowed("15551234567") is True
    assert is_allowed("+1 (555) 123-4567") is True  # compared on digits
    assert is_allowed("19999999999") is False


# ── inbound payload extraction ────────────────────────────────────
def test_extract_messages_reads_text_skips_statuses():
    payload = {
        "entry": [{
            "changes": [
                {"value": {"messages": [
                    {"from": "15551234567", "id": "wamid.1", "type": "text",
                     "text": {"body": "draft: bathroom flooring"}},
                    {"from": "15551234567", "id": "wamid.2", "type": "image"},
                ]}},
                {"value": {"statuses": [{"status": "delivered"}]}},
            ]
        }]
    }
    msgs = extract_messages(payload)
    assert len(msgs) == 1
    assert msgs[0].from_number == "15551234567"
    assert msgs[0].text == "draft: bathroom flooring"


# ── command parsing ───────────────────────────────────────────────
@pytest.mark.parametrize("text,action,arg", [
    ("daily", "daily", ""),
    ("run daily", "daily", ""),
    ("calendar", "calendar", ""),
    ("status", "status", ""),
    ("help", "help", ""),
    ("draft: best flooring for kitchens", "draft", "best flooring for kitchens"),
    ("draft radiant heating", "draft", "radiant heating"),
    ("write: vinyl vs laminate", "draft", "vinyl vs laminate"),
    ("something random", "unknown", "something random"),
    ("", "help", ""),
])
def test_parse_command(text, action, arg):
    cmd = parse_command(text)
    assert cmd.action == action
    assert cmd.arg == arg
