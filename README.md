# Agente Secretário IA — Bot Telegram

Bot Telegram com agente IA que atua como secretário pessoal para diretores de empresa.
Gerencia tarefas, agenda (integrada ao Google Calendar) e lembretes automáticos via conversa natural.

## Stack

- Python 3.11+
- OpenAI Agents SDK (`openai-agents`) + GPT-4.1 Mini
- python-telegram-bot v21+
- PostgreSQL + SQLAlchemy 2.0 (async) + Alembic
- Google Calendar API (Domain-Wide Delegation)
- APScheduler via JobQueue

---

## Pré-requisitos

- Python 3.11+
- PostgreSQL 15+ rodando localmente ou em servidor
- Conta no [BotFather](https://t.me/BotFather) para criar o bot Telegram
- Chave de API OpenAI com acesso ao GPT-4.1 Mini
- (Opcional) Google Workspace com permissão de administrador para configurar Domain-Wide Delegation

---

## Instalação

```bash
# 1. Clone e acesse o diretório
cd secretary-bot

# 2. Crie e ative o ambiente virtual
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
.venv\Scripts\activate           # Windows

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Configure as variáveis de ambiente
cp .env.example .env
# Edite o .env com seus valores reais
```

---

## Configuração do `.env`

```env
TELEGRAM_BOT_TOKEN=token_do_botfather
OPENAI_API_KEY=sk-...
DATABASE_URL=postgresql+asyncpg://usuario:senha@localhost:5432/secretary_bot
AUTHORIZED_USERS=123456789,987654321
GOOGLE_SERVICE_ACCOUNT_FILE=credentials/google-service-account.json
USER_EMAIL_MAP=123456789:diretor1@empresa.com.br,987654321:diretor2@empresa.com.br
TIMEZONE=America/Sao_Paulo
```

**Como obter o Telegram ID:** Envie uma mensagem para [@userinfobot](https://t.me/userinfobot).

---

## Banco de Dados

```bash
# Crie o banco no PostgreSQL
createdb secretary_bot

# Execute as migrations
alembic upgrade head
```

---

## Configuração do Google Calendar (Domain-Wide Delegation)

> Esta etapa é necessária apenas se o Google Workspace for usado. O bot funciona sem ela,
> usando apenas o banco local para reuniões.

### Passo 1 — Google Cloud Console

1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. Crie um projeto: **secretary-bot**
3. Habilite as APIs:
   - **Google Calendar API**
4. Vá em **IAM & Admin → Service Accounts → Create Service Account**
   - Nome: `secretary-bot-sa`
   - Clique em **Create Key → JSON → Download**
   - Salve como `credentials/google-service-account.json`
   - Anote o **Client ID** numérico (ex: `1234567890123456789`)

### Passo 2 — Google Admin Console

1. Acesse [admin.google.com](https://admin.google.com) com conta super admin
2. Vá em: **Segurança → Acesso e controle de dados → Controles de API → Delegação domain-wide**
3. Clique em **Adicionar novo**
4. Insira o **Client ID** da service account
5. Insira os escopos:
   ```
   https://www.googleapis.com/auth/calendar
   https://www.googleapis.com/auth/calendar.events
   ```
6. Salve

---

## Iniciando o Bot

```bash
python -m bot.main
```

O bot inicia, conecta ao banco, carrega as credenciais Google e começa a receber mensagens.

---

## Exemplos de Interação

```
Diretor: "Cria uma tarefa de revisar o contrato do fornecedor, urgente"
Agente:  "✅ Tarefa criada: Revisar contrato do fornecedor [URGENTE]"

Diretor: "Marca uma reunião com o financeiro na quinta às 14h"
Agente:  "📅 Reunião marcada: Reunião com o financeiro — Quinta, 24/04, 14:00
          Sincronizado com Google Calendar. Quer um lembrete antes?"

Diretor: "Me lembra às 13:30"
Agente:  "🔔 Lembrete criado para 13:30 — Reunião com o financeiro em breve."

Diretor: "O que tenho hoje?"
Agente:  "☀️ Seu dia (24/04):
          📋 1 tarefa pendente: Revisar contrato do fornecedor [URGENTE]
          📅 1 reunião: 14:00 - Reunião com o financeiro
          ⏰ 1 lembrete: 13:30 - Reunião com o financeiro em breve."
```

---

## Estrutura de Arquivos

```
secretary-bot/
├── bot/
│   ├── main.py                    # Entry point
│   ├── config.py                  # Configurações (.env)
│   ├── database/
│   │   ├── connection.py          # Engine SQLAlchemy async
│   │   ├── models.py              # 5 modelos (User, Task, Meeting, Reminder, ConversationMessage)
│   │   └── crud.py                # Operações de banco
│   ├── integrations/
│   │   └── google_calendar.py     # Client Google Calendar API
│   ├── agent/
│   │   ├── secretary_agent.py     # Agente principal (orquestrador)
│   │   ├── guardrails.py          # Autorização por Telegram ID
│   │   ├── memory.py              # Histórico de conversa
│   │   └── tools/
│   │       ├── task_tools.py      # 4 tools de tarefas
│   │       ├── meeting_tools.py   # 5 tools de reuniões + Google Calendar
│   │       └── reminder_tools.py  # 3 tools de lembretes
│   ├── handlers/
│   │   └── telegram_handler.py    # Telegram → Agente → Resposta
│   └── scheduler/
│       └── reminder_jobs.py       # Jobs automáticos (lembretes + resumo diário)
├── alembic/                       # Migrations
├── credentials/                   # Service Account JSON (não commitar)
├── .env.example
├── requirements.txt
└── README.md
```

---

## Roadmap

- **Fase 1 (atual):** Core + Google Calendar (12 tools)
- **Fase 2:** Sub-agente BI (Power BI via REST API)
- **Fase 3:** Sub-agente E-mail (Gmail via Domain-Wide Delegation)
- **Fase 4:** Suporte a áudio (Whisper) + Dashboard web
