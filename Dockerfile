FROM alpine/git@sha256:2306e2b05ee0ef99f5d25b0779f645b24ae9f91ba187107503592fcdfe78da3a AS upstream
RUN git init /sqlglot && git -C /sqlglot fetch --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && git -C /sqlglot checkout --detach FETCH_HEAD && rm -rf /sqlglot/.git

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS maps
RUN pip install --no-cache-dir coverage==7.17.4 pytest==9.2.0 duckdb==2.0.0 \
    numpy==2.5.5 pandas==3.1.1 pytz==2026.6 setuptools==84.2.1
COPY --from=upstream /sqlglot /sqlglot
COPY build_map.py /build_map.py
WORKDIR /sqlglot
RUN SKIP_INTEGRATION=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python /build_map.py /dependencies.json

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS test-runtime
RUN pip install --no-cache-dir pytest==9.2.0 duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 pytz==2026.6 setuptools==84.2.1
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1

FROM test-runtime AS observations
COPY --from=upstream /sqlglot /sqlglot
COPY observe.py /observe.py
WORKDIR /sqlglot
RUN python /observe.py /classes.json

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY dependencies.json classes.json /app/
COPY --from=upstream /sqlglot /app/source
COPY selector.py /app/
ENV PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
ENTRYPOINT ["python", "/app/selector.py"]
