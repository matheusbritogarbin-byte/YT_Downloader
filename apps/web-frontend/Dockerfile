# Estágio único usando Alpine Linux para garantir um contêiner leve e seguro
FROM node:18-alpine

WORKDIR /app

# Instala um servidor estático seguro de forma global
RUN npm install -g serve

# Copia os arquivos do frontend para dentro do contêiner
COPY . .

# Expõe a porta padrão do servidor web do frontend
EXPOSE 3000

# Inicializa o servidor apontando para os arquivos estáticos na porta 3000
CMD ["serve", "-s", ".", "-l", "3000"]
