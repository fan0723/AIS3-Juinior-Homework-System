FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt

COPY aams ./aams
COPY run.py manage.py ./

RUN useradd --uid 10001 --create-home aams && mkdir -p /data/spool /spool && chown -R aams:aams /data /spool
USER aams

ENV AAMS_DATA_DIR=/data AAMS_HOST=0.0.0.0
EXPOSE 8000 8001
CMD ["python", "run.py"]
