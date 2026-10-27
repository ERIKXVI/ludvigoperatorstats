FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY r6tracker ./r6tracker
RUN pip install --no-cache-dir .

# The SQLite database lives here; mount a volume to keep it.
ENV DATA_DIR=/app/data
VOLUME /app/data

CMD ["r6tracker", "bot"]
