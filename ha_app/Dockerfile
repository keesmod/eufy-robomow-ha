# Home Assistant app candidate for the Eufy mower bridge. Built locally by the Supervisor from
# this directory; the bridge sources are staged here by scripts/prepare_ha_app.py.
# Same pinned Node 24 base as bridge/Dockerfile. The Supervisor's BUILD_FROM argument is ignored
# on purpose so the runtime stays the pinned official image.
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
WORKDIR /app
COPY package.json package-lock.json tsconfig.json ./
RUN npm ci --ignore-scripts
COPY src ./src
RUN npm run build && npm prune --omit=dev --ignore-scripts

FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
ENV NODE_ENV=production \
    EUFY_MOWER_OPTIONS_FILE=/data/options.json \
    EUFY_MOWER_BIND_ADDRESS=0.0.0.0 \
    EUFY_MOWER_PORT=8090 \
    EUFY_MOWER_DATA_DIR=/data/eufy-mower
WORKDIR /app
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY bootstrap.mjs ./
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["node", "dist/healthcheck.js"]
CMD ["node", "bootstrap.mjs"]
