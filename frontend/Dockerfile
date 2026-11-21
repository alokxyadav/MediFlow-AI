# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------
# MediFlow AI frontend - builds the Vite/React app and serves the static
# output with nginx. VITE_API_BASE_URL must be supplied as a build arg
# since Vite bakes env vars into the bundle at build time (see
# docker-compose.yml for the local-stack example).
# ---------------------------------------------------------------------

FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL=http://localhost:8000/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
