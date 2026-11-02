# The Claude connector (app/connector.py) for Render's Docker runtime: Python 3.12 plus LibreOffice, which makes the
# slide pictures for the review screen where PowerPoint isn't installed (app/render.py). Setup: app/connector-setup.md.
# Only the connector runs here: the test UI (app.main) has no sign-in and is never deployed.
FROM python:3.12-slim-bookworm

# LibreOffice Impress without its extras, and fonts metric-compatible with Arial, Times New Roman, Calibri and
# Cambria, so text in the pictures wraps as it does in PowerPoint.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-impress fonts-liberation fonts-dejavu-core \
      fonts-crosextra-carlito fonts-crosextra-caladea \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY icons icons

ENV ICON_LIBRARY_DIR=/srv/icons PYTHONUNBUFFERED=1
# Render sets PORT. No access log: run links carry their private key in the path.
CMD ["sh", "-c", "exec uvicorn app.connector:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips '*' --no-access-log"]
