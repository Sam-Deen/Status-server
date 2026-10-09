FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --system --gid 10001 status && useradd --system --uid 10001 --gid status --home /app status \
    && mkdir /data && chown status:status /data
COPY --chown=status:status app.py /app/app.py
COPY --chown=status:status health_contract.py /app/health_contract.py
COPY --chown=status:status status_server /app/status_server
COPY --chown=status:status static /app/static
USER status
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1
CMD ["python", "/app/app.py"]
