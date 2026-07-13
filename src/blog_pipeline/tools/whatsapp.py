"""Meta WhatsApp Cloud API client + webhook helpers.

Concepts (Meta Cloud API), so the flow is legible:

  * Webhook verification (GET): when you save the callback URL in the Meta app,
    Meta sends GET ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    You echo `hub.challenge` back *only* if the verify_token matches yours.
    (`verify_challenge` below.)

  * Inbound messages (POST): when someone messages your number, Meta POSTs a
    nested JSON payload. The text lives at
    entry[].changes[].value.messages[].{from, text.body}. Delivery/read
    receipts arrive as `statuses` (ignored). (`extract_messages` below.)

  * Signature: every webhook POST carries an `X-Hub-Signature-256: sha256=<hmac>`
    header — HMAC-SHA256 of the raw body keyed by your App Secret. Verifying it
    proves the request really came from Meta. (`verify_signature` below.)

  * Sending: POST to graph.facebook.com/<ver>/<phone_number_id>/messages with a
    Bearer access token. Free-form text is allowed only inside the 24h window
    opened by the user's last message — fine here, since the user just messaged
    us to trigger the run. (`WhatsAppClient.send_text`.)
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

import httpx

from blog_pipeline.config import get_settings


class WhatsAppError(RuntimeError):
    pass


# ── webhook verification (GET) ───────────────────────────────────
def verify_challenge(mode: str, token: str, challenge: str) -> str | None:
    """Return the challenge to echo back if the subscribe handshake is valid,
    else None (caller should 403)."""
    settings = get_settings()
    if mode == "subscribe" and token and token == settings.whatsapp_verify_token:
        return challenge
    return None


# ── signature verification (POST) ────────────────────────────────
def verify_signature(raw_body: bytes, header: str | None, app_secret: str | None = None) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header against HMAC-SHA256
    of the raw request body keyed by the app secret."""
    secret = app_secret if app_secret is not None else get_settings().whatsapp_app_secret
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


# ── inbound payload parsing ───────────────────────────────────────
@dataclass
class InboundMessage:
    from_number: str
    text: str
    message_id: str


def extract_messages(payload: dict) -> list[InboundMessage]:
    """Pull text messages out of a webhook payload; skip status callbacks."""
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                if msg.get("type") != "text":
                    continue
                out.append(
                    InboundMessage(
                        from_number=msg.get("from", ""),
                        text=(msg.get("text", {}) or {}).get("body", "").strip(),
                        message_id=msg.get("id", ""),
                    )
                )
    return out


def is_allowed(number: str) -> bool:
    """Only allow-listed numbers may trigger the pipeline. Compares on digits
    so '+1 555…' and '1555…' match."""
    allowed = get_settings().whatsapp_allowed_list
    if not allowed:
        return False
    digits = "".join(c for c in number if c.isdigit())
    return any(digits == "".join(c for c in a if c.isdigit()) for a in allowed)


# ── command parsing ───────────────────────────────────────────────
@dataclass
class Command:
    action: str  # "daily" | "calendar" | "draft" | "status" | "help" | "unknown"
    arg: str = ""


def parse_command(text: str) -> Command:
    """Map a WhatsApp message to a pipeline action. Forgiving of phrasing."""
    t = (text or "").strip()
    low = t.lower()
    if not low:
        return Command("help")
    if low in ("help", "?", "commands", "hi", "hello"):
        return Command("help")
    if low in ("status", "health", "stats"):
        return Command("status")
    if low in ("daily", "run daily", "run-daily", "publish today", "today"):
        return Command("daily")
    if low in ("calendar", "run calendar", "run-calendar", "refresh", "topics"):
        return Command("calendar")
    for prefix in ("draft:", "draft ", "write:", "write ", "article:", "article "):
        if low.startswith(prefix):
            topic = t[len(prefix):].strip(" :")
            return Command("draft", topic) if topic else Command("help")
    return Command("unknown", t)


HELP_TEXT = (
    "🛠️ *Blog pipeline commands*\n"
    "• `draft: <topic>` — draft one article now\n"
    "• `daily` — draft everything due today\n"
    "• `calendar` — refresh the topic queue\n"
    "• `status` — pipeline health\n"
    "• `help` — this message"
)


# ── outbound (Graph API) ──────────────────────────────────────────
class WhatsAppClient:
    def __init__(self, access_token: str | None = None, phone_number_id: str | None = None) -> None:
        s = get_settings()
        self.token = access_token or s.whatsapp_access_token
        self.phone_number_id = phone_number_id or s.whatsapp_phone_number_id
        self.version = s.whatsapp_graph_version
        if not self.token or not self.phone_number_id:
            raise WhatsAppError("WhatsApp not configured: set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID.")

    def send_text(self, to: str, body: str) -> dict:
        url = f"https://graph.facebook.com/{self.version}/{self.phone_number_id}/messages"
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body[:4096], "preview_url": True},
            },
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise WhatsAppError(f"send failed {resp.status_code}: {resp.text[:300]}")
        return resp.json()


def notify(to: str, body: str) -> None:
    """Best-effort outbound message; never raises (used from background tasks)."""
    try:
        WhatsAppClient().send_text(to, body)
    except Exception:
        pass
