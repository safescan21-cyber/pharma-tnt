FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TNT_DB=/data/tnt.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 1000 --create-home tnt && mkdir /data && chown tnt /data
COPY tnt ./tnt
USER tnt
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
# Single worker by design: one serialized writer keeps the audit hash chain ordered.
CMD ["python", "-m", "tnt", "serve", "--host", "0.0.0.0", "--port", "8000"]
