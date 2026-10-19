FROM node:22-slim
WORKDIR /app
COPY package.json ./
COPY src ./src
USER 10001:10001
CMD ["node", "src/process.mjs"]
