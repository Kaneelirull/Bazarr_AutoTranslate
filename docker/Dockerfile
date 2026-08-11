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
    && python -c "import autotranslate; import autotranslate.app; import autotranslate.persistence.state_store; import autotranslate.status.server"

EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
