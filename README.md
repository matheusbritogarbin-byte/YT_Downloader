# YT Downloader - Monorepo Corporativo

Aplicações distribuídas para download de mídias do YouTube de alta velocidade, projetadas sob os pilares de **Segurança Defensiva** e **Conformidade de Dados Industrial**.

---

## 🏗️ Arquitetura do Monorepo

O projeto utiliza uma estrutura de monorepo para isolar totalmente as responsabilidades, facilitando deploys modulares na infraestrutura do **Railway**:

*   **`apps/web-frontend/`**: Interface estática e responsiva construída com Tailwind CSS v4 e preparada para acoplamento do Stripe Elements.
*   **`apps/core-backend/`**: API REST síncrona/assíncrona desenvolvida em Python + FastAPI, responsável pelo controle de acessos, cobranças recorrentes e orquestração do extrator de mídia.
*   **`infrastructure/railway/`**: Declarações de Infraestrutura como Código (IaC) para provisionamento automático de containers separados na nuvem.

---

## 🛡️ Pilares de Segurança & Blindagem (Secure by Design)

Esta aplicação lida com chaves restritas e fluxos de pagamentos de assinaturas (R$ 4,99), sendo totalmente blindada contra as vulnerabilidades críticas mais comuns (OWASP Top 10):

1.  **Conformidade Estrita PCI-DSS (Escopo SAQ-A)**: O ecossistema foi desenhado para **Zero Armazenamento de Dados de Cartão**. O backend manipula apenas IDs de referência criptografados gerados pelo Stripe.
2.  **Proteção contra Execução Remota de Código (Anti-RCE)**: A extração de links via `yt-dlp` é executada de forma nativa pela biblioteca Python, isolando o interpretador e bloqueando qualquer injeção de comandos via Shell (`os.system`/`subprocess`).
3.  **Algoritmo Criptográfico Ouro (Argon2id)**: Para o armazenamento seguro de hashes de senhas, o sistema substitui o bcrypt convencional pelo Argon2id (Recomendação oficial da OWASP), elevando exponencialmente a barreira contra ataques massivos de força bruta por hardware dedicada (GPUs/ASICs).
4.  **Barreira Anti-DDoS e Abuso (Rate Limiting)**: Um middleware interceptador de tráfego injetado na API bloqueia automaticamente requisições automatizadas ou bots que estourem o limite estipulado por IP.
5.  **Contêineres de Privilégio Mínimo**: Os Dockerfiles utilizam imagens reduzidas (Alpine/Slim) e rodam sob usuários do sistema sem privilégios administrativos (`non-root`), mitigando ataques de quebra de contêiner (*container breakout*).

---

## 🚀 Como Executar o Projeto Localmente

### 🎛️ Pré-requisitos
*   Python 3.11 ou superior instalado.
*   Node.js 18 ou superior instalado.

### 🐍 Configurando o Backend (API)
1. Acesse o diretório correspondente:
   ```bash
   cd apps/core-backend
   ```
2. Crie e ative o seu ambiente virtual Python:
   ```bash
   python -m venv .venv
   # No Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   ```
3. Instale as dependências travadas:
   ```bash
   pip install -r requirements.txt
   ```
4. Crie o seu arquivo `.env` com base no arquivo `.env.example` e preencha as chaves secretas.
5. Inicie o servidor em modo de desenvolvimento:
   ```bash
   python -m uvicorn app.main:app --reload --port 8000
   ```

### 🎨 Configurando o Frontend (Site)
1. Acesse o diretório correspondente:
   ```bash
   cd ../web-frontend
   ```
2. Crie o seu arquivo `.env` com base no `.env.example` (Mantenha apenas chaves públicas `pk_...`).
3. Inicialize o servidor estático local:
   ```bash
   npx serve -s . -l 3000
   ```

---

## 🤖 Pipeline de Integração Contínua (CI/CD)

O diretório `.github/workflows/` contém regras automatizadas que rodam a cada `git push` na ramificação `main`:
*   **TruffleHog**: Varredura profunda do repositório para impedir o vazamento acidental de chaves privadas ou tokens no histórico do Git.
*   **Trivy Scan**: Auditoria estática de segurança nas dependências à procura de vulnerabilidades públicas conhecidas (CVEs).
