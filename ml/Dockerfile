FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install backend package with ML extras (sentence-transformers, scikit-learn)
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/app ./app
RUN pip install --no-cache-dir -e ".[ml]"

# Copy the ml service package
COPY ml ./ml

ENV PYTHONPATH=/app

EXPOSE 8001
CMD ["uvicorn", "ml.service.main:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "info"]
