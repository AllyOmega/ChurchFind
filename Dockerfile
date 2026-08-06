# ChurchFind, built to run somewhere real.
#
# Two stages, for one reason: the database is a 60 MB build artifact derived from
# data/, and building it needs the scraper's parsing code but nothing at runtime
# needs the 40 MB of state JSON that produced it. The builder does the work and
# the runtime image copies out only the result.
#
# The image is stateless. The database lives on a mounted volume, and is seeded
# from the baked copy on first boot -- see docker-entrypoint.sh for why that is
# a copy rather than a symlink.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY data/ data/
COPY scraper/ scraper/
COPY server/ server/

# Building the database needs no third-party packages at all -- it is sqlite3
# and the standard library. FastAPI is a runtime concern.
RUN python server/build_db.py --db /build/seed.db


FROM python:3.12-slim

# curl is for the container healthcheck. Nothing else is added.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first so a code change does not re-resolve the dependency tree.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY assets/ assets/
COPY index.html .
COPY server/ server/
COPY scraper/ scraper/
COPY docker-entrypoint.sh /usr/local/bin/
COPY --from=builder /build/seed.db /app/seed.db

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 churchfind \
    && mkdir -p /data && chown churchfind:churchfind /data /app
USER churchfind

# Where the live database lives. Mount a volume here or every account is lost on
# the next deploy.
ENV CHURCHFIND_DB=/data/churchfind.db
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
