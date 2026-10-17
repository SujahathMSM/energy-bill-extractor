# Runs on amd64 and arm64 (e.g. a Raspberry Pi 5): all dependencies ship wheels for both.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY bill_extractor ./bill_extractor
RUN pip install . && useradd --create-home --uid 10001 app

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "bill_extractor.api:app", "--host", "0.0.0.0", "--port", "8000"]
