FROM python:3.13-slim
WORKDIR /app
COPY localcloud /app/localcloud
RUN mkdir -p /data && chown 10001:10001 /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 4566
VOLUME ["/data"]
ENTRYPOINT ["python3", "-m", "localcloud"]
CMD ["--host", "0.0.0.0", "--data", "/data/state.sqlite3"]
