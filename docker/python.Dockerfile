FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 POETRY_VIRTUALENVS_CREATE=false PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev libssl-dev ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir poetry==2.3.4
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN --mount=type=cache,target=/root/.cache/pypoetry POETRY_REQUESTS_TIMEOUT=120 POETRY_INSTALLER_PARALLEL=false poetry install --only main --no-root --no-interaction --no-ansi
COPY . .
RUN useradd --create-home --uid 1000 ace && chown -R ace:ace /app
USER ace
CMD ["python", "-m", "server.main"]
