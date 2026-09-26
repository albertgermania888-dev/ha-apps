FROM debian:bookworm-slim

# Устанавливаем системный Python, нужные библиотеки, драйверы и netcat
RUN apt-get update && apt-get install -y \
    python3 \
    python3-flask \
    python3-requests \
    printer-driver-gutenprint \
    escputil \
    netcat-openbsd \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app.py
COPY templates/ /templates/

WORKDIR /
CMD ["python3", "/app.py"]
