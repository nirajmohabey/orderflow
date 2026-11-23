FROM node:26.13.1-slim AS dashboard
WORKDIR /build
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.12.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade \
    apt ca-certificates libapt-pkg7.0 libc-bin libc6 liblzma5 libpcre2-8-0 \
    libssl3t64 openssl openssl-provider-legacy zlib1g \
    && mkdir -p /usr/local/share/orderflow \
    && dpkg-query -W > /usr/local/share/orderflow/os-packages.txt \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY . .
COPY --from=dashboard /build/dist ./dashboard/dist
RUN useradd --uid 10001 --create-home app && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["python","-m","uvicorn","orderflow.api:app","--host","0.0.0.0","--port","8000","--no-access-log"]

