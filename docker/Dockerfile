FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY autotranslate ./autotranslate
COPY static ./static
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh clean_et_subs.py \
    && mkdir -p /var/log/bazarr-autotranslate /config \
    && BAZARR_URL=http://bazarr:6767 BAZARR_API_KEY=build-smoke \
       LINGARR_URL=http://lingarr:8080 \
       python -c "import autotranslate, media_identity, state_store, status_dashboard, Bazarr_AutoTranslate"

EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
