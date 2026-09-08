FROM python:3.12-slim

# System deps: ffmpeg (Whisper), libsndfile (soundfile), build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# R-18: pre-download Whisper base model at build time so cold starts skip the ~140 MB fetch.
# Model is cached to /root/.cache/whisper/ inside this image layer.
RUN python -c "import whisper; whisper.load_model('base')"

# Copy all application modules and runtime assets.
# Keeping the complete top-level Python module set prevents newly added
# services from passing local tests but being omitted from the container.
COPY *.py ./
COPY knowledge_bank/ ./knowledge_bank/
COPY static/ ./static/
COPY skills/ ./skills/
COPY data/ ./data/

# Copy knowledge base if present (optional)
COPY KNOWLEDGE.md* ./

# Runtime data dirs (SQLite DB and audio cache)
# /data is the Railway persistent volume — both DB and audio cache live here
RUN mkdir -p audio_cache static/temp_audio /data/artists /data/audio_cache

EXPOSE 8765

ENV MAESTRO_DIR=/app

# $PORT is injected by Railway; falls back to 8765 for local use
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8765}"]
