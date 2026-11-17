# mail-mcp-server image.
# Build the mailbox first (./run.sh build), then:
#   docker build -t mail-mcp-server:latest .
# The image serves MCP over streamable HTTP on port 8000. With the default
# env it uses the baked-in SQLite mailbox; point MAIL_MCP_BACKEND at
# postgres and MAIL_MCP_DSN at a Postgres service to use that backend.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY eval/ ./eval/
COPY scripts/ ./scripts/
COPY verify_results.py .
# Baked-in demo mailbox (built by ./run.sh build before docker build).
COPY data/mailbox.db ./data/mailbox.db

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["python", "-m", "src.server", "--http", "--port", "8000"]
