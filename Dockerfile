# Multi-stage / lightweight production container for Discord HR Bot
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for optimal Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and sample data
COPY bot/ ./bot/
COPY sample_data/ ./sample_data/

# Create non-root user and persistent directories (logs and credentials)
RUN mkdir -p logs credentials && \
    useradd -u 10001 -m appuser && \
    chown -R appuser:appuser /app

USER appuser

# Run bot module
CMD ["python", "-m", "bot.main"]

