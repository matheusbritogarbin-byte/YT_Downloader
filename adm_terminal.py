import sys
import os
import requests

# CONFIGURAÇÃO DE SEGURANÇA MESTRA
API_BASE_URL = "https://backend-production-5a6c0.up.railway.app/"
ADMIN_SECRET_TOKEN = "@Matheus07052008"


def limpar_tela():
    os.system("cls" if os.name == "nt" else "clear")


def menu_principal():
    limpar_tela()
    print("==================================================")
    print("      CENTRO DE COMANDO ADMINISTRATIVO - SAAS     ")
    print("==================================================")
    print("[1] Listar todos os Tokens Premium Ativos no Redis")
    print("[2] Resetar Cota Gratuita de um IP Específico")
    print("[3] Adicionar Novo Token Premium Manualmente")
    print("[4] Checar Status e PCI Compliance do Servidor")
    print("[5] Sair")
    print("==================================================")


def listar_tokens():
    print("\n[+] Consultando banco de dados Redis remoto...")
    headers = {"X-Admin-Token": ADMIN_SECRET_TOKEN}
    try:
        resp = requests.get(f"{API_BASE_URL}/admin/tokens", headers=headers, timeout=15)
        if resp.status_code == 200:
            tokens = resp.json().get("tokens", [])
            print(f"\n✅ Total de tokens premium ativos: {len(tokens)}")
            for t in tokens:
                print(f" ➔ {t}")
        else:
            print(f"❌ Acesso negado pelo Railway: {resp.status_code}")
    except Exception as e:
        print(f"❌ Erro de conexao: {str(e)}")
    input("\nPressione Enter para voltar ao menu...")


def resetar_cota():
    ip_alvo = input(
        "\nDigite o IP do cliente para resetar a cota (ex: 187.xx.xx.xx): "
    ).strip()
    if not ip_alvo:
        return
    headers = {"X-Admin-Token": ADMIN_SECRET_TOKEN}
    try:
        resp = requests.post(
            f"{API_BASE_URL}/admin/reset-quota",
            json={"ip": ip_alvo},
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"✅ Cota do IP {ip_alvo} limpa e resetada com sucesso no Redis!")
        else:
            print(f"❌ Erro na operacao: {resp.status_code}")
    except Exception as e:
        print(f"❌ Erro de conexao: {str(e)}")
    input("\nPressione Enter para voltar ao menu...")


def main():
    while True:
        menu_principal()
        opcao = input("Escolha uma operacao: ").strip()
        if opcao == "1":
            listar_tokens()
        elif opcao == "2":
            resetar_cota()
        elif opcao == "3":
            print("\nFuncionalidade em desenvolvimento...")
            input("\nEnter para voltar...")
        elif opcao == "4":
            try:
                resp = requests.get("https://backend-production-5a6c0.up.railway.app/")
                print(f"\nStatus: {resp.json()}")
            except Exception as e:
                print(f"Erro: {e}")
            input("\nEnter para voltar...")
        elif opcao == "5":
            print("\nFechando terminal de controle de segurança. Ate logo!")
            sys.exit(0)


if __name__ == "__main__":
    main()
