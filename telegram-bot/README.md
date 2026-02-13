# Telegram Bot - Sistema de Vendas VIP com Follow-ups Automáticos

Bot de Telegram com sistema de vendas via PIX (Amplopay), follow-ups automáticos com descontos crescentes a cada 5 minutos, e gerenciamento de campanhas via Redis/Valkey.

## Estrutura

```
telegram-bot/
├── app/
│   ├── __init__.py
│   ├── config.py              # Configurações e variáveis de ambiente
│   ├── webhook.py             # FastAPI (webhook do Telegram + callback Amplopay)
│   ├── worker.py              # Worker que processa updates + follow-ups
│   ├── bot_handlers.py        # Handlers de comandos e callbacks
│   ├── campaign.py            # Lógica de campanha e follow-ups
│   ├── pix_payment.py         # Integração com Amplopay (geração de PIX)
│   ├── redis_client.py        # Cliente Redis compartilhado
│   ├── text_utils.py          # Utilitários para sanitizar HTML
│   ├── copy.py                # Textos e mensagens do bot
│   └── media/                 # Vídeos e imagens (*.mp4, *.jpg, *.jpeg, *.png)
├── requirements.txt
├── Procfile.web               # Railway: serviço web (FastAPI)
├── Procfile.worker            # Railway: worker (processamento)
└── README.md
```

## Como Funciona

### 1. `/start` - Primeira Mensagem
- Envia `video_32525.*` + legenda + botão "VIP COM DESCONTO por R$19,90"

### 2. Clique no Botão (CTA)
- Envia `image_32528.*`
- Gera pagamento PIX via **Amplopay** (R$19,90 ou valor com desconto)
- Envia código Pix Copia e Cola
- Envia botão "Verificar Status do Pagamento"
- Inicia timer de 5 minutos para follow-up

### 3. Follow-ups Automáticos (a cada 5 minutos)
Se o usuário **não pagar**, o bot envia automaticamente:

| Tempo | Mídia | Desconto | Valor |
|-------|-------|----------|-------|
| +5 min | att.jpeg | 5% OFF | R$18,90 |
| +10 min | video_32495 | 10% OFF | R$17,91 |
| +15 min | video_32497 | 15% OFF | R$16,92 |
| +20 min | video_32501 | 20% OFF | R$15,92 |
| +25 min | video_32503 | 30% OFF | R$13,93 |
| +30 min | video_32508 | 40% OFF | R$11,94 |
| +35 min | video_32501 | 50% OFF | R$10,97 |
| +40 min | video_32521 | 45% OFF | R$10,94 |

### 4. Verificação de Pagamento
- Usuário clica em "Verificar Status do Pagamento"
- Bot consulta Redis (atualizado via callback da Amplopay)
- Se pago: para follow-ups e libera acesso VIP

## Variáveis de Ambiente

Configure no Railway (ou `.env` local):

```bash
# Bot do Telegram
BOT_TOKEN=seu_token_do_botfather
BASE_URL=https://seu-servico.up.railway.app
WEBHOOK_SECRET=seu_secret_para_webhook

# Redis/Valkey (Railway fornece automaticamente)
REDIS_URL=redis://...
# ou
VALKEY_URL=valkey://...

# Amplopay (Gateway de PIX)
AMPLOPAY_PUBLIC_KEY=caduresendex_rzyako90g3w6rmjx
AMPLOPAY_SECRET_KEY=v9ls0bu9p3aqc9e7w6b04bww0wsmc9xjn7dr7mcjz95z6gvh7v1yiokvq8k51gpo
AMPLOPAY_API_URL=https://app.amplopay.com/api/v1

# Opcional
QUEUE_KEY=tg:updates
```

## Deploy no Railway

### 1. Adicione Serviços
- **Valkey** (Redis): adicione via Railway
- **Webhook Service** (web): aponta para `Procfile.web`
- **Worker Service** (worker): aponta para `Procfile.worker`

### 2. Configure Variáveis
- Copie as variáveis acima no painel do Railway
- `BASE_URL`: copie a URL pública do serviço **web**

### 3. Adicione Mídia
Suba os arquivos para `telegram-bot/app/media/`:
- `video_32525.mp4`
- `image_32528.jpg`
- `att.jpeg`
- `video_32495.mp4`, `video_32497.mp4`, `video_32501.mp4`, `video_32503.mp4`, `video_32508.mp4`, `video_32521.mp4`

### 4. Deploy
```bash
# Railway detecta automaticamente os Procfiles e cria 2 serviços
railway up
```

## Callback da Amplopay

Configure no painel da Amplopay para notificar pagamentos:

**URL do Callback:**
```
https://seu-servico.up.railway.app/amplopay/callback
```

**Formato esperado (POST JSON):**
```json
{
  "transactionId": "clwuwmn4i0007emp9lgn66u1h",
  "status": "OK",
  "metadata": {
    "user_id": "123456789",
    "platform": "telegram"
  }
}
```

Quando `status == "OK"`, o bot:
- Para os follow-ups
- Marca o usuário como "pago"
- Libera acesso VIP na próxima verificação

## Arquitetura

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│  Telegram   │ ───> │  webhook.py  │ ───> │ Redis Queue │
│   Update    │      │  (FastAPI)   │      │  tg:updates │
└─────────────┘      └──────────────┘      └─────────────┘
                                                   │
                                                   ▼
                    ┌──────────────────────────────────┐
                    │         worker.py                │
                    │  1. Processa updates (aiogram)   │
                    │  2. Loop de follow-ups (Redis)   │
                    └──────────────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │        campaign.py               │
                    │  - Envia mídia + texto + botão   │
                    │  - Gera PIX (pix_payment.py)     │
                    │  - Agenda follow-ups (Redis)     │
                    └──────────────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │       Amplopay API               │
                    │  POST /gateway/pix/receive       │
                    │  → Retorna código Pix            │
                    │  → Callback: /amplopay/callback  │
                    └──────────────────────────────────┘
```

## Editar Textos

Edite `telegram-bot/app/copy.py` para alterar:
- Mensagem do `/start`
- Textos dos follow-ups
- Nomes dos botões

**Importante:** O código agora sanitiza HTML automaticamente (remove tags `<div>`, `<img>`, etc.). Se quiser texto puro, cole diretamente.

## Testes Locais

```bash
# 1. Instale dependências
pip install -r requirements.txt

# 2. Configure .env
export BOT_TOKEN=...
export REDIS_URL=redis://localhost:6379
export BASE_URL=https://seu-ngrok.io
export AMPLOPAY_PUBLIC_KEY=...
export AMPLOPAY_SECRET_KEY=...

# 3. Rode webhook (FastAPI)
uvicorn app.webhook:app --host 0.0.0.0 --port 8000

# 4. Rode worker (em outro terminal)
python -m app.worker
```

## Troubleshooting

### "/start não responde"
1. **Webhook não configurado**: verifique `BASE_URL` e rode o worker
2. **WEBHOOK_SECRET errado**: deixe vazio ou configure corretamente no `setWebhook`
3. **Mídia não encontrada**: verifique `telegram-bot/app/media/`
4. **Erro de sintaxe (Python < 3.10)**: use Python 3.10+

### "Follow-ups não disparam"
1. **Worker não está rodando**: verifique logs do serviço `worker`
2. **Redis desconectado**: verifique `REDIS_URL`/`VALKEY_URL`
3. **Usuário já marcado como pago**: limpe Redis ou teste com outro usuário

### "Pagamento não confirma"
1. **Callback da Amplopay não configurado**: configure no painel
2. **Credenciais erradas**: verifique `AMPLOPAY_PUBLIC_KEY` e `AMPLOPAY_SECRET_KEY`
3. **API retorna erro**: verifique logs do webhook

## Logs

- **Railway:** veja logs do serviço `web` e `worker` no dashboard
- **Local:** logs aparecem no terminal

## Segurança

- **Nunca commite** `.env` ou chaves da API no Git
- **WEBHOOK_SECRET**: use um valor aleatório forte
- **Amplopay**: guarde as chaves em variáveis de ambiente, não hardcode

## Suporte

Em caso de dúvidas, verifique:
- [Documentação do aiogram](https://docs.aiogram.dev/)
- [Documentação da Amplopay](https://docs.amplopay.com/)
- [Railway Docs](https://docs.railway.app/)
