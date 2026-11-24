# LogosUpdater in a container: UI on http://localhost:8000 (see docker-compose.yml).
# All user data (config.yaml, work/, inputs/, usb_package/, logos) lives in the /data volume.
# USB sticks are not detected inside the container: take the USB package as a .zip download.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LOGOSUPDATER_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --create-home --uid 1000 app && mkdir /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 8000

# 0.0.0.0 inside the container only; compose publishes it on the host's 127.0.0.1
CMD ["python", "view_stations.py", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
