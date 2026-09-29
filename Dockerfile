FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# --timeout must stay above the LONGEST portal-side wait (STRAIN_TIMEOUT and
# MICRO_TIMEOUT default to 600) or gunicorn kills the worker mid-analysis and the
# user gets a dropped connection instead of the service's own error. Threads keep
# one long upload from blocking everyone.
CMD ["sh", "-c", "python manage.py migrate && python manage.py collectstatic --noinput && gunicorn config.wsgi -b 0.0.0.0:8000 --workers 3 --threads 4 --timeout ${GUNICORN_TIMEOUT:-660}"]
