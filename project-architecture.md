# Project #2: AI Inventory Assistant — Architecture

## 1. Problem

SMEs running Odoo receive repetitive WhatsApp/customer messages asking
"is X in stock?" and "how much is X?". Answering these manually costs
staff time and delays responses, sometimes costing sales to customers
who don't wait for a reply.

## 2. Business Value

- Instant, always-on responses to stock/price queries
- Frees staff from repetitive lookups
- Answers are grounded in live Odoo data — never guessed or stale
- Runs on Meta's official WhatsApp Business Cloud API directly (started
  on Twilio's Sandbox for early development, migrated once the core
  agent logic was proven — see `DEBUGGING_LOG.md` #16-20) — no sandbox
  message caps, suitable for a real client trial rather than a demo
  limited to 5 messages/day

## 3. Architecture Diagram

```
Customer (WhatsApp)
      |
      v
  Meta Cloud API  --webhook-->  n8n
                                  |
                                  v
                      Guard: real message or
                      status callback?
                                  |
                                  v
                           AI Agent Node
                           (Google Gemini, tool-calling, memory)
                            |          |            |
                            v          v            v
                     search_product  get_product_   vector_search
                     (structured)    details         (semantic
                                     (structured)     fallback)
                            |          |            |
                            +----------+------------+
                                       v
                      Odoo (JSON-RPC, execute_kw,
                      reusable credential: url/db/user/pass)
                      + Supabase pgvector (product_embeddings)
                                       |
                                       v
                            Grounded reply composed
                                       |
                                       v
                     [STATUS:...] tag parsed & stripped
                     (internal resolution signal, not customer-facing)
                                       |
                                       v
                     HTTP Request → Meta Graph API
                     (POST /{phone_number_id}/messages)
                                       |
                                       v
                        Log to Supabase Postgres
                        (phone, message, clean reply, tool_called,
                         matched_product_id, match_score,
                         resolved, resolution_status, response_time_ms)
```

`search_product`/`get_product_details` are exact/structured Odoo lookups,
tried first. `vector_search` only runs when structured matching doesn't
resolve a query cleanly (synonyms, natural-language descriptions) — see
§10 for why it was added and what it doesn't replace.

## 4. Components

| Component | Role |
|---|---|
| Meta WhatsApp Business Cloud API | Customer-facing channel — direct Graph API integration, not a third-party wrapper |
| n8n | Orchestration: webhook, AI Agent, tools, memory, logging |
| Google Gemini (`gemini-2.5-flash`/`3-flash-preview`) | LLM backend — chosen after Groq's signup flow proved broken (see `DEBUGGING_LOG.md` #11); swappable without touching tools or logic, validated in practice by that actual swap |
| Google Gemini Embeddings (`gemini-embedding-001`, 3072-dim) | Turns the catalog into vectors for `vector_search`; same Gemini credential as the chat model |
| Odoo | Source of truth for product name/SKU/category/stock/price |
| Supabase (Postgres + pgvector) | Conversation audit log (`conversation_logs`) and product embedding store (`product_embeddings`) — cloud-hosted, separate from the local Docker stack (see §10.3) |

## 5. Data Flow

1. Customer sends WhatsApp message → Meta webhook fires → n8n (guard
   checks this is a real message, not a delivery/read status callback)
2. AI Agent receives message + phone-keyed memory context (last 6
   messages — message-count-based, **not** a time-based TTL; see §7)
3. Agent calls `search_product` if the message names/implies a product
4. If `search_product` returns 0 structured matches, the agent falls back
   to `vector_search` for a semantic match (synonyms, natural-language
   descriptions) — in practice, the LLM's own keyword extraction often
   bridges the gap before this fallback is even needed (see
   `DEBUGGING_LOG.md`'s "LLM keyword extraction" observation)
5. If exactly 1 match (from either path) → agent calls
   `get_product_details` → replies with stock+price
6. If >1 match → agent lists candidates, asks user to disambiguate
7. If 0 match from both tools → agent escalates, never substitutes a
   guess
8. The agent appends an internal `[STATUS:RESOLVED|ESCALATED|CLARIFYING]`
   tag to its own output; a Code node parses it into `resolution_status`
   and strips it before the reply reaches the customer
9. Every turn logged to Supabase Postgres regardless of outcome, keyed on
   `resolution_status` rather than inferred from the reply text

## 6. Tool Definitions

### `search_product(query: string)`
- Odoo model: `product.product`
- Domain: OR across `name`, `default_code` (SKU), `barcode`
- Odoo's `ilike` domain match is a broad substring fetch; results are then
  filtered in n8n to require a **whole-word** match against the product
  name before being returned (an earlier version allowed any substring
  through, so "bin" matched inside "cabinet" — see `DEBUGGING_LOG.md` #29)
- Returns: list of `{product_id, name, default_code, match_score}`
- `match_score`: string-similarity ranking between query and matched
  field — NOT an LLM-reported confidence value (deliberately avoided,
  see Project #1 documentation-honesty precedent)

### `get_product_details(product_id: int)`
- Odoo model: `product.product`
- Returns: `{qty_available, list_price, categ_id}`
- Must handle Odoo's `false`-as-empty sentinel explicitly

### `vector_search(query: string)`
- Semantic fallback for queries `search_product` can't resolve by
  exact/substring matching
- Embeds `query` with `gemini-embedding-001`, matches against
  `product_embeddings` in Supabase via the `match_product_embeddings`
  Postgres RPC function, returns the top 4 by cosine similarity
- Catalog snapshot is **not live** — rebuilt by a separate,
  manually-triggered sync workflow (full delete-and-reinsert, not
  incremental); see §10.2
- Similarity score is **not a reliable match/no-match threshold on its
  own** — genuinely unrelated products have scored 0.55–0.65 against
  queries with no real catalog match in testing. The grounding rules in
  §7, not the score, are what prevent a mediocre vector match from
  becoming a hallucinated answer

## 7. Conversation Flow / Grounding Rules

- Agent must NEVER state stock or price without a tool result backing it
  — applies equally regardless of which tool produced the match;
  `vector_search` returning a product is not itself permission to state
  stock/price, `get_product_details` still has to be called
- Ambiguous match (>1 candidate, from either structured or semantic
  search) → always ask, never guess
- No match from either tool → escalate ("let me get a team member") +
  tag `[STATUS:ESCALATED]` in the agent's own output for logging
- A product that's found but out of stock is a **resolved** interaction,
  not an escalation — the agent gave a real, grounded answer, it just
  wasn't the answer the customer wanted
- Memory: last 6 messages, phone-keyed — **message-count-based, not
  time-based.** (Originally scoped as a ~20-30 min TTL; actually
  implemented as a fixed window via n8n's Simple Memory node, which
  resets per chat session rather than expiring on a clock. Documented
  here to match reality, not the original plan — see README's Known
  Limitations.)

## 8. MVP Boundaries (as of v1 — some since revisited, see §10)

- No order placement or payment
- No web widget (stretch goal only if time allows)
- No admin dashboard (Postgres/Supabase queries suffice for now)
- ~~No semantic/vector search — direct structured query is sufficient and
  easier to explain for a ~20-product catalog~~ **Revisited in Phase 2.**
  The original reasoning was sound for v1's scope and catalog size, but
  testing surfaced real queries (synonyms, natural-language descriptions
  like "something to charge my phone") that substring matching
  genuinely couldn't resolve — not a case of over-engineering ahead of
  need. Added as a fallback, not a replacement; `search_product` is still
  tried first on every query. See §10 for the full design.

## 9. Future Improvements (Phase 2+, pre-RAG list — see §11 for current)

- Category-filter queries ("show me accessories under $30")
- Product recommendations ("gaming mouse under $40" → ranked options)
- Web chat widget
- ~~Swap Groq → OpenAI/Anthropic/self-hosted if a client requires it~~
  **Already happened** — Groq's account signup flow proved broken
  (external, unresolved; see `DEBUGGING_LOG.md` #11), switched to Gemini
  mid-build. Only the Chat Model sub-node changed; tools, grounding
  prompt, memory, and logging were untouched — this is the swappability
  claim validated in practice, not just asserted.

## 10. RAG / Semantic Search Architecture (Phase 2)

### 10.1 Why

§8 originally scoped vector search out, reasoning that a ~20-product
catalog didn't justify the overhead and structured matching would be
simpler to explain. That held through early testing, but real queries
surfaced that `search_product` genuinely couldn't resolve — synonyms and
natural-language product descriptions with no literal substring overlap
with any product name ("something to make my office quieter" for
"Acoustic Bloc Screens"). `vector_search` was added as a **fallback**,
not a replacement: `search_product` is still tried first on every query,
and still resolves the large majority of real traffic on its own —
testing showed Gemini's own keyword extraction frequently bridges
synonym gaps before the vector tool is ever needed (e.g. "throw away
tissues" → the LLM extracts "bin" itself).

### 10.2 Sync Pipeline

A separate workflow (`Sync Odoo Products to Supabase`) rebuilds
`product_embeddings` from Odoo's live catalog:

1. Clear `product_embeddings` (full delete, not incremental — a sync
   workflow without this duplicated every product on re-run; see
   `DEBUGGING_LOG.md` #30)
2. Fetch all products from Odoo via `search_read`
3. Format each into `pageContent` (`"Product: {name} | Category: {cat} |
   SKU: {sku}"`) and metadata (`odoo_id`, `name`, `default_code`,
   `list_price`, `qty_available`, `category`)
4. Embed with `gemini-embedding-001` (3072-dim) and insert into
   `product_embeddings`

Manually triggered, not scheduled. Any product added, edited, or renamed
in Odoo requires a manual re-sync before `vector_search` reflects it —
acceptable at this catalog size, would need revisiting (scheduled
trigger, or incremental upsert) at real scale. `pageContent` is
deliberately minimal (no descriptions) for v1 — this means ranking is
shallower than it could be; documented as a known limitation rather than
fixed, since it hasn't yet been shown to matter against real queries.

### 10.3 Retrieval

`Tool - Vector Search` (sub-workflow, same pattern as the two structured
tools) embeds the incoming query, calls `match_product_embeddings` (a
Postgres RPC function, set explicitly as the node's Query Name — n8n
defaults to expecting a function called `match_documents`), and returns
the top 4 matches with their cosine similarity score. The score is
surfaced to the agent but, per §6, is not treated as a reliable
match/no-match signal on its own.

### 10.4 Why Supabase Hosts Both the Vector Store and Conversation Logging

`conversation_logs` was originally scoped for local Docker Postgres
(matching Project #1's pattern). Mid-build, the logging credential was
found already pointed at Supabase instead — discovered via a `relation
"public.conversation_logs" does not exist` error, not a deliberate
choice at the time. Rather than move it back, the call was made to keep
it there: `product_embeddings` already had to live in Supabase (pgvector
isn't available on the local Postgres image without extra setup), so
consolidating this project's AI-adjacent cloud data into one place —
rather than splitting it between local Docker and Supabase — was judged
simpler than correcting course. Project #1's invoice audit data stays on
local Docker Postgres; this is Project #2-specific. Full writeup in
`DEBUGGING_LOG.md` #32; security implications (and a real RLS gap this
decision's data ended up exposed to) in #33.

## 11. Future Improvements (current)

- Category-filter queries, product recommendations, web chat widget,
  business-verified Meta phone number (unchanged from §9)
- Scheduled or incremental sync for `product_embeddings`, replacing the
  manual full-resync pattern once catalog size or update frequency
  justifies the added complexity
- Enrich `pageContent` with product descriptions/use-case language to
  improve vector ranking quality (§10.2)
- Log `tool_called` per turn once n8n's AI Agent node exposes
  intermediate tool-call steps at the top level, or have each tool
  sub-workflow write a partial log row itself as a workaround
