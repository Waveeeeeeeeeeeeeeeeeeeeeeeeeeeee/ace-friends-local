FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir PyYAML==6.0.2
COPY bootstrap.py /opt/ace/bootstrap.py
WORKDIR /workspace
CMD ["python", "/opt/ace/bootstrap.py"]
