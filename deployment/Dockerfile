FROM python:3.14-alpine
RUN apk add --no-cache openssh-client
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY deployment/control.py deployment/inventory.py /app/
COPY monitoring/dashboards/lido-validator-fleet.json /app/dashboard.json
ENTRYPOINT ["python", "/app/control.py"]
