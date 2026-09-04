FROM python:3.12-slim

WORKDIR /app

# zoneinfo needs the OS tz database for America/New_York (M6 calendar).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Stdlib-only today: skip pip unless a real requirement appears.
RUN pkgs="$(grep -vE '^\s*(#|$)' requirements.txt || true)"; \
    if [ -n "$pkgs" ]; then \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY src ./src

ENV PYTHONUNBUFFERED=1
ENV DATABASE_PATH=/data/watchlist.db

CMD ["python", "-m", "src.main"]
