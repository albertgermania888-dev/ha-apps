FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    printer-driver-gutenprint \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

RUN pip install flask requests

COPY app.py /app.py
COPY templates/ /templates/

WORKDIR /
CMD ["python3", "app.py"]
