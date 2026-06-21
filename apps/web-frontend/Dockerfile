FROM nginx:alpine

RUN rm -rf /usr/share/nginx/html/*

COPY . /usr/share/nginx/html/

# Configuração robusta com try_files para aceitar URLs sem a extensão .html de forma transparente
RUN echo "events {} http { include /etc/nginx/mime.types; default_type application/octet-stream; server { listen 80; server_name localhost; location / { root /usr/share/nginx/html; index index.html; try_files \$uri \$uri.html \$uri/ /index.html; } error_page 404 /index.html; } }" > /etc/nginx/nginx.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
