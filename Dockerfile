FROM python:3.13-slim
WORKDIR /app
COPY localcloud /app/localcloud
COPY bin/localcloud /app/bin/localcloud
RUN mkdir -p /data && chown 10001:10001 /data \
    && chmod 0755 /app/bin/localcloud \
    && ln -s /app/bin/localcloud /usr/local/bin/localcloud
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 4566
VOLUME ["/data"]
ENTRYPOINT ["python3", "-m", "localcloud"]
CMD ["--host", "0.0.0.0", "--data", "/data/state.sqlite3"]
