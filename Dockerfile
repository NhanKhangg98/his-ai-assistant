FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY app ./app
COPY sample_data ./sample_data
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY tests ./tests
COPY story_workflow.http ./story_workflow.http

RUN pip install --no-cache-dir -e ".[dev]"

EXPOSE 8000

CMD ["sh", "-c", "python -m sample_data.init_sample_repo && alembic upgrade head && his-kb ingest && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
