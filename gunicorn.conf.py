"""Gunicorn settings for MCH PDF Organizer (used by the Dockerfile / Render).

Render tells the service which port to listen on through $PORT; we bind 0.0.0.0 on it.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# One process: LibreOffice runs one conversion at a time to stay inside the memory of a
# small instance. Threads keep thumbnails, previews and downloads responsive meanwhile.
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

# A large Word/Excel file may convert for a few minutes (SOFFICE_TIMEOUT, default 240 s).
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5

# Heartbeat files in memory rather than on the container's disk
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

# No access log: request URLs carry only random ids, but there is no need to keep them.
accesslog = None
errorlog = "-"
loglevel = "info"
