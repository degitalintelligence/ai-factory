FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN groupadd -g 10001 factory && useradd -u 10001 -g factory -M factory && mkdir /workspaces && chown factory:factory /workspaces

FROM base AS control
COPY app ./app
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS sandbox
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap util-linux libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY app/__init__.py app/config.py app/security.py app/schemas.py app/version.py ./app/
COPY sandbox ./sandbox
USER 10001:10001
EXPOSE 8090
CMD ["uvicorn", "sandbox.server:app", "--host", "0.0.0.0", "--port", "8090"]
