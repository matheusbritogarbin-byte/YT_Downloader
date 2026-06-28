#!/usr/bin/env python3
"""
Envia cookies.txt para o Railway e ativa por 30 dias.
Uso: python update_cookies.py [caminho/para/cookies.txt]
Se nenhum caminho for fornecido, usa 'cookies.txt' na raiz.
"""

import os
import sys
import requests
from pathlib import Path

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

RAILWAY_URL = os.getenv("RAILWAY_URL", "https://yt-downloader-max.up.railway.app")
ADMIN_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "@Matheus07052008")
DEFAULT_FILE = "cookies.txt"


def main():
    if len(sys.argv) > 1:
        cookies_path = Path(sys.argv[1])
    else:
        cookies_path = Path(__file__).parent / DEFAULT_FILE

    if not cookies_path.exists():
        print(f"❌ Arquivo não encontrado: {cookies_path}")
        print(f"💡 Exporte os cookies do YouTube e salve como '{DEFAULT_FILE}' na raiz")
        sys.exit(1)

    cookies_text = cookies_path.read_text(encoding="utf-8")
    headers = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}
    url = f"{RAILWAY_URL}/api/v1/admin/cookies/save"
    payload = {"cookies_text": cookies_text}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            print("✅ Cookies enviados! Válidos por 30 dias.")
        elif resp.status_code == 401:
            print("❌ Token inválido. Verifique ADMIN_SECRET_TOKEN")
            sys.exit(1)
        else:
            print(f"❌ Erro {resp.status_code}: {resp.text}")
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"❌ Não foi possível conectar a {RAILWAY_URL}")
        sys.exit(1)


if __name__ == "__main__":
    main()
