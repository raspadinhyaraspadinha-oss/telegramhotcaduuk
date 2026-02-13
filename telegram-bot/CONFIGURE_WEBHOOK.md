# 🔧 Como Configurar o Webhook do Telegram

O webhook é necessário para o bot receber mensagens. Sem ele, o bot não responde.

## ⚡ Opção 1: Script Python (Recomendado)

### 1. Configure as variáveis de ambiente

No seu terminal (ou no Railway, configure como variáveis de ambiente):

```bash
export BOT_TOKEN=seu_token_aqui
export BASE_URL=https://telegram-webhook-production-e13d.up.railway.app
export WEBHOOK_SECRET=sua_senha_secreta
```

### 2. Instale httpx (se não tiver)

```bash
pip install httpx
```

### 3. Execute o script

```bash
cd telegram-bot
python setup_webhook.py
```

Você verá algo assim:
```
🔧 Configurando webhook do Telegram...
   Bot Token: 1234567890...xyz
   Webhook URL: https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook
   Secret Token: chang...e-me

🗑️  Removendo webhook antigo...
   ✅ Webhook antigo removido

📡 Configurando novo webhook...
   ✅ Webhook configurado com sucesso!

🔍 Verificando webhook...
   ✅ Status do webhook:
      URL: https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook
      Pending updates: 0
      Max connections: 40

✅ Configuração concluída!
```

---

## ⚡ Opção 2: cURL (Terminal)

Se preferir usar cURL direto no terminal:

### Windows (PowerShell):

```powershell
$BOT_TOKEN = "SEU_TOKEN_AQUI"
$WEBHOOK_URL = "https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook"
$SECRET = "sua_senha_secreta"

$body = @{
    url = $WEBHOOK_URL
    secret_token = $SECRET
    drop_pending_updates = $false
    allowed_updates = @("message", "callback_query")
} | ConvertTo-Json

Invoke-RestMethod -Uri "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" -Method Post -Body $body -ContentType "application/json"
```

### Mac/Linux:

```bash
BOT_TOKEN="SEU_TOKEN_AQUI"
WEBHOOK_URL="https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook"
SECRET="sua_senha_secreta"

curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"$WEBHOOK_URL\",
    \"secret_token\": \"$SECRET\",
    \"drop_pending_updates\": false,
    \"allowed_updates\": [\"message\", \"callback_query\"]
  }"
```

Resposta esperada:
```json
{
  "ok": true,
  "result": true,
  "description": "Webhook was set"
}
```

---

## ⚡ Opção 3: Navegador (Rápido e Simples)

Abra esta URL no navegador (substitua `SEU_TOKEN_AQUI` pelo token do seu bot):

```
https://api.telegram.org/botSEU_TOKEN_AQUI/setWebhook?url=https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook&secret_token=sua_senha_secreta&allowed_updates=["message","callback_query"]
```

Exemplo real (substitua os valores):
```
https://api.telegram.org/bot1234567890:ABCdefGHIjklMNOpqrsTUVwxyz/setWebhook?url=https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook&secret_token=minha_senha_123&allowed_updates=["message","callback_query"]
```

---

## 🔍 Verificar se o Webhook Está Configurado

### Via navegador:

```
https://api.telegram.org/botSEU_TOKEN_AQUI/getWebhookInfo
```

### Via cURL:

```bash
curl "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"
```

### Via script Python:

```bash
cd telegram-bot
python -c "
import os, httpx
token = os.getenv('BOT_TOKEN')
resp = httpx.get(f'https://api.telegram.org/bot{token}/getWebhookInfo')
print(resp.json())
"
```

Resposta esperada:
```json
{
  "ok": true,
  "result": {
    "url": "https://telegram-webhook-production-e13d.up.railway.app/telegram/webhook",
    "has_custom_certificate": false,
    "pending_update_count": 0,
    "max_connections": 40
  }
}
```

---

## 🚨 Troubleshooting

### ❌ Erro: "Webhook URL seems to be incorrect"

**Causa:** URL do webhook não está acessível ou retorna erro.

**Solução:**
1. Verifique se o serviço `web` está rodando no Railway
2. Acesse `https://telegram-webhook-production-e13d.up.railway.app/` no navegador
3. Deve retornar: `{"ok": true, "service": "telegram-webhook", ...}`
4. Se não retornar nada, o serviço não está rodando

### ❌ Erro: "Unauthorized"

**Causa:** Token do bot está errado.

**Solução:**
1. Verifique o token no BotFather do Telegram
2. Verifique a variável `BOT_TOKEN` no Railway
3. Token deve ter formato: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`

### ❌ Bot responde "Recebido ✅" ao /start

**Causa:** Webhook não está configurado OU worker não está rodando.

**Solução:**

1. **Configure o webhook** (siga os passos acima)

2. **Verifique se o worker está rodando:**
   - No Railway, vá em "Deployments" → serviço `worker`
   - Deve estar com status "Active"
   - Veja os logs: deve aparecer algo como "Webhook configurado" ou "Bot iniciado"

3. **Verifique se o Redis está conectado:**
   - Acesse `https://telegram-webhook-production-e13d.up.railway.app/debug` no navegador
   - Deve mostrar: `"redis_status": "✅ Conectado"`

4. **Verifique variáveis de ambiente no Railway:**
   ```
   BOT_TOKEN=...
   BASE_URL=https://telegram-webhook-production-e13d.up.railway.app
   WEBHOOK_SECRET=...
   REDIS_URL=... (ou VALKEY_URL)
   ```

### ❌ Webhook configurado mas bot não responde

**Causa:** Worker não está processando a fila.

**Solução:**
1. Verifique logs do serviço `worker` no Railway
2. Procure por erros como:
   - `RuntimeError: BOT_TOKEN não definido`
   - `RuntimeError: REDIS_URL não definido`
   - `Connection refused` (Redis não conectado)

3. Se o worker não está rodando, redeploy o serviço:
   - Railway → serviço `worker` → "Redeploy"

---

## ✅ Checklist Final

Antes de testar o bot, verifique:

- [ ] Webhook configurado (URL retorna `"ok": true`)
- [ ] Serviço `web` rodando no Railway (status "Active")
- [ ] Serviço `worker` rodando no Railway (status "Active")
- [ ] Redis/Valkey conectado (veja `/debug`)
- [ ] Variáveis de ambiente configuradas:
  - [ ] `BOT_TOKEN`
  - [ ] `BASE_URL`
  - [ ] `WEBHOOK_SECRET`
  - [ ] `REDIS_URL` ou `VALKEY_URL`
- [ ] Mídia adicionada em `telegram-bot/app/media/`

---

## 🧪 Testar o Bot

1. Abra o Telegram
2. Procure pelo seu bot (ex: `@seu_bot`)
3. Envie `/start`
4. Deve receber:
   - ✅ Vídeo com legenda
   - ✅ Botão "VIP COM DESCONTO"

Se receber apenas "Recebido ✅", volte ao troubleshooting acima.

---

## 📚 Recursos Úteis

- [Telegram Bot API - setWebhook](https://core.telegram.org/bots/api#setwebhook)
- [Telegram Bot API - getWebhookInfo](https://core.telegram.org/bots/api#getwebhookinfo)
- [Railway Docs](https://docs.railway.app/)
