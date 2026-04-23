ARG DOCKER_REGISTRY=docker.io/
FROM ${DOCKER_REGISTRY}python:3.13-slim

LABEL maintainer="OpenNode LLC <info@opennodecloud.com>"
LABEL org.opencontainers.image.source="https://code.opennodecloud.com/waldur/rancher-keycloak-operator"
LABEL org.opencontainers.image.description="Kubernetes operator for managing Rancher projects and Keycloak groups via CRDs"
LABEL org.opencontainers.image.licenses="MIT"

ARG COMMIT_INFO=""
ARG VERSION=""

ENV COMMIT_INFO=${COMMIT_INFO}
ENV VERSION=${VERSION}

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project 2>/dev/null || uv sync --no-dev --no-install-project

# Install the application
COPY rancher_keycloak_operator/ rancher_keycloak_operator/
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

USER nobody

ENTRYPOINT ["uv", "run", "kopf", "run", "--module", "rancher_keycloak_operator.operator", "--namespace=waldur-system"]
