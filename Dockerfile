FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better build caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Persistent data lives here (settings.json, msgmap.db) — mounted as a volume.
RUN mkdir -p /app/data

# Run the forwarder (headless; logs go to Fly).
CMD ["python", "forwarder.py"]
