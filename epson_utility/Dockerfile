FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    printer-driver-gutenprint \
    iputils-ping \
    python3-flask \
    python3-requests \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app.py
COPY templates/ /templates/

WORKDIR /
CMD ["python3", "/app.py"]
