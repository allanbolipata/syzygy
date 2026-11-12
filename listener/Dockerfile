# Syzygy SMILE listener image.
#
# smile-client is NOT vendored in this repo anymore: the upstream
# mskcc/smile-client package is pip-installed from git, pinned to commit
# 548a394a180dde05a99dd3136b65552c93a8753c (see the RUN layer below).
# --no-deps is required there: upstream setup.cfg pins Django==2.2.28,
# which cannot install on the python:3.14 image, and that pin would break
# a normal dependency-resolution install. The real pins (modern Django,
# nats-py, docopt, httpx) live in listener/requirements.txt.
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

# Upstream smile-client pinned to a commit; --no-deps so its Django 2.2.28
# setup.cfg pin is never resolved (requirements.txt carries our pins).
RUN pip install --no-cache-dir --no-deps "smile_client @ git+https://github.com/mskcc/smile-client@548a394a180dde05a99dd3136b65552c93a8753c"

COPY smile_bridge.py /app/smile_bridge.py
COPY nats_publisher.py /app/nats_publisher.py
COPY run.py /app/run.py
COPY init_nats_stream.py /app/init_nats_stream.py

CMD ["python", "/app/run.py"]
