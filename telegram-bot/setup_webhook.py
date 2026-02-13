#!/usr/bin/env python3
"""
Script para configurar o webhook do Telegram.
Uso: python setup_webhook.py
"""
import os
import sys

import httpx


def setup_webhook():
    bot_token = os.getenv("BOT_TOKEN")
    base_url = os.getenv("BASE_URL")
    webhook_secret = os.getenv("WEBHOOK_SECRET", "change-me")

    if not bot_token:
        print("❌ BOT_TOKEN não definido. Configure a variável de ambiente.")
        sys.exit(1)

    if not base_url:
        print("❌ BASE_URL não definido. Configure a variável de ambiente.")
        print("Exemplo: export BASE_URL=https://telegram-webhook-production-e13d.up.railway.app")
        sys.exit(1)

    webhook_url = f"{base_url}/telegram/webhook"

    print(f"🔧 Configurando webhook do Telegram...")
    print(f"   Bot Token: {bot_token[:10]}...{bot_token[-4:]}")
    print(f"   Webhook URL: {webhook_url}")
    print(f"   Secret Token: {webhook_secret[:5]}...{webhook_secret[-3:]}")
    print()

    # Primeiro, remove webhook antigo (se existir)
    print("🗑️  Removendo webhook antigo...")
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10.0,
        )
        if resp.status_code == 200:
            print("   ✅ Webhook antigo removido")
        else:
            print(f"   ⚠️  Resposta: {resp.text}")
    except Exception as e:
        print(f"   ⚠️  Erro ao remover webhook: {e}")

    print()

    # Configura novo webhook
    print("📡 Configurando novo webhook...")
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": webhook_secret,
                "drop_pending_updates": False,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=10.0,
        )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                print("   ✅ Webhook configurado com sucesso!")
                print()
                print("📊 Informações do webhook:")
                print(f"   URL: {webhook_url}")
                print(f"   Secret Token: configurado")
                print(f"   Allowed Updates: message, callback_query")
            else:
                print(f"   ❌ Erro: {data.get('description', 'Erro desconhecido')}")
                sys.exit(1)
        else:
            print(f"   ❌ Código HTTP: {resp.status_code}")
            print(f"   Resposta: {resp.text}")
            sys.exit(1)
    except Exception as e:
        print(f"   ❌ Erro: {e}")
        sys.exit(1)

    print()

    # Verifica webhook configurado
    print("🔍 Verificando webhook...")
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                result = data.get("result", {})
                print("   ✅ Status do webhook:")
                print(f"      URL: {result.get('url', 'N/A')}")
                print(f"      Pending updates: {result.get('pending_update_count', 0)}")
                print(f"      Max connections: {result.get('max_connections', 40)}")
                print(f"      IP: {result.get('ip_address', 'N/A')}")
                
                last_error = result.get("last_error_message")
                if last_error:
                    print(f"      ⚠️ Último erro: {last_error}")
                    print(f"         Data: {result.get('last_error_date', 'N/A')}")
            else:
                print(f"   ❌ Erro: {data.get('description', 'Erro desconhecido')}")
        else:
            print(f"   ❌ Código HTTP: {resp.status_code}")
    except Exception as e:
        print(f"   ❌ Erro: {e}")

    print()
    print("✅ Configuração concluída!")
    print()
    print("📝 Próximos passos:")
    print("   1. Teste enviando /start para o bot no Telegram")
    print("   2. Se não funcionar, verifique os logs do Railway (serviço 'worker')")
    print("   3. Verifique se o Redis/Valkey está conectado")


if __name__ == "__main__":
    setup_webhook()
