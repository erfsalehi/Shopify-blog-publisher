# Runs the always-on WhatsApp trigger webhook (blog-pipeline serve) on Railway
# or any other container host. The scheduled crons (weekly calendar, daily
# draft) stay on GitHub Actions (.github/workflows/) — this image is only for
# the web process that needs to be reachable 24/7.
FROM python:3.12-slim

# build-essential: safety net in case any dependency needs to build from
# source on this platform (most ship prebuilt wheels, so this rarely triggers).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what the install needs first, so dependency layers cache across
# rebuilds that only touch application code.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[whatsapp,postgres]"

# brand_voice.md etc. — read at runtime via BRAND_VOICE_PATH (relative path).
COPY prompts ./prompts

# Railway sets $PORT at runtime; 8000 is just the local-docker-run default.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT expands at container start, not at build time.
# serve_cmd runs init-db itself before starting the server.
CMD ["sh", "-c", "blog-pipeline serve --host 0.0.0.0 --port ${PORT}"]
