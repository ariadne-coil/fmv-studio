FROM node:22-bookworm-slim AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-bookworm-slim AS builder

WORKDIR /app

ARG NEXT_PUBLIC_LIVE_DIRECTOR_WS_ORIGIN=""
ARG NEXT_PUBLIC_GCP_PROJECT_ID=""
ARG NEXT_PUBLIC_VERTEX_MEDIA_LOCATION="us-central1"
ARG NEXT_PUBLIC_LIVE_DIRECTOR_MODEL="gemini-live-2.5-flash-native-audio"

ENV NEXT_PUBLIC_LIVE_DIRECTOR_WS_ORIGIN=${NEXT_PUBLIC_LIVE_DIRECTOR_WS_ORIGIN} \
    NEXT_PUBLIC_GCP_PROJECT_ID=${NEXT_PUBLIC_GCP_PROJECT_ID} \
    NEXT_PUBLIC_VERTEX_MEDIA_LOCATION=${NEXT_PUBLIC_VERTEX_MEDIA_LOCATION} \
    NEXT_PUBLIC_LIVE_DIRECTOR_MODEL=${NEXT_PUBLIC_LIVE_DIRECTOR_MODEL}

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-bookworm-slim AS runner

WORKDIR /app
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=8080

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 8080

CMD ["node", "server.js"]
