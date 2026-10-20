# Reproducible environment for the read-only CLI: `liq phase0 collect`, `liq phase0 report`,
# `liq registry verify`, `liq doctor`. Nothing in this image can sign anything — there is no
# key material anywhere in the build, and step 20/D24 mean there never should be. It exists to
# make "it works on my machine" not a precondition for running a historical collection.
#
# Two stages so the final image carries no compiler and no build cache, only the installed
# package and its dependencies — smaller, and nothing in it can be used to rebuild or modify
# the source it was built from.

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[dev]"

FROM python:3.12-slim

# Certificates for TLS to RPC providers; nothing else the runtime needs beyond the Python
# standard library and what pip installed.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local

WORKDIR /app
COPY config ./config
COPY README.md ARCHITECTURE.md ROADMAP.md ./

# Collected data and .env are mounted, not baked in — a rebuilt image must never accidentally
# bundle a key or a partial scan into a layer that gets pushed somewhere.
VOLUME ["/app/data"]
ENV LIQ_DATA_DIR=/app/data

ENTRYPOINT ["liq"]
CMD ["--help"]
