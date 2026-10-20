ARG DATING_IMAGE=dating
FROM postgres:16-bookworm AS pgtools
FROM ${DATING_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client liblz4-1 libzstd1 && rm -rf /var/lib/apt/lists/*
COPY --from=pgtools /usr/lib/postgresql/16/bin/psql /usr/local/bin/
COPY --from=pgtools /usr/lib/*-linux-gnu/libpq.so.5* /usr/local/lib/
RUN ldconfig && psql --version
RUN pip install --no-cache-dir bcrypt==4.3.0
WORKDIR /workspace
COPY seed.py verify.py /opt/ace/
CMD ["python", "/opt/ace/seed.py", "apply"]
