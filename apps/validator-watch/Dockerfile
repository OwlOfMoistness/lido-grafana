FROM python:3.13-alpine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY probe.py /app/probe.py
COPY watch /app/watch
COPY grafana /app/grafana
RUN mkdir /data /provisioned && chown 65532:65532 /data /provisioned
USER 65532:65532
ENTRYPOINT ["python", "-m", "watch.main"]
CMD ["serve"]
