# Setup Rápido - Bot Telegram VIP

## 🚀 Passos para Deploy no Railway

### 1. Criar Conta e Projeto
1. Acesse [Railway.app](https://railway.app)
2. Crie novo projeto
3. Adicione serviço **Valkey** (Redis)

### 2. Adicionar Código
```bash
# No seu computador:
cd telegram-bot
git init
git add .
git commit -m "Initial commit"

# No Railway:
# - Crie um novo serviço "From GitHub"
# - Conecte seu repositório
```

### 3. Configurar Variáveis de Ambiente

No **painel do Railway**, adicione estas variáveis:

```
BOT_TOKEN=SEU_TOKEN_DO_BOTFATHER
BASE_URL=https://SEU_SERVICO.up.railway.app
WEBHOOK_SECRET=qualquer_senha_forte_aleatoria
REDIS_URL=(Railway preenche automaticamente quando você adiciona Valkey)
AMPLOPAY_PUBLIC_KEY=caduresendex_rzyako90g3w6rmjx
AMPLOPAY_SECRET_KEY=v9ls0bu9p3aqc9e7w6b04bww0wsmc9xjn7dr7mcjz95z6gvh7v1yiokvq8k51gpo
```

### 4. Criar Dois Serviços

#### Serviço 1: Web (Webhook)
- Nome: `telegram-bot-web`
- Start Command: `uvicorn app.webhook:app --host 0.0.0.0 --port $PORT`
- Port: Público
- Domínio: Copie a URL (ex: `https://telegram-bot-web-production-xxxx.up.railway.app`)

#### Serviço 2: Worker (Processamento)
- Nome: `telegram-bot-worker`
- Start Command: `python -m app.worker`
- Port: Privado (não precisa de domínio público)

### 5. Adicionar Mídia

Suba os arquivos para `telegram-bot/app/media/`:

**Vídeos:**
- `video_32525.mp4` (vídeo do /start)
- `video_32495.mp4` (follow-up 2)
- `video_32497.mp4` (follow-up 3)
- `video_32501.mp4` (follow-up 4 e 7)
- `video_32503.mp4` (follow-up 5)
- `video_32508.mp4` (follow-up 6)
- `video_32521.mp4` (follow-up 8)

**Imagens:**
- `image_32528.jpg` (imagem após clicar no botão)
- `att.jpeg` (follow-up 1)

### 6. Configurar Webhook do Telegram

Use a API do Telegram para configurar o webhook:

```bash
curl -X POST "https://api.telegram.org/bot<SEU_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://SEU_SERVICO.up.railway.app/telegram/webhook",
    "secret_token": "SEU_WEBHOOK_SECRET"
  }'
```

Resposta esperada:
```json
{
  "ok": true,
  "result": true,
  "description": "Webhook was set"
}
```

### 7. Configurar Callback da Amplopay

No painel da **Amplopay**:

1. Vá em **Configurações** > **Webhooks/Callbacks**
2. Adicione a URL:
   ```
   https://SEU_SERVICO.up.railway.app/amplopay/callback
   ```
3. Salve

Agora quando um pagamento for confirmado, a Amplopay notifica automaticamente o bot.

---

## 🧪 Testar o Bot

### 1. Envie `/start` no Telegram
Deve receber:
- ✅ Vídeo com legenda promocional
- ✅ Botão "VIP COM DESCONTO por R$19,90"

### 2. Clique no Botão
Deve receber:
- ✅ Imagem do plano
- ✅ Mensagem "Você selecionou o seguinte plano..."
- ✅ Código Pix Copia e Cola (em monospace)
- ✅ Mensagem "Toque na chave PIX acima..."
- ✅ Botão "Verificar Status do Pagamento"

### 3. Aguarde 5 Minutos (sem pagar)
Deve receber:
- ✅ Imagem `att.jpeg`
- ✅ Mensagem de desconto (5% OFF)
- ✅ Novo botão com valor R$18,90

### 4. Continue Aguardando
A cada 5 minutos, recebe novo desconto até 45% OFF.

### 5. Pague o PIX
Após o pagamento:
- ✅ Clique em "Verificar Status do Pagamento"
- ✅ Deve aparecer "Pagamento confirmado!"
- ✅ Follow-ups param automaticamente

---

## 🐛 Troubleshooting

### Bot não responde ao /start

**Causa 1:** Webhook não configurado
```bash
# Verifique o webhook:
curl "https://api.telegram.org/bot<SEU_BOT_TOKEN>/getWebhookInfo"
```

Deve retornar:
```json
{
  "ok": true,
  "result": {
    "url": "https://SEU_SERVICO.up.railway.app/telegram/webhook",
    "has_custom_certificate": false,
    "pending_update_count": 0
  }
}
```

**Causa 2:** Serviço `worker` não está rodando
- Verifique logs no Railway: serviço `telegram-bot-worker`
- Deve aparecer: "Webhook configurado" ou similar

**Causa 3:** Mídia não encontrada
- Verifique se os arquivos estão em `telegram-bot/app/media/`
- Extensões suportadas: `.mp4`, `.mov`, `.jpg`, `.jpeg`, `.png`

### Follow-ups não disparam

**Causa:** Redis/Valkey desconectado
- Verifique variável `REDIS_URL` no Railway
- Deve estar preenchida automaticamente

### Pagamento não confirma

**Causa 1:** Callback da Amplopay não configurado
- Verifique no painel da Amplopay
- URL deve ser: `https://SEU_SERVICO.up.railway.app/amplopay/callback`

**Causa 2:** Credenciais erradas
- Verifique `AMPLOPAY_PUBLIC_KEY` e `AMPLOPAY_SECRET_KEY`

**Causa 3:** Ambiente de teste
- A Amplopay pode ter ambiente de sandbox
- Verifique na documentação oficial

---

## 📝 Editar Textos

Edite `telegram-bot/app/copy.py`:

```python
START_CAPTION = """Seu texto aqui"""
FOLLOWUP_1_TEXT = """Texto do primeiro follow-up"""
# ...
```

Após editar, faça commit e push (Railway redeploya automaticamente).

---

## 🔐 Segurança

- ✅ Nunca commite `.env` no Git
- ✅ Use `WEBHOOK_SECRET` forte e aleatório
- ✅ Guarde chaves da Amplopay em variáveis de ambiente
- ✅ Adicione `.gitignore` (já criado)

---

## 📊 Monitoramento

### Railway Logs
- Serviço `web`: webhook do Telegram + callback Amplopay
- Serviço `worker`: processamento de updates + follow-ups

### Redis Keys
- `tg:user:{user_id}`: dados do usuário (paid, followup_idx, chat_id)
- `tg:pix:{user_id}`: dados do pagamento PIX (transaction_id, status, code)
- `tg:campaign:due`: fila ordenada de follow-ups (sorted set)
- `tg:updates`: fila de updates do Telegram (list)

### Verificar Redis Manualmente
```bash
# No Railway, abra o shell do Redis/Valkey:
redis-cli

# Ver usuários com follow-ups pendentes:
ZRANGE tg:campaign:due 0 -1 WITHSCORES

# Ver dados de um usuário específico:
HGETALL tg:user:123456789

# Ver pagamento de um usuário:
HGETALL tg:pix:123456789
```

---

## 🎯 Próximos Passos

1. **Adicionar link do grupo VIP**: edite `bot_handlers.py`, linha com `[LINK_DO_GRUPO_VIP]`
2. **Customizar mensagens**: edite `copy.py`
3. **Adicionar analytics**: integre com Google Analytics ou Mixpanel
4. **Adicionar mais planos**: crie novos callbacks com valores diferentes
5. **Integrar com CRM**: salve dados de vendas em banco de dados

---

## 📚 Recursos

- [Documentação aiogram](https://docs.aiogram.dev/)
- [Documentação Amplopay](https://docs.amplopay.com/)
- [Railway Docs](https://docs.railway.app/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

---

**Dúvidas?** Verifique logs do Railway ou revise este guia.
