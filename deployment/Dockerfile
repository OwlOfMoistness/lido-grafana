FROM python:3.13-alpine
RUN apk add --no-cache openssh-client
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY deployment/control.py /app/control.py
COPY monitoring/dashboards/lido-validator-fleet.json /app/dashboard.json
ENTRYPOINT ["python", "/app/control.py"]
