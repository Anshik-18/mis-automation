FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY processor ./processor
COPY db ./db
COPY config ./config
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd --system --uid 10001 mis && chown -R mis /app
USER mis
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
