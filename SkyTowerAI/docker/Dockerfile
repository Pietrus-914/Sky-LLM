# SkyTower-AI Flask server — Linux image (MT5 stays native on the Windows host).
# Build context is the SkyTowerAI/ directory: docker compose build (see docker-compose.yml)
FROM python:3.11-slim

ENV TZ=UTC \
    PYTHONUNBUFFERED=1

WORKDIR /app

# requirements.txt is the Linux-safe set (MetaTrader5 lives in requirements-windows.txt)
COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy python/ contents into /app so relative paths (templates/, logs/) resolve
# exactly like running `python server.py` from the python/ directory.
COPY python/ /app/

EXPOSE 5555

CMD ["python", "server.py"]
