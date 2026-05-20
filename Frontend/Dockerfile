FROM node:20-alpine AS builder
WORKDIR /app

COPY . .
RUN rm -rf node_modules package-lock.json
RUN npm install

# This makes your frontend call the backend directly
ENV VITE_API_BASE_URL=https://backend-api-127510819679.europe-west1.run.app/api
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html

# Simple nginx config - no proxy needed
RUN echo 'server { \
    listen 8080; \
    server_name _; \
    \
    location / { \
        root /usr/share/nginx/html; \
        index index.html; \
        try_files $uri $uri/ /index.html; \
    } \
}' > /etc/nginx/conf.d/default.conf

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]