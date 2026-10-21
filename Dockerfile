# MCH PDF Organizer - production image (Render builds and runs this file)
#
# Base: official Python 3.12 on Debian 13 "trixie" -> LibreOffice 25.2 from Debian.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    GUNICORN_CMD_ARGS= \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive

# 1) LibreOffice Writer + Calc, used headless for Word/Excel -> PDF, and tini as the
#    container's init (forwards signals and reaps LibreOffice processes that were stopped
#    after a timeout). Required: the build stops here if they can't be installed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc fontconfig tini \
 && rm -rf /var/lib/apt/lists/*

# 2) Fonts so documents keep their layout: Liberation (= Arial/Times New Roman/Courier New),
#    Carlito (= Calibri), Caladea (= Cambria), DejaVu, Noto + Amiri (Arabic).
#    Installed one by one so a renamed font package can't break the build; every missing
#    one is printed in the build log, and the font check below shows what was installed.
RUN apt-get update \
 && for p in fonts-liberation fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea \
             fonts-dejavu-core fonts-noto-core fonts-hosny-amiri; do \
      apt-get install -y --no-install-recommends "$p" || echo "WARNING: font package $p not available"; \
    done \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f \
 && echo "Fonts for common Office faces:" \
 && for f in Arial "Times New Roman" Calibri Cambria "Noto Naskh Arabic" Amiri; do \
      printf '  %-18s -> %s\n' "$f" "$(fc-match -f '%{family}' "$f")"; \
    done

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py converter.py pdf_ops.py storage.py gunicorn.conf.py ./
COPY templates ./templates
COPY static ./static

# Run as an unprivileged user. Uploads and LibreOffice's profile live under /tmp only
# (temporary; Render's disk is wiped on every restart/deploy).
RUN useradd --create-home --uid 10001 appuser
USER appuser
ENV HOME=/home/appuser \
    TMPDIR=/tmp \
    MCH_TMP_DIR=/tmp/mch-pdf-organizer \
    SAL_USE_VCLPLUGIN=svp \
    PORT=10000

# 3) Build-time proof that LibreOffice works inside this image: convert a real Word
#    document and a real workbook to PDF as the app user. The build fails otherwise.
RUN python converter.py --self-test && rm -rf /tmp/mch-*

EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','10000'), timeout=8)"

# Gunicorn binds 0.0.0.0:$PORT (Render supplies PORT); see gunicorn.conf.py
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
