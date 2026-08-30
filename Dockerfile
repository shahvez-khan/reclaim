FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY ml ./ml
COPY frontend ./frontend
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# data/, models/, logs/ are created at runtime by the entrypoint —
# not baked into the image, so a mounted volume (see docker-compose.yml)
# persists them across container restarts.

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
