"""Sync jobs — the only code here allowed to call an external API.

A job is a plain function returning a `JobResult`. The runner wraps it with
the parts every job needs identically: a `job_run` row, retry with backoff
against a proxy that fails at random, and a lock so a manual click can't race
the scheduler.

**Every job must be idempotent.** The runner retries the whole function on a
transient failure, so a job that appends rather than upserts will double-count
its own rows the first time the proxy hiccups mid-run.
"""

from __future__ import annotations

from dashboard.jobs.registry import JobResult, JobSpec, all_jobs, get_job, register

# Importing the modules is what registers their jobs, in the order they'd
# sensibly run: catalogue first (everything joins against it), then the
# measurement sources.
from dashboard.jobs import shopify_catalog as _shopify  # noqa: F401,E402
from dashboard.jobs import gsc as _gsc  # noqa: F401,E402
from dashboard.jobs import ga4 as _ga4  # noqa: F401,E402
from dashboard.jobs import ga4_city as _ga4_city  # noqa: F401,E402
from dashboard.jobs import local_serp as _local_serp  # noqa: F401,E402
from dashboard.jobs import ads_windsor as _ads  # noqa: F401,E402
from dashboard.jobs import blog_articles as _blog  # noqa: F401,E402
from dashboard.jobs import dataforseo_keywords as _dfs  # noqa: F401,E402
from dashboard.jobs import competitor_watch as _competitors  # noqa: F401,E402
from dashboard.jobs import publish_reconcile as _reconcile  # noqa: F401,E402
# Last: these read what the others just wrote.
from dashboard.jobs import alerts_job as _alerts  # noqa: F401,E402
from dashboard.jobs import advisor_job as _advisor  # noqa: F401,E402
from dashboard.jobs import strategy_weekly as _strategy  # noqa: F401,E402

__all__ = ["JobResult", "JobSpec", "all_jobs", "get_job", "register"]
