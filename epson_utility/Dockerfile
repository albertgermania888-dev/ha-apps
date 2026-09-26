FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    cups \
    printer-driver-gutenprint \
    escputil \
    python3 \
    python3-flask \
    python3-requests \
    python3-werkzeug \
    python3-zeroconf \
    imagemagick \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/Listen localhost:631/Listen 127.0.0.1:631/' /etc/cups/cupsd.conf

COPY app.py /app.py
COPY templates/ /templates/

WORKDIR /
CMD ["python3", "/app.py"]
