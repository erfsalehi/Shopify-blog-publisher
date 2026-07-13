# Trigger the pipeline from WhatsApp (Meta Cloud API)

A hands-on walkthrough. By the end you'll message your WhatsApp number
`draft: best flooring for bathrooms` and get back `📝 Drafted … Linear DAN-13`.

You'll learn the four core Cloud API concepts: **webhook verification**,
**signature verification**, the **inbound message payload**, and **sending
messages** (plus the **24-hour window**).

---

## 0. Mental model

```
You (WhatsApp)  ──►  Meta's servers  ──►  POST https://<your-host>/webhook
                                              │  (our FastAPI app: blog-pipeline serve)
                                              ▼
                                    verify signature → allow-list you →
                                    parse "draft: X" → run pipeline (background)
                                              │
   You (WhatsApp)  ◄──  Meta's servers  ◄──  POST graph.facebook.com/.../messages
```

Two directions:
- **Inbound**: Meta calls *your* URL when you message the number (a webhook).
- **Outbound**: *you* call Meta's Graph API to send a message back.

---

## 1. Create the Meta app

1. Go to <https://developers.facebook.com/> → **My Apps → Create App**.
2. Use case: **Other** → type: **Business** → name it (e.g. "DR Flooring Bot").
3. In the app dashboard, **Add product → WhatsApp → Set up**.

This creates a free **test business account** and a **test sender number** — no
Facebook Page or real number required to start.

## 2. Grab the values you need

**WhatsApp → API Setup** page:
- **Temporary access token** — a 24-hour token for testing → `WHATSAPP_ACCESS_TOKEN`.
- **Phone number ID** (under "From") → `WHATSAPP_PHONE_NUMBER_ID`.
  (This is *not* the phone number — it's an internal ID.)
- **"To" recipient**: click **Manage phone number list** and add **your own**
  WhatsApp number. In dev you can only message up to 5 pre-registered numbers.
  Put that same number (E.164, e.g. `15551234567`) in `WHATSAPP_ALLOWED_NUMBERS`.

**App Settings → Basic**:
- **App secret** (click Show) → `WHATSAPP_APP_SECRET`. Used to verify that
  webhook calls really come from Meta.

**Invent a verify token** — any random string, e.g. `dr-flooring-verify-9f3a` →
`WHATSAPP_VERIFY_TOKEN`. You'll type the same value into the dashboard next.

Fill all of these into `.env`.

## 3. Expose your webhook publicly

Meta must reach your `/webhook` over HTTPS. For local development, tunnel it:

```bash
pip install -e ".[whatsapp]"
blog-pipeline serve            # runs on http://0.0.0.0:8000

# in another terminal, expose it (pick one):
cloudflared tunnel --url http://localhost:8000
#   or:  ngrok http 8000
```

You'll get a public URL like `https://abc123.trycloudflare.com`. Your webhook
is `https://abc123.trycloudflare.com/webhook`.

For always-on hosting later, deploy the same app to Railway / Render / Fly.io
with your env vars and a persistent `DATABASE_URL` (Postgres).

## 4. Register the webhook (the GET handshake)

In the app: **WhatsApp → Configuration → Webhook → Edit**:
- **Callback URL**: `https://<your-tunnel>/webhook`
- **Verify token**: the exact `WHATSAPP_VERIFY_TOKEN` string you invented.

When you click **Verify and save**, Meta sends a **GET** to your URL:

```
GET /webhook?hub.mode=subscribe&hub.verify_token=dr-flooring-verify-9f3a&hub.challenge=1158201444
```

Our handler (`verify_challenge`) checks the token matches and echoes back
`hub.challenge` as plain text. If it matches, Meta marks the webhook verified.
*This is the whole handshake — a shared secret + an echo.*

Then click **Manage** and **subscribe to the `messages` field** (that's the one
that delivers inbound texts).

## 5. How an inbound message looks

Message the number "hi". Meta **POSTs** JSON to `/webhook`:

```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messaging_product": "whatsapp",
        "contacts": [{ "wa_id": "15551234567" }],
        "messages": [{
          "from": "15551234567",
          "id": "wamid.XXX",
          "type": "text",
          "text": { "body": "hi" }
        }]
      },
      "field": "messages"
    }]
  }]
}
```

The text is buried at `entry[].changes[].value.messages[].text.body`.
`extract_messages()` digs it out and ignores delivery/read receipts (which
arrive as `statuses` instead of `messages`).

## 6. Verifying the signature (security)

Every POST includes a header:

```
X-Hub-Signature-256: sha256=<hex>
```

where `<hex>` is `HMAC-SHA256(raw_request_body, app_secret)`. Our
`verify_signature()` recomputes it and compares in constant time. If it doesn't
match, we return **403** — this is what stops a random person who guesses your
URL from triggering (and publishing!) anything. We *also* allow-list your phone
number on top of that.

## 7. Sending a message back (Graph API + the 24h window)

To reply, we POST to:

```
POST https://graph.facebook.com/v21.0/<PHONE_NUMBER_ID>/messages
Authorization: Bearer <ACCESS_TOKEN>
{ "messaging_product": "whatsapp", "to": "15551234567",
  "type": "text", "text": { "body": "🛠️ On it…" } }
```

**The 24-hour window:** WhatsApp only lets a business send *free-form* text
within 24 hours of the user's last message. Since you trigger a run *by
messaging us*, that window is open and our replies (an instant "On it…" plus the
result minutes later) go through fine. You'd only need pre-approved **message
templates** if you wanted to message someone who *hasn't* messaged you recently
(e.g. an unsolicited scheduled digest).

## 8. Try it

With `blog-pipeline serve` running and the webhook verified, message your number:

| You send | What happens |
|---|---|
| `help` | Lists the commands |
| `status` | Pipeline health (published / synced / coverage) |
| `draft: heated tile floors` | Drafts one article now, replies with the Linear link |
| `daily` | Drafts everything due today |
| `calendar` | Refreshes the topic queue |

You'll get an instant "🛠️ On it…", then the result once the run finishes.

## 9. Going to production

- **Permanent token**: the 24h test token expires. Create a **System User** in
  Meta Business Settings, assign it to the app with `whatsapp_business_messaging`
  permission, and generate a **non-expiring token** → put it in
  `WHATSAPP_ACCESS_TOKEN`.
- **Your own number / business verification**: to move off the test sender and
  use a real branded number, you'll complete Meta **Business Verification** and
  add a phone number to the WhatsApp Business Account.
- **Host it**: deploy `blog-pipeline serve` somewhere always-on and set the
  webhook Callback URL to that host. Keep the same `WHATSAPP_VERIFY_TOKEN`.

## Security checklist

- ✅ Signature verified on every POST (`WHATSAPP_APP_SECRET`).
- ✅ Sender allow-listed (`WHATSAPP_ALLOWED_NUMBERS`) — only you can trigger.
- ✅ Secrets in `.env` (git-ignored), never committed.
- ⚠️ This endpoint can publish to your live store — keep the allow-list tight
  and rotate the token if it ever leaks.
