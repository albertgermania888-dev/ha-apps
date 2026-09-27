FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    cups \
    escputil \
    ghostscript \
    imagemagick \
    iputils-ping \
    python3 \
    python3-flask \
    python3-requests \
    python3-werkzeug \
    python3-zeroconf \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app.py
COPY templates/ /templates/

WORKDIR /
CMD ["python3", "/app.py"]
