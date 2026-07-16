# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS dependencies

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /workspace/apps/web

# The web app currently consumes the local contracts package through a file:
# dependency, so that package must exist before npm resolves dependencies.
COPY packages/contracts /workspace/packages/contracts
COPY apps/web/package*.json ./

RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM dependencies AS build

COPY apps/web ./

# NEXT_PUBLIC_* values are bundled into the browser build. They must be a
# browser-reachable API URL and must never contain server secrets.
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS
ARG MONITUBE_WEB_API_PROXY_TARGET
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL} \
    NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS=${NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS} \
    MONITUBE_WEB_API_PROXY_TARGET=${MONITUBE_WEB_API_PROXY_TARGET}

RUN npm run build

FROM build AS production

ENV NODE_ENV=production

EXPOSE 3000

CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
