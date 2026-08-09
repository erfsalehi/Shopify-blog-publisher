"""D&R Flooring Control Center — a local dashboard over the blog pipeline.

A layer on top of `blog_pipeline`, not a rewrite: it imports that package's
Shopify/Search Console/GA4 clients as a library and keeps its own database for
everything it collects.

The architecture is one rule, and every module here obeys it:

    **Sync jobs talk to external APIs. The UI reads only the database.**

That is not a performance nicety. The machine this runs on sits behind a local
HTTP proxy that intermittently returns 429 or drops SSL connections, so a page
that called an API to render would fail unpredictably. It is also the only way
experiments and price history can exist at all — both need yesterday's numbers
kept, not re-fetched.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
