# Syzygy SMILE listener image.
#
# Vendored from mskcc/smile-client @ 548a394a180dde05a99dd3136b65552c93a8753c
# (smile_client/ was copied verbatim into this directory on 2026-10-28 and
# must not be modified here; refresh by re-copying from a fresh clone).
#
# This image serves two entry points:
#   python /app/run.py             — standalone JetStream consumer (smile-client)
#   python /app/init_nats_stream.py — one-shot dev bootstrap of the MDB_STREAM stream
#
# smile-client imports django.conf at module load, so Django is installed
# import-only (no settings configured, no app run); a modern version is used
# instead of the client's pinned Django 2.2.
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY smile_client/ /app/smile_client/
COPY smile_bridge.py /app/smile_bridge.py
COPY nats_publisher.py /app/nats_publisher.py
COPY run.py /app/run.py
COPY init_nats_stream.py /app/init_nats_stream.py

CMD ["python", "/app/run.py"]
