# ── Build stage ───────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

# better-sqlite3 requires native compilation
RUN apk add --no-cache python3 make g++

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM node:20-alpine

WORKDIR /app

# Copy node_modules from builder (avoids reinstalling build tools in runtime)
COPY --from=builder /app/node_modules ./node_modules
COPY package.json server.js ./

# SQLite data directory — mount Railway volume here
RUN mkdir -p /data

EXPOSE 3000
ENV NODE_ENV=production

CMD ["node", "server.js"]
