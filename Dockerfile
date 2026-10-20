FROM docker:28-cli AS docker_cli
FROM python:3.13-slim
WORKDIR /app
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY localcloud /app/localcloud
COPY bin/localcloud /app/bin/localcloud
RUN groupadd --gid 10001 localcloud \
    && useradd --uid 10001 --gid localcloud --home-dir /data --no-create-home localcloud \
    && mkdir -p /data && chown 10001:10001 /data \
    && chmod 0755 /app/bin/localcloud \
    && ln -s /app/bin/localcloud /usr/local/bin/localcloud
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 4566
VOLUME ["/data"]
ENTRYPOINT ["python3", "-m", "localcloud"]
CMD ["--host", "0.0.0.0", "--data", "/data/state.sqlite3"]
