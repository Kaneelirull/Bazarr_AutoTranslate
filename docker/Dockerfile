FROM python:3.11-slim

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY Bazarr_AutoTranslate.py .
COPY clean_et_subs.py .
COPY entrypoint.sh .

# Make scripts executable
RUN chmod +x entrypoint.sh clean_et_subs.py

# Create log directory
RUN mkdir -p /var/log/bazarr-autotranslate

ENTRYPOINT ["/app/entrypoint.sh"]
