FROM nginx:alpine

# Remove a página padrão do Nginx
RUN rm -rf /usr/share/nginx/html/*

# Copia todos os seus arquivos estáticos para dentro do diretório público do servidor
COPY . /usr/share/nginx/html/

# Altera as regras internas para o Nginx rodar de forma leve
RUN echo "events {} http { include /etc/nginx/mime.types; default_type application/octet-stream; server { listen 80; server_name localhost; location / { root /usr/share/nginx/html; index index.html; try_files \$uri \$uri/ /index.html; } error_page 404 /index.html; } }" > /etc/nginx/nginx.conf

# Expõe a porta de comunicação padrão
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
