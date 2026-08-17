FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-dev.txt /app/requirements-dev.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements-dev.txt

COPY README.md /app/README.md
COPY pyproject.toml /app/pyproject.toml
COPY shared /app/shared
COPY services /app/services
COPY contracts /app/contracts

EXPOSE 8003

CMD ["uvicorn", "services.document_ai.app.main:app", "--host", "0.0.0.0", "--port", "8003"]
