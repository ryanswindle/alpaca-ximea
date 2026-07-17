FROM python:3.12-slim

LABEL maintainer="Ryan Swindle <rswindle@gmail.com>"
LABEL description="ASCOM Alpaca server for Ximea cameras (xiAPI)"

# Install minimal system dependencies for lib
RUN apt-get update && apt-get install -y \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /alpyca

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY config.yaml .
COPY src ./src

CMD ["python", "src/main.py"]
