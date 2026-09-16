# 🤖 AI Inventory Assistant — WhatsApp Stock & Pricing Agent for Odoo

**An AI agent that answers customer WhatsApp questions about product stock
and pricing, grounded entirely in live Odoo ERP data — never guessing, never
inventing an answer.**

![Status](https://img.shields.io/badge/Status-MVP%20Complete-brightgreen)
![License](https://img.shields.io/badge/License-MIT-blue)
![n8n](https://img.shields.io/badge/n8n-2.27-orange)
![Odoo](https://img.shields.io/badge/Odoo-17.0-red)

This is Project #2 in a portfolio of SME workflow automation projects, built
on the same core stack as [Project #1: Odoo Invoice
Automation](../Odoo-Invoice-Automation) — same Odoo integration, extended
with an AI Agent, tool-calling, and conversation memory. The WhatsApp
channel started on Twilio's Sandbox for early development, then was
migrated to Meta's official WhatsApp Business Cloud API directly once the
core agent logic was proven — see the [Setup](#-setup) and
[Documentation](#-documentation) sections for why that move mattered.

---

## 🎯 Overview

Retail and distribution SMEs running Odoo get a constant stream of WhatsApp
messages asking "is this in stock?" and "how much is it?". Answering these
manually costs staff time and delays responses — sometimes losing the sale
to a customer who doesn't wait.

This assistant answers those questions automatically, in seconds, over the
business's real WhatsApp number:

1. **Customer messages** the business's WhatsApp number with a product
   question
2. **AI Agent** (Google Gemini) interprets the question and calls tools to
   search the live Odoo catalog
3. **Grounded reply** — stock and price are only ever stated after a real
   tool call returns that data; the agent never answers from assumption
4. **Ambiguity handling** — if multiple products match, the agent asks which
   one, rather than guessing
5. **Every conversation logged** to PostgreSQL for review and analytics

**Business impact:**
- ✅ Instant, always-on responses to stock/price questions
- ✅ Frees staff from repetitive lookups
- ✅ Runs on Meta's official WhatsApp Business API — no sandbox message
  caps, no 72-hour rejoin requirement, suitable for a real client trial
- ✅ Every answer traceable to a real database query, not a hallucination

---

## 📊 System Architecture

```
Customer (WhatsApp)
      |
      v
   Meta Cloud API  --webhook-->  n8n
                                   |
                                   v
                       Guard: real message or
                       status callback? (messages.length > 0)
                                   |
                                   v
                            AI Agent Node
                            (Gemini, tool-calling, memory)
                             |              |
                             v              v
                      search_product   get_product_details
                      (sub-workflow)   (sub-workflow)
                             |              |
                             +------+-------+
                                    v
                            Odoo (JSON-RPC, execute_kw)
                                    |
                                    v
                           Grounded reply composed
                                    |
                                    v
                    HTTP Request → Meta Graph API
                    (POST /{phone_number_id}/messages)
                                    |
                                    v
                           Logged to PostgreSQL
                           (phone, message, reply, resolved,
                            response_time_ms)
```

Full design rationale, data flow, and tool contracts are in
[`project-architecture.md`](./project-architecture.md).

---

## ⚙️ Technology Stack

| Component | Role |
|---|---|
| **n8n** | Orchestration — webhook, AI Agent, tools, memory, logging |
| **Google Gemini API** (`gemini-2.5-flash` / `3-flash-preview`) | LLM backend — chosen for a genuine free tier; swappable without touching tools or logic |
| **Odoo 17** | Source of truth for product name/SKU/category/stock/price, via JSON-RPC |
| **Meta WhatsApp Business Cloud API** | Customer-facing channel — direct integration via Graph API, not a third-party wrapper |
| **PostgreSQL 16** | Conversation audit log |
| **Docker Compose** | Local dev environment (Odoo, n8n, Postgres) |
| **Cloudflare Tunnel** | Permanent public HTTPS endpoint for the n8n webhook (survives restarts, unlike a disposable ngrok tunnel) |

---

## 🧠 How the Agent Stays Grounded

The system prompt enforces strict rules, not just suggestions:

- Stock/price is **only ever stated immediately after a tool call returns
  that exact data** — never from prior knowledge or assumption
- **Multiple matches → the agent asks which one**, rather than guessing
  (e.g. "Blue T-Shirt" resolves to Small/Medium/Large and the agent asks the
  customer to clarify)
- **No match → clean escalation** ("a team member will follow up"), never a
  substituted or invented product
- **Follow-up references** ("what about the gaming one?") are resolved
  using conversation memory, but stock/price is always **re-verified with a
  fresh tool call**, never assumed stale from earlier in the conversation

This is deliberately more constrained than a general-purpose chatbot — the
constraint is the actual product.

---

## 🔧 Tool Definitions

### `search_product(query)`
- Searches Odoo's `product.product` by name, SKU, or barcode
- Multi-word queries are matched as an AND of substrings against the name
  (so "blue shirt" correctly matches "Blue T-Shirt Small/Medium/Large"),
  OR'd against a direct SKU/barcode match
- Returns candidates ranked by `match_score` — a reproducible string-overlap
  score, deliberately **not** an LLM-reported "confidence" value

### `get_product_details(product_id)`
- Direct lookup of `qty_available`, `list_price`, and category
- Explicitly normalizes Odoo's `false`-as-empty-field sentinel before
  returning data, so a genuinely out-of-stock item is never misreported

Both tools are built as standalone n8n sub-workflows (not inline HTTP
nodes) because each needs pre/post-processing logic — see
[`DEBUGGING_LOG.md`](./DEBUGGING_LOG.md#5-http-request-tool-cant-run-multi-step-logic)
for why that choice was necessary, not just preferred.

---

## 🚀 Setup

### Prerequisites
- Docker Desktop with WSL2 (Windows) or native Docker (Linux/Mac)
- An Odoo 17 instance running (see [Project #1's
  docker-compose.yml](../Odoo-Invoice-Automation/docker-compose.yml) for a
  working reference)
- n8n v2.27+ (self-hosted, Community Edition is sufficient)
- A Google AI Studio API key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) — free tier, no card required
- A Meta Developer account with an app configured for WhatsApp Business
  Messaging, plus a permanent System User access token (a temporary
  24-hour token will not survive past your first testing session)
- A public HTTPS endpoint for the n8n webhook — Cloudflare Tunnel
  (permanent domain, recommended) or ngrok (disposable, fine for quick
  local testing only)

### 1. Seed the demo catalog

```bash
pip install requests --break-system-packages

export ODOO_URL=http://localhost:8069
export ODOO_DB=odoo
export ODOO_USERNAME=admin
export ODOO_PASSWORD=admin

python3 seed_demo_catalog.py
```

Seeds 21 products across Apparel/Electronics/Accessories, including a
same-name product cluster (for testing disambiguation) and one intentionally
zero-stock item (for testing out-of-stock handling). See the script's
comments for the full catalog list.

### 2. Set required environment variables

In your n8n container's `docker-compose.yml`:
```yaml
environment:
  N8N_BLOCK_ENV_ACCESS_IN_NODE: "false"
  ODOO_URL: "http://odoo:8069"
  ODOO_DB: "odoo"
  ODOO_USER: "${ODOO_USER}"
  ODOO_PASSWORD: "${ODOO_PASSWORD}"
  META_VERIFY_TOKEN: "${META_VERIFY_TOKEN}"
  META_ACCESS_TOKEN: "${META_ACCESS_TOKEN}"
  META_PHONE_NUMBER_ID: "${META_PHONE_NUMBER_ID}"
```
`http://odoo:8069` (the Docker service name), not `localhost` — n8n and
Odoo run in separate containers on the same Docker network.
`META_VERIFY_TOKEN` is a secret string **you invent yourself** — it just
needs to match on both sides (this file, and Meta's webhook config page).
`META_ACCESS_TOKEN` and `META_PHONE_NUMBER_ID` come from your Meta app's
System User and API Setup page respectively.

**Changing any of these three values requires a container restart**
(`docker-compose restart n8n`) before n8n will pick them up — a plain
workflow save/publish is not enough.

### 3. Create the logging database

```bash
docker exec -it <postgres_container_name> psql -U odoo -c "CREATE DATABASE ai_assistant_logs;"

docker exec -it <postgres_container_name> psql -U odoo -d ai_assistant_logs << 'EOF'

CREATE TABLE IF NOT EXISTS conversation_logs (
  id SERIAL PRIMARY KEY,
  phone VARCHAR(20) NOT NULL,
  user_message TEXT NOT NULL,
  assistant_reply TEXT,
  tool_called VARCHAR(50),
  matched_product_id INTEGER,
  match_score NUMERIC(3,2),
  resolved BOOLEAN NOT NULL DEFAULT false,
  response_time_ms INTEGER,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_conversation_phone ON conversation_logs(phone);
CREATE INDEX idx_conversation_created_at ON conversation_logs(created_at);

EOF
```

### 4. Import the workflows

Import all three files from `workflows/` into n8n:
- `Tool - Search Product.json`
- `Tool - Get Product Details.json`
- `AI Inventory Assistant.json` (the main workflow)

**Publish/Activate both tool sub-workflows** before testing — the main
workflow will fail to call them otherwise (see [Debugging Log
#6](./DEBUGGING_LOG.md#6-tool-sub-workflows-not-activepublished)).

### 5. Configure credentials in n8n

- **Google Gemini**: paste your AI Studio API key
- **Postgres**: point at `ai_assistant_logs` (host: `postgres`, port `5432`)
- Meta's access token and phone number ID are read from the environment
  variables set in Step 2, not an n8n credential object — Meta's Graph API
  calls need these as plain header/URL values, not an auth type n8n's
  credential UI models directly.

### 6. Register the webhook with Meta

1. Expose your n8n webhook publicly (Cloudflare Tunnel domain, or ngrok
   for quick testing)
2. In your Meta app's WhatsApp Configuration page, set:
   - **Callback URL**: `https://<your-domain>/webhook/whatsapp-incoming`
   - **Verify token**: the same string as `META_VERIFY_TOKEN`
3. Meta sends a one-time **GET** verification request (`hub.mode`,
   `hub.verify_token`, `hub.challenge`) before it will save the webhook.
   Since n8n's self-hosted Webhook node can only listen for one HTTP
   method at a time, this requires a **temporary method swap**: set the
   webhook node to GET, verify with Meta, then switch it back to POST for
   live traffic. Editing an already-active trigger's method requires a
   full deactivate/reactivate cycle to actually take effect — see
   [Debugging Log #17](./DEBUGGING_LOG.md) for why.
4. Subscribe to the `messages` webhook field (a separate toggle from
   verifying the URL — both are required)
5. **Publish the app** (App Settings → Basic → App Mode → Live). This step
   is easy to miss and is the single most important one: Meta's dashboard
   will show test messages as "received" even while the app is
   Unpublished, but will silently withhold delivering them to your webhook
   until it's Live. See [Debugging Log #20](./DEBUGGING_LOG.md) for the
   full story — this cost more debugging time than everything else in the
   migration combined.

### 7. Test

Send a WhatsApp message to your Meta test number:
```
Do you have a wireless mouse?
```

---

## 📈 Verified Test Scenarios

All confirmed working over real WhatsApp via Meta's Cloud API:

| Scenario | Message | Result |
|---|---|---|
| Single clean match | "Do you have a wireless mouse?" | Correct stock + price |
| 2-way ambiguity | "Do you have an SSD?" | Asks 1TB vs 512GB |
| 3-way ambiguity | "Do you have a blue shirt?" | Asks Small/Medium/Large |
| No match / escalation | "Do you have gaming headphones?" | Clean escalation, no substitution |
| Memory / pronoun resolution | "wireless mouse?" → "what about the gaming one?" | Correctly pivots, re-verifies stock fresh |

---


## ⚠️ Known Limitations (Honest, By Design)

- **Response time**: Ranging from 2.7s to 13s end-to-end (as logged in PostgreSQL), driven by multi-turn Gemini reasoning and live Odoo JSON-RPC queries. Outbound message dispatch itself is immediate via the Meta Graph API.
- **`tool_called`, `matched_product_id`, `match_score` are not logged**: This version of n8n's AI Agent node doesn't natively expose which tools ran or their results at the top level. Rather than guess from reply text, these columns are left `NULL` — a real, documented gap, not a bug or a fake value.
- **Memory is message-count-based, not time-based**: Resets per chat session rather than expiring on a time-based TTL.
- **Free-tier LLM quota**: While Meta's Permanent System Token never expires or runs out of quota, the Google Gemini LLM backend runs on a free tier and remains subject to provider rate limits during heavy load testing.
- **Meta test number only**: Currently registered against Meta's developer test phone number, which requires recipient whitelisting until migrated to a fully verified Meta Business Account.
- **No order placement, no web widget, no admin dashboard** — all
  deliberately out of scope for this MVP (see
  [`project-architecture.md`](./project-architecture.md#8-mvp-boundaries-explicitly-out-of-scope-for-v1)).

---

## 🔮 Future Improvements

- Consolidate `search_product` + `get_product_details` into a single tool
  call for single-match queries, saving one LLM reasoning round-trip (see
  discussion in project notes — deliberately deferred to preserve tested
  disambiguation behavior)
- Category-filter queries ("show me accessories under $30")
- Product recommendations ("gaming mouse under $40" → ranked options)
- Web chat widget alongside WhatsApp
- TTL-based memory via a persistent store
- Business-verified Meta phone number for real client deployments

---

## 📚 Documentation

- **[`project-architecture.md`](./project-architecture.md)** — full design
  rationale, data flow, tool contracts, MVP boundaries
- **[`DEBUGGING_LOG.md`](./DEBUGGING_LOG.md)** — real bugs hit during the
  build, including the original agent/tools/memory build and the later
  Twilio → Meta Cloud API migration, with root causes and fixes — the most
  honest documentation of what this actually took to build

---

## 📄 License

MIT License © 2026 Taha Tahir

---

## 🤝 Related Work

Project #1 in this portfolio: [Odoo Invoice
Automation](../Odoo-Invoice-Automation) — automated WhatsApp invoice
delivery via Twilio, sharing the same Odoo/n8n foundation this project
builds on.
