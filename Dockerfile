# Use Python 3.11 slim as lightweight base image
FROM python:3.11-slim

# Set environment variables for non-interactive installs and unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIPELINE_PROJECT_ROOT=/app \
    PIPELINE_STORAGE_ROOT=/app/storage \
    STATE_DB_PATH=/app/storage/state/pipeline.db

# Install system dependencies: ffmpeg, git, build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only PyTorch and Torchaudio to keep container compact
RUN pip install --no-cache-dir torch torchaudio --extra-index-url https://download.pytorch.org/whl/cpu

# Install ctc-forced-aligner for Arabic speech alignment
RUN pip install --no-cache-dir git+https://github.com/MahmoudAshraf97/ctc-forced-aligner.git

# Copy project metadata and install package
COPY pyproject.toml /app/
RUN pip install --no-cache-dir -e .

# Copy application source and bundled assets
COPY pipeline /app/pipeline

# Prepare persistent storage directories
RUN mkdir -p /app/storage/raw \
    /app/storage/audio \
    /app/storage/match \
    /app/storage/align \
    /app/storage/render \
    /app/storage/review-queue \
    /app/storage/publish \
    /app/storage/state \
    /app/storage/corpus

# Default HTTP port (used by Railway if PORT is assigned)
EXPOSE 8080

# Default command: run autonomous ingestion & orchestration worker
CMD ["python", "-m", "pipeline.ingestion.app"]
