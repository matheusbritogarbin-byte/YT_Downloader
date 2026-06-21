FROM nginx:alpine

# Remove arquivos padrao do Nginx
RUN rm -rf /usr/share/nginx/html/*

# Copia os arquivos locais para a pasta publica do container
COPY . /usr/share/nginx/html/

# Expõe a porta padrao de containers web estaticos
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
