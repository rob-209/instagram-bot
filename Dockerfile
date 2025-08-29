FROM python:3.11-slim

# Install ffmpeg for yt-dlp post-processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt


ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
