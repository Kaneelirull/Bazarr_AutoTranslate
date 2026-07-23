FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY Bazarr_AutoTranslate.py .
COPY clean_et_subs.py .
COPY status_dashboard.py .
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh clean_et_subs.py \
    && mkdir -p /var/log/bazarr-autotranslate /config

EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
