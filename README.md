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
   search the live Odoo catalog — falling back to a semantic vector search
   (Supabase pgvector) when exact/substring matching can't resolve a
   synonym or natural-language description
3. **Grounded reply** — stock and price are only ever stated after a real
   tool call returns that data; the agent never answers from assumption
4. **Ambiguity handling** — if multiple products match, the agent asks which
   one, rather than guessing
5. **Every conversation logged** to Supabase Postgres, with a structured
   resolution status (resolved / escalated / clarifying) for review and
   analytics

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
                             |          |            |
                             v          v            v
                      search_product  get_product_   vector_search
                      (sub-workflow)  details          (sub-workflow,
                                      (sub-workflow)    semantic fallback)
                             |          |            |
                             +----------+------------+
                                        v
                       Odoo (JSON-RPC, execute_kw)
                       + Supabase pgvector (product_embeddings)
                                        |
                                        v
                             Grounded reply composed
                                        |
                                        v
                       [STATUS:...] tag parsed & stripped
                                        |
                                        v
                      HTTP Request → Meta Graph API
                      (POST /{phone_number_id}/messages)
                                        |
                                        v
                          Logged to Supabase Postgres
                          (phone, message, clean reply, resolved,
                           resolution_status, response_time_ms)
```

`search_product` and `get_product_details` are exact/structured lookups
against live Odoo data — always correct when they hit. `vector_search` is a
semantic fallback against a Supabase-hosted embedding snapshot of the
catalog (synced separately, not live) for queries the structured tools
can't match by substring — see [RAG Architecture](#-rag--semantic-search-architecture)
below.

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
| **Supabase (Postgres + pgvector)** | Conversation audit log (`conversation_logs`) and the product embedding table (`product_embeddings`) — cloud-hosted, separate from the local Docker stack; see [Infrastructure Split](#-infrastructure-split-local-vs-cloud) |
| **Google Gemini Embeddings** (`gemini-embedding-001`, 3072-dim) | Turns the catalog into vectors for semantic search — same Gemini credential as the chat model |
| **Docker Compose** | Local dev environment (Odoo, n8n) — Project #1's Postgres container handles invoice audit data only, not this project's logging |
| **Cloudflare Tunnel** | Permanent public HTTPS endpoint for the n8n webhook (survives restarts, unlike a disposable ngrok tunnel) |

---

## 🗂️ Infrastructure Split: Local vs. Cloud

This project deliberately runs on two separate Postgres instances, not one:

- **Project #1's local Docker Postgres** — invoice audit data
  (`invoice_deliveries`), stays fully self-hosted for that project's own
  reasons (see its own README)
- **This project's Supabase instance** — `conversation_logs` and
  `product_embeddings`, both cloud-hosted

This wasn't the original plan — the conversation logging credential was
found already pointed at Supabase mid-build (not local Postgres as
originally documented), traced via a `relation "public.conversation_logs"
does not exist` error. Rather than move it back, the call was made to keep
AI/RAG-adjacent data (logging + embeddings) together in Supabase, since the
vector store already had to live there, and leave Project #1's audit trail
where it's always been. One cloud dependency for this project's AI
concerns, not two.

Schema and security fixes for the Supabase side are tracked as numbered SQL
migrations in [`SQL/`](./SQL), applied in order against the Supabase SQL
editor — same idea as `init.sql` in Project #1, just cloud-side.

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
- Candidates are filtered to require a **whole-word** match against the
  product name before being returned — an earlier version allowed any
  substring through, which meant a query like "bin" matched inside
  "ca**bin**et" and "com**bin**ation"; fixed after the golden-dataset
  review surfaced it, see `DEBUGGING_LOG.md`
- Returns candidates ranked by `match_score` — a reproducible string-overlap
  score, deliberately **not** an LLM-reported "confidence" value

### `get_product_details(product_id)`
- Direct lookup of `qty_available`, `list_price`, and category
- Explicitly normalizes Odoo's `false`-as-empty-field sentinel before
  returning data, so a genuinely out-of-stock item is never misreported

### `vector_search(query)`
- Semantic fallback for queries `search_product` can't resolve by
  exact/substring matching — synonyms, typos beyond a simple substring, or
  natural-language descriptions ("something to make my office quieter")
- Runs the query against `product_embeddings` in Supabase (pgvector),
  embedded with Google Gemini's `gemini-embedding-001` (3072-dim), via a
  Postgres RPC function (`match_product_embeddings`)
- Returns the top 4 matches with a real cosine similarity score — **not a
  reliable match/no-match signal on its own.** Testing showed genuinely
  unrelated products (a lamp, a charging cable substitute) scoring
  0.55–0.65 against a query with no real catalog match. The agent's
  no-hallucination grounding rules — not a similarity threshold — are what
  keep a mediocre vector match from becoming an invented answer
- The catalog snapshot in Supabase is **not live** — it's rebuilt by a
  separate, manually-triggered `Sync Odoo Products to Supabase` workflow,
  so a product added/edited/renamed in Odoo won't appear here until that
  sync runs again (see Setup §7)

Both structured tools are built as standalone n8n sub-workflows (not
inline HTTP nodes) because each needs pre/post-processing logic — see
[`DEBUGGING_LOG.md`](./DEBUGGING_LOG.md#5-http-request-tool-cant-run-multi-step-logic)
for why that choice was necessary, not just preferred. `vector_search`
follows the same sub-workflow pattern for the same reason: the raw
Supabase node output needs reshaping before the agent can use it.

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
- A Supabase project (free tier) with the `vector` extension enabled —
  hosts both `conversation_logs` and `product_embeddings` (see
  [Infrastructure Split](#-infrastructure-split-local-vs-cloud))

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

### 3. Set up the Supabase schema

`conversation_logs` and `product_embeddings` both live in Supabase, not the
local Docker Postgres — run the migrations in
[`SQL/`](./SQL) against your Supabase project's **SQL Editor**, in order:

1. `SQL/001_initial_conversation_logs_schema.sql` — creates `conversation_logs`
   with RLS enabled
2. `SQL/002_security_and_logging_fixes.sql` — tightens the RLS policy to
   `service_role` only (the first pass used `USING (true)` with no role
   restriction, which Supabase's own Security Advisor flags as
   effectively granting `anon`/`authenticated` full read/write on customer
   phone numbers and message content — closed, not left in place), enables
   RLS on `product_embeddings` (originally created with none), pins
   `match_product_embeddings`'s `search_path`, and adds the
   `resolution_status` column used for structured conversation logging

Also create the `product_embeddings` table + `match_product_embeddings` RPC
function if this is a fresh Supabase project — the vector store nodes in
`Sync Odoo Products to Supabase.json` and `Tool - Vector Search.json`
expect both to already exist (n8n's Supabase Vector Store node doesn't
create them for you). Table: `id`, `content` (text), `metadata` (jsonb),
`embedding vector(3072)`. RPC function signature:
`match_product_embeddings(query_embedding vector, match_count integer,
filter jsonb)`.

Verify RLS is actually restrictive, not just enabled, before moving on:
```sql
SET ROLE anon;
SELECT * FROM conversation_logs;  -- should error, not return rows
RESET ROLE;
```

### 4. Import the workflows

Import all five files from `workflows/` into n8n:
- `Tool - Search Product.json`
- `Tool - Get Product Details.json`
- `Tool - Vector Search.json`
- `Sync Odoo Products to Supabase.json`
- `AI Inventory Assistant.json` (the main workflow)

**Publish/Activate all three tool sub-workflows** (not just the two
structured ones) before testing — the main workflow will fail to call any
of them otherwise (see [Debugging Log
#6](./DEBUGGING_LOG.md#6-tool-sub-workflows-not-activepublished)). This
bit twice during development: publishing a sub-workflow's *editor changes*
after an edit is a separate action from publishing it for the first time —
"Execute step" on a single node inside the editor runs your current draft,
**not** what the live agent actually calls. Always re-Publish after any
edit to a tool sub-workflow, then verify against a real execution from the
main workflow, not just the node's own test output.

### 5. Configure credentials in n8n

- **Google Gemini**: paste your AI Studio API key — reused for both the
  chat model and the embeddings node in the vector search / sync workflows
- **Postgres — AI Assistant Logs**: point at your **Supabase** project's
  connection string (Project Settings → Database → Connection string),
  not local Docker Postgres — used by `log_conversation`
- **Supabase account**: your Supabase project URL + API key (service role,
  not anon) — used by the `Supabase Vector Store` nodes in
  `Sync Odoo Products to Supabase` and `Tool - Vector Search`
- Meta's access token and phone number ID are read from the environment
  variables set in Step 2, not an n8n credential object — Meta's Graph API
  calls need these as plain header/URL values, not an auth type n8n's
  credential UI models directly.

### 6. Sync the catalog to Supabase

Run `Sync Odoo Products to Supabase` manually (Execute Workflow). It clears
`product_embeddings` and rebuilds it from Odoo's current catalog — a full
resync, not incremental, which is fine at this catalog size but means
**re-run this any time products are added, edited, or renamed in Odoo**,
or `vector_search` will be answering against a stale snapshot. Confirm the
row count matches your Odoo product count afterward:
```sql
SELECT COUNT(*) FROM product_embeddings;
```

### 7. Register the webhook with Meta

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

### 8. Test

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
| Semantic fallback, no substring match | "something to charge my phone" | Falls to `vector_search`, no real match found (0.55–0.61 similarity on unrelated products), correctly escalates rather than guessing |
| Out-of-stock, still a resolved answer | "is the large meeting table in stock?" | Reports out-of-stock as a real answer — tagged `resolved`, not `escalated`, since the product was found |
| Ambiguity after catalog rename | "chair" | Lists all 4 distinct chair products (`Conference Chair (Black)`, `Conference Chair (Grey)`, `Office Chair`, `Office Chair Black`) — catalog originally had two identically-named "Conference Chair" SKUs, renamed after the golden-dataset review surfaced it would break disambiguation |

---


## ⚠️ Known Limitations (Honest, By Design)

- **Response time**: Ranging from 2.7s to 13s end-to-end (as logged in PostgreSQL), driven by multi-turn Gemini reasoning and live Odoo JSON-RPC queries. Outbound message dispatch itself is immediate via the Meta Graph API. Adding `vector_search` to the tool chain adds latency on fallback queries specifically — not yet broken out separately in the logs.
- **`tool_called` and `matched_product_id` are not logged**: This version of n8n's AI Agent node doesn't natively expose which specific tool ran or which product ID it matched at the top level. `resolution_status` (RESOLVED/ESCALATED/CLARIFYING, parsed from a tag the agent appends and that gets stripped before the customer sees it) replaced the old brittle approach of guessing resolution from reply text via string-matching — but which *tool* answered a given query is still not captured. Rather than guess, these two columns are left `NULL` — a real, documented gap, not a bug or a fake value.
- **Vector similarity scores are not a reliable match/no-match threshold**: testing showed genuinely unrelated products scoring 0.55–0.65 against queries with no real catalog match. The agent's grounding rules prevent this from producing hallucinated answers, but a similarity cutoff alone would not be safe to rely on here.
- **`product_embeddings` sync is manual and full-resync only**: no scheduled trigger, no incremental upsert — every run clears and rebuilds the whole table. Fine at ~50 products; would need revisiting (incremental sync, or a scheduled trigger) before this pattern scaled to a much larger catalog.
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
- Scheduled/incremental sync for `product_embeddings` instead of manual
  full-resync, once the catalog size justifies it
- Log `tool_called` per turn once n8n's AI Agent node exposes intermediate
  tool-call steps at the top level (or have each tool sub-workflow write
  a partial log row itself as a workaround)

---

## 📚 Documentation

- **[`project-architecture.md`](./project-architecture.md)** — full design
  rationale, data flow, tool contracts, MVP boundaries
- **[`DEBUGGING_LOG.md`](./DEBUGGING_LOG.md)** — real bugs hit during the
  build: the original agent/tools/memory build, the Twilio → Meta Cloud
  API migration, and the RAG/Supabase integration (sync idempotency,
  substring false positives, a silently-zeroed similarity score, Supabase
  RLS gaps caught by its own Security Advisor, and the
  publish-vs-editor-draft trap) — the most honest documentation of what
  this actually took to build
- **[`SQL/`](./SQL)** — numbered Supabase schema migrations, applied in
  order against the Supabase SQL Editor

---

## 📄 License

MIT License © 2026 Taha Tahir

---

## 🤝 Related Work

Project #1 in this portfolio: [Odoo Invoice
Automation](../Odoo-Invoice-Automation) — automated WhatsApp invoice
delivery via Twilio, sharing the same Odoo/n8n foundation this project
builds on.
