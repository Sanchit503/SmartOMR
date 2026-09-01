FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md PROJECT_SPEC.md ./
COPY omr ./omr
COPY prototype_eval ./prototype_eval

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

RUN useradd --create-home --shell /usr/sbin/nologin smartomr \
    && mkdir -p /app/data \
    && chown -R smartomr:smartomr /app/data

USER smartomr

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD smartomr-health --data-dir /app/data || exit 1

CMD ["smartomr-parse", "--help"]
