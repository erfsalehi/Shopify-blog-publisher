# Deploy to Railway

Railway hosts the one process that needs to be **always-on**: the WhatsApp
trigger webhook (`blog-pipeline serve`, [webhook.py](../src/blog_pipeline/webhook.py)).
It also gives you a managed **Postgres** database — useful even if you don't
deploy the webhook, since the scheduled crons in `.github/workflows/` need
Postgres too (SQLite doesn't survive GitHub Actions' ephemeral runners).

The repo ships `Dockerfile` + `railway.json` — Railway builds and runs the
image with no extra config beyond environment variables.

---

## 1. Create the project

1. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
   → select `Shopify-blog-publisher`.
2. Railway detects `railway.json` and builds via the Dockerfile automatically
   (`builder: DOCKERFILE`) — nothing to configure here.

## 2. Add Postgres

**+ New → Database → Add PostgreSQL** in the same project.

This creates a second service and a `DATABASE_URL` variable *on that Postgres
service*. Your app service needs its own copy of that value — the easiest way
is a **reference variable** (next step) rather than copy-pasting the string.

## 3. Set environment variables

On the **app service** (not the Postgres one) → **Variables**, add everything
from `.env.example` that you use, plus:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

That `${{Postgres.DATABASE_URL}}` syntax is Railway's reference-variable
syntax — it always points at the Postgres service's current connection
string, even if Railway rotates it. Our `normalize_database_url()`
([db/session.py](../src/blog_pipeline/db/session.py)) automatically rewrites
the bare `postgresql://` Railway hands you into `postgresql+psycopg://`, so
you don't need to edit the value.

Minimum set to actually run the webhook:

```
GOOGLE_API_KEY=...
LINEAR_API_KEY=...
LINEAR_TEAM=...
DATABASE_URL=${{Postgres.DATABASE_URL}}
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_VERIFY_TOKEN=...
WHATSAPP_APP_SECRET=...
WHATSAPP_ALLOWED_NUMBERS=15551234567
```

Add `SHOPIFY_*`, `OPENROUTER_API_KEY`, `DATAFORSEO_*`, `SLACK_WEBHOOK_URL`,
`BUSINESS_NAME`, etc. the same way as any other env var — same names as
`.env.example`.

## 4. Get a public URL

**Settings → Networking → Generate Domain** gives you
`https://<something>.up.railway.app`. (Or attach a custom domain there.)

Point your Meta app's webhook **Callback URL** at
`https://<something>.up.railway.app/webhook` (see
[whatsapp-setup.md](whatsapp-setup.md) §4 for the verify-token handshake).

## 5. Deploys

Railway redeploys automatically on every push to `main` (it's watching the
GitHub repo). Check **Deployments** tab for build/runtime logs — that's also
where you'll see the health check (`GET /health`, configured in
`railway.json`) confirming the container came up.

## 6. Share the database with GitHub Actions crons

The weekly/daily cron workflows (`.github/workflows/`) run on GitHub's
infra, outside Railway's private network, so they need Postgres's **public**
connection string, not the internal one:

1. Railway → Postgres service → **Settings → Networking → TCP Proxy** →
   enable it if not already (gives you a `postgres://…:<proxy-port>/railway`
   URL reachable from the internet).
2. Copy that URL into the `DATABASE_URL` **GitHub Actions secret**
   (Repo → Settings → Secrets and variables → Actions).

Now the webhook (drafting on demand) and the crons (scheduled drafting) share
one calendar/article history instead of drifting apart.

## Notes

- **Local dev doesn't need any of this** — `blog-pipeline serve` locally still
  defaults to SQLite; Railway/Postgres is purely for the deployed webhook.
- **Cost**: Railway's free trial credit covers light usage; a small always-on
  service + a small Postgres instance typically lands on the Hobby plan
  (~$5/mo base). Check current pricing at railway.app/pricing.
- **Logs**: `railway logs` (CLI) or the Deployments tab streams stdout —
  useful for watching a WhatsApp-triggered run in real time.
