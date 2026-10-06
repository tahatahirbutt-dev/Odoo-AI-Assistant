# Hosting Architecture — Odoo Automation Stack (Projects #1 and #2)

**Current state:** one Docker Compose stack running on a single Windows
laptop (Docker Desktop + WSL2), published to the internet through a
Cloudflare named tunnel, split across two hostnames. Both portfolio
projects run on this one stack:

- **Project #1** — Odoo Invoice Automation (Twilio WhatsApp delivery)
- **Project #2** — AI Inventory Assistant (Meta WhatsApp Cloud API + Gemini)

---

## 1. Topology

```
 Customer's phone ── WhatsApp ──► Meta Cloud API
                                      │  HTTPS POST /webhook/whatsapp-incoming
                                      ▼
                        Cloudflare edge
                        (DNS: hooks.tahatahirbutt.me)
                                      ▲
                                      │  outbound-only tunnel,
                                      │  held open by cloudflared
 ┌──────────────────── Host: Windows laptop ─────────────────────────────┐
 │  Docker Desktop (WSL2) — bridge network: invoice_network              │
 │                                                                       │
 │   cloudflared_tunnel ───────► n8n_invoice_automation  :5678           │
 │                                   │            │                      │
 │                                   │            └──► odoo_invoice_     │
 │                                   │                 automation :8069  │
 │                                   ▼                       │           │
 │                     postgres_invoice_automation :5432 ◄───┘           │
 │                     databases: odoo · n8n_db ·                        │
 │                     invoice_automation_logs                           │
 └───────────────────────────────────────────────────────────────────────┘
      │ outbound calls made by n8n
      ├──► Meta Graph API      (send WhatsApp replies)
      ├──► Google Gemini API   (LLM + embeddings)
      ├──► Supabase (Postgres + pgvector)
      │        ├── product_embeddings   (RAG fallback search)
      │        └── conversation_logs    (Project #2 audit log)
      └──► Twilio API          (Project #1 invoice delivery)

 Inbound to n8n from third parties, via the same tunnel:
      Meta   → /webhook/whatsapp-incoming   (Project #2 messages)
      Twilio → /webhook/invoice-pdf         (Project #1 media fetch)
```

---

## 2. Components

| Container | Image | Role | Host port | Persistent volume |
|---|---|---|---|---|
| `postgres_invoice_automation` | `postgres:16-alpine` | Postgres server for Odoo, n8n, and Project #1's audit log. Healthcheck: `pg_isready`. `init.sql` mounted for first-run schema. | 5432 | `postgres_data` |
| `odoo_invoice_automation` | `odoo:17.0` | ERP: products, stock, invoices. Started with `--database=odoo`. | 8069 | `odoo_data`, `odoo_addons` |
| `n8n_invoice_automation` | `n8nio/n8n:latest` | Workflow engine: webhooks, AI Agent, tools, logging. Postgres backend (`n8n_db`), `regular` execution mode. | 5678 | `n8n_data`, `n8n_files_data` |
| `cloudflared_tunnel` | `cloudflare/cloudflared:latest` | Outbound tunnel to Cloudflare; the only path from the internet to n8n. `depends_on: n8n`. | none | `./.cloudflared` (read-only) |

Startup order is enforced by `depends_on` with a Postgres health condition,
so Odoo and n8n don't start before the database accepts connections.

---

## 3. Databases

**Local, in the `postgres_invoice_automation` container:**

| Database | Used by | Notes |
|---|---|---|
| `odoo` | Odoo | Created by the Postgres image because `POSTGRES_USER=odoo` |
| `n8n_db` | n8n | Workflow definitions, credentials (encrypted), executions |
| `invoice_automation_logs` | Project #1 | `invoice_deliveries` audit table |

**External, on Supabase (managed Postgres + pgvector):**

| Table | Used by | Notes |
|---|---|---|
| `product_embeddings` | Project #2 (RAG fallback) | Catalog embedded via Gemini `gemini-embedding-001` |
| `conversation_logs` | Project #2 (audit log) | Originally scoped as a local database (`ai_assistant_logs`); moved here after discovering the n8n Postgres credential was already pointed at Supabase, which made `log_conversation` fail with "relation does not exist" against the empty local database. Decided to keep logging on Supabase alongside the vector store rather than move it back — one fewer moving piece, and both tables already sit next to each other for any future join between a conversation and the product it resolved to. |

Project #2's audit data is therefore **not** on the laptop at all — the
only local databases left are Odoo, n8n's own backend, and Project #1's
invoice log.

---

## 4. Request flows

### A. Inbound WhatsApp message (Project #2)
1. Customer messages the Meta business number.
2. Meta POSTs to `https://hooks.tahatahirbutt.me/webhook/whatsapp-incoming`.
3. Cloudflare's edge forwards it down the tunnel to `cloudflared`, which
   proxies to `http://n8n:5678` over the Docker network.
4. n8n's guard node checks the payload contains a real message (Meta also
   sends delivery-status events to the same URL).
5. The AI Agent calls its tools, which reach Odoo at `http://odoo:8069`
   using the service name — never `localhost`, which inside a container
   means the container itself. If `search_product` finds nothing, a
   vector-search fallback tool queries `product_embeddings` on Supabase.
6. The AI Agent's system prompt appends a status tag to its own output —
   `[STATUS:RESOLVED]`, `[STATUS:ESCALATED]`, or `[STATUS:CLARIFYING]` —
   which a `parse_agent_status` Code node strips before the reply is sent
   to the customer, exposing `resolution_status` separately for logging.
   This replaced an earlier approach that tried to infer resolution by
   string-matching the reply text for phrases like "couldn't find," which
   drifted as the LLM's phrasing varied.

### B. Outbound reply
`parse_agent_status` → `POST
https://graph.facebook.com/v21.0/{phone_number_id}/messages` with the
System User bearer token → Meta → customer. The turn, including
`resolution_status`, is then logged to Supabase's `conversation_logs`.

### C. Project #1 invoice delivery
A 2-hour schedule trigger polls Odoo, renders each invoice PDF, and calls
Twilio. Twilio fetches the PDF by calling back into
`/webhook/invoice-pdf` through the same tunnel, which re-renders it live
from Odoo — no PDFs are written to disk.

### D. Webhook verification (one-time, per hostname change)
Meta verifies a callback URL with a GET carrying `hub.challenge`. On
self-hosted n8n, a single Webhook node matches only one HTTP method per
path, and two separate Webhook nodes cannot share one path (see
`DEBUGGING_LOG.md` #16). The procedure is therefore:

1. Add a verification branch to the main workflow: `If` node checking
   `$json.query['hub.verify_token']` equals `$env.META_VERIFY_TOKEN`,
   true branch → `Respond to Webhook` node returning
   `{{ $json.query['hub.challenge'] }}`, false branch → a second
   `Respond to Webhook` node returning `403`.
2. Swap the webhook's HTTP Method to `GET`, and set its **Respond** mode
   to `Using 'Respond to Webhook' Node` (not `Immediately` — the
   challenge-echo requires the downstream node to control the response).
3. Publish → Deactivate → Activate (required — editing an active
   trigger's method doesn't take effect until this cycle; see
   `DEBUGGING_LOG.md` #17).
4. Register the callback URL in Meta and click Verify and Save.
5. Swap method back to `POST`, set **Respond** back to `Immediately`,
   disconnect the verification branch from the live path.
6. Publish → Deactivate → Activate again.

---

## 5. Public exposure

- **Tunnel type:** locally-managed named tunnel created with the
  `cloudflared` CLI (`tunnel login` → `tunnel create` → `tunnel route
  dns`). This avoids the Zero Trust dashboard's payment-method
  requirement. Routing lives in `config.yml`; the credentials JSON sits in
  `./.cloudflared/` (gitignored).
- **Direction:** `cloudflared` opens an *outbound* connection and holds it
  open. No inbound firewall ports, no port forwarding, no public IP.
- **Two hostnames, two audiences, same backend:**
  - `hooks.tahatahirbutt.me` — public. Meta and Twilio deliver webhooks
    here. No Cloudflare Access application, because those services cannot
    authenticate through a browser login.
  - `n8n.tahatahirbutt.me` — editor/admin only. Protected by a Cloudflare
    Access application ("n8n Editor") scoped to this hostname. One-time
    PIN to the owner's email; 24-hour session duration.
  Both hostnames route to the same `n8n:5678` container in `config.yml`.
  The split is enforced at the Cloudflare edge, not in the backend.
- **n8n URL settings:**
  ```
  N8N_HOST=n8n.tahatahirbutt.me
  N8N_PROTOCOL=https
  N8N_EDITOR_BASE_URL=https://n8n.tahatahirbutt.me/
  WEBHOOK_URL=https://hooks.tahatahirbutt.me/
  N8N_PROXY_HOPS=1
  N8N_SECURE_COOKIE=true
  ```
- **To verify periodically:** `config.yml`'s ingress should list only
  these two hostnames plus the catch-all 404 — Odoo and Postgres must not
  be reachable through the tunnel.

---

## 6. Configuration and secrets

| Layer | What lives there |
|---|---|
| `.env` (gitignored) | Source values for compose `${VAR}` substitution |
| `docker-compose.yml` → container env | `ODOO_USER`, `ODOO_PASSWORD`, `ODOO_URL`, `ODOO_DB`, `TWILIO_ACCOUNT_SID`, `PUBLIC_BASE_URL`, `META_VERIFY_TOKEN`, `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `WEBHOOK_URL`, `N8N_EDITOR_BASE_URL`, `N8N_PROXY_HOPS` |
| n8n credential store (encrypted at rest) | Gemini API key, Twilio Auth Token, Supabase Postgres connection (used for both `product_embeddings` and `conversation_logs`) |
| n8n workflow JSON (in git) | Only `{{$env.NAME}}` references, never values |

Rules that have each cost debugging time:
- `{{$env.*}}` inside nodes requires `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.
  The editor's "access to env vars denied" preview text is cosmetic;
  runtime resolves correctly.
- **Any env change needs the container recreated**
  (`docker-compose up -d n8n`, not just `restart`) before n8n picks it
  up. Saving or publishing a workflow does not reload the environment.
- n8n Variables (`$vars`) are Enterprise-only, so env vars are the
  substitute.
- Stale `.env` entries exist (`ODOO_DB=odoo_db`, unused `N8N_URL`);
  compose hardcodes the correct values for the affected services.
- A Postgres credential pointed at a different database than assumed
  fails loudly ("relation does not exist") rather than silently — this is
  how the `conversation_logs` → Supabase move was discovered in the first
  place. Worth checking which database a credential actually targets
  before assuming it matches the architecture doc.

---

## 7. Persistence and state

- Five named volumes hold local state on the laptop's Docker disk:
  `postgres_data`, `odoo_data`, `odoo_addons`, `n8n_data`,
  `n8n_files_data`.
- `docker-compose down -v` **permanently deletes those volumes**. Use
  `docker-compose down` for normal stops.
- Supabase (pgvector + `conversation_logs`) persists independently of the
  laptop and is unaffected by local Docker volume operations.
- Workflows live in two places: n8n's database (the running copy) and JSON
  exports in git. Exports are refreshed by hand — n8n's autosave does not
  update files on disk, so export before every commit.

---

## 8. Operations runbook

| Task | Command / rule |
|---|---|
| Start stack | `docker-compose up -d` |
| Check health | `docker ps` (postgres and n8n show `healthy`) |
| Stop, keep data | `docker-compose down` |
| Apply env change | `docker-compose up -d n8n` (recreates the container with new env) |
| n8n logs | `docker logs --tail 50 n8n_invoice_automation` |
| Tunnel logs | `docker logs --tail 30 cloudflared_tunnel` |
| Webhook smoke test | `curl -X POST https://hooks.tahatahirbutt.me/webhook/whatsapp-incoming ...` — expect `{"message":"Workflow was started"}` |
| Re-verify webhook with Meta | Swap `whatsapp_incoming` to GET, Respond to "Using Respond to Webhook Node" → Publish → Deactivate/Activate → verify in Meta → swap back to POST, Respond to "Immediately" → Publish → Deactivate/Activate |
| Check conversation log | Query Supabase's `conversation_logs`, not local Postgres |
| Multi-line SQL | Run in WSL, not `cmd.exe` (no heredoc support) |

Publishing rules: sub-workflow tools must each be **Published** or the
agent fails with "Workflow is not active." Meta's app must be in **Live**
mode or real webhooks are never delivered.

---

## 9. Gaps between this and production

| # | Gap | Impact | Fix |
|---|---|---|---|
| 1 | Hosted on a laptop | Down whenever it sleeps or is off; Project #1's 2-hour cron only fires while awake | Move to an always-on host |
| 2 | Dev credentials (Odoo `admin`/`admin`, Postgres password in compose) | Unacceptable on a public host | Secrets outside compose; dedicated Odoo API user with least privilege |
| 3 | Ports 5432, 8069, 5678 published to the host | Extra attack surface on a server | Drop `ports:` for internal services; only the tunnel/proxy needs n8n |
| 4 | ~~n8n editor reachable on the public hostname~~ | ~~Login page exposed to the internet~~ | **Closed.** Split into `hooks.tahatahirbutt.me` (public webhooks) and `n8n.tahatahirbutt.me` (Cloudflare Access protected editor). |
| 5 | No backups | Volume loss = total loss for local data; Supabase has its own backup posture, not yet reviewed | Scheduled `pg_dump` for local DBs plus `n8n_data`; confirm Supabase's backup/retention settings |
| 6 | No monitoring or alerting; healthcheck is only `n8n --version` | Silent failure (e.g. tunnel drops) | External uptime check on the webhook; n8n error-workflow alerts |
| 7 | Unpinned `latest` images | Surprise upgrades break workflows | Pin image versions |
| 8 | Secrets exposed outside the stack (tokens visible in files/screenshots shared into chat tools) | Compromise risk | Rotate Twilio Auth Token, Meta System User token, and verify token before going public |
| 9 | Free-tier LLM and Meta test number | ~2.7-13 s replies; test number only messages pre-verified recipients | Paid tier; business verification for a real number |
| 10 | Demo data only | Not a real client's Odoo | Client's own Odoo credentials per deployment |

**History:** a cloud VM was the first plan and was blocked — Oracle Cloud
signup was rejected at card verification, and Azure's smallest workable VM
(B2als_v2) was about $38/month. The Cloudflare tunnel was chosen as the way
to get a stable public HTTPS URL without one. That is why the stack still
runs locally.
