# Connect Google Search Console

Search Console is the only data source here that describes **your** site rather
than the market. DataForSEO says what people search; this says what you get
shown for, from what position, and whether that's rising or falling. Two things
depend on it:

- **Topic research** gets *striking-distance* queries — terms you already earn
  impressions for from positions 8–30. Google already thinks you're relevant;
  those are the shortest path to page one.
- **`run-refresh`** ranks candidates by measured decay instead of by age. Age is
  a poor proxy: an old post that still ranks should be left alone, while one
  that's quietly halved should jump the queue.

Both degrade gracefully — skip this and the pipeline still runs, just blind to
your own performance.

---

## 1. Create a service account

A service account is a robot Google identity with its own key. It's the right
choice for a cron job: no browser consent, no refresh tokens to expire.

1. [console.cloud.google.com](https://console.cloud.google.com) → create a
   project (or reuse one).
2. **APIs & Services → Library** → search **Google Search Console API** →
   **Enable**. Missing this step gives a `403 SERVICE_DISABLED` later.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Name it anything (`blog-pipeline`). No roles needed — Search Console
   permissions are granted separately in step 2, not through IAM.
4. Open the new service account → **Keys → Add key → Create new key → JSON**.
   A `.json` file downloads. That file is a credential; treat it like a
   password.

Note the `client_email` inside it — something like
`blog-pipeline@your-project.iam.gserviceaccount.com`. You need it next.

## 2. Grant it access to the property

**This is the step everyone misses**, and skipping it produces a `403` on a
property you can plainly see in your own browser. Creating a key doesn't grant
access to anything.

1. [search.google.com/search-console](https://search.google.com/search-console)
   → select `drflooring.ca`.
2. **Settings → Users and permissions → Add user**.
3. Paste the `client_email` from the JSON. Permission: **Full** (or
   **Restricted** — the pipeline only reads).

## 3. Add the secret

The whole JSON file goes in one secret, pasted verbatim including the outer
braces. A file path would be friendlier locally, but there's no filesystem to
put it on in Actions.

```bash
gh secret set GSC_CREDENTIALS_JSON < path/to/downloaded-key.json
```

Locally, put it in `.env` as a single line:

```
GSC_CREDENTIALS_JSON={"type":"service_account","project_id":"...",...}
```

### GSC_SITE_URL

Only needed if your property **isn't** the domain form. The property string
must match Search Console exactly:

| Property type in the UI | String to use |
|---|---|
| Domain (`drflooring.ca`) | `sc-domain:drflooring.ca` |
| URL prefix (`https://drflooring.ca/`) | `https://drflooring.ca/` — trailing slash required |

Blank derives `sc-domain:<PUBLIC_DOMAIN>`, which is right for a domain
property. Wrong form is the usual first failure and shows up as a `404`.

## 4. Verify

```bash
blog-pipeline sync-performance --list-sites
```

This lists every property the service account can actually read, and is the
fastest way to tell the two setup failures apart:

- **Empty list** → the key authenticates but step 2 was never done.
- **Lists a property, but not the one you configured** → your `GSC_SITE_URL`
  form is wrong. Copy the string it prints.

Then pull the data:

```bash
blog-pipeline sync-performance
```

It fetches the last 90 days *and* the preceding 90, because decay needs two
windows to mean anything — fetching both at once makes the trend readable from
the first run rather than months later.

Watch the `matched` count: it's how many Search Console pages joined to a known
article. If it's `0` while `pages` is non-zero, run `import-existing` first, and
check `PUBLIC_DOMAIN` matches the property — the join is on URL, and a mismatch
looks exactly like a site with no traffic.

## Notes

- **Data is never backfilled.** Search Console starts collecting when the
  property is verified. If you add a property today, there's no history to pull.
- **It lags 2–3 days.** `sync-performance` ends its window 3 days back; ending
  it today would report a partial tail as a decline.
- The weekly workflow runs `sync-performance` automatically, after
  `import-existing` (pages join to articles by URL) and before `run-calendar`
  (striking-distance queries feed research).
