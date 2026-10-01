# Technical Journal — AI Inventory Assistant (WhatsApp + Odoo + Gemini)

This log documents the real debugging process behind Project #2: an AI agent
that answers live stock/pricing questions over WhatsApp, grounded in Odoo
inventory data. As with Project #1, it's included deliberately — the bugs
below, and how they were diagnosed, are a more honest picture of building
production automation than the finished workflow diagram alone. Covers the
original agent/tools/memory build, the Twilio → Meta Cloud API migration,
and the later addition of a Supabase pgvector semantic search fallback
(`vector_search`) plus a pre-launch security and data-integrity review.

## Summary

Built an n8n AI Agent (Google Gemini) with two custom tools —
`search_product` and `get_product_details` — that query a live Odoo catalog
via JSON-RPC. Wired the agent to WhatsApp through Twilio, added phone-keyed
conversation memory, and logged every turn to PostgreSQL. The build surfaced
bugs across five different layers of the stack: n8n's own webhook URL
construction, Odoo's query semantics, n8n's AI Agent tool architecture,
Twilio's WhatsApp-specific field requirements, and a silent data-loss bug in
n8n's Set node — each with a distinct root cause worth documenting on its
own.

## Bugs found and fixed (chronological)

### 1. n8n Variables are an Enterprise-only feature
**Symptom:** Settings → Variables showed "Upgrade to unlock variables" instead
of an input form, on n8n Community Edition.
**Cause:** Variables (`$vars.*`) are gated behind an Enterprise license;
Community/self-hosted-free doesn't include them.
**Fix:** Reused the environment-variable pattern already proven in Project #1
(`{{ $env.* }}` via `docker-compose.yml`, with
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`) instead — added `ODOO_URL` and
`ODOO_DB` alongside the existing `ODOO_USER`/`ODOO_PASSWORD` env vars.
**Lesson:** Don't assume n8n UI features shown in docs/tutorials are
available on Community Edition — check tier gating before designing an
architecture around a feature.

### 2. Stale `.env` value for `ODOO_DB`
**Symptom:** `.env` had `ODOO_DB=odoo_db`, but the Odoo container is actually
started with `--database=odoo` (confirmed both by `docker-compose.yml` and by
the seed script only working when `ODOO_DB=odoo` was exported manually).
**Cause:** Leftover placeholder value from an early template, never
corrected, silently never read by anything that mattered until this project.
**Fix:** Hardcoded the correct value (`ODOO_DB: "odoo"`) directly in
`docker-compose.yml`'s `n8n` service rather than trusting the `.env`
passthrough for this specific variable.
**Lesson:** A wrong `.env` value can sit undetected indefinitely if nothing
exercises that code path — worth auditing `.env` against actual running
container config periodically, not just once at setup.

### 3. Literal `=` typed into an expression field
**Symptom:** `Invalid URL: =http://odoo:8069/jsonrpc` — n8n treated the whole
string, including a leading `=`, as the literal URL.
**Cause:** Manually typed `=` before an expression, mimicking n8n's internal
expression-mode marker, which n8n doesn't need or want typed by hand.
**Fix:** Removed the leading `=`; typed only `{{ $env.ODOO_URL }}/jsonrpc`.
**Lesson:** n8n's `{{ }}` syntax is sufficient on its own — don't add a `=`
prefix manually, that's an internal implementation detail, not user syntax.

### 4. Odoo's `ilike` does substring matching, not word matching
**Symptom:** Searching `"blue shirt"` against a product named
`"Blue T-Shirt Small"` returned zero results.
**Cause:** `ilike` checks whether the *entire query string* appears as a
contiguous substring. `"blue shirt"` never appears verbatim in
`"blue t-shirt small"` — the `t-` breaks the match.
**Fix:** Rebuilt the search domain in a Code node to split the query into
words and require each word to independently match somewhere in the product
name (AND of substrings), OR the whole query matching SKU/barcode. Verified
against both single-word ("mouse") and multi-word ("blue shirt", "red shirt
medium") queries.
**Lesson:** Naive substring search fails on any query with word order or
insertions the stored name doesn't share verbatim — worth testing multi-word
queries explicitly, not just the easy single-word case.

### 5. HTTP Request Tool can't run multi-step logic
**Symptom:** Attempted to wire `search_product` directly into the AI Agent
as a plain "HTTP Request Tool" node.
**Cause:** The real `search_product` behavior is three steps
(`build_search_domain` Code node → Odoo HTTP call → `rank_results` Code
node), not one. A bare HTTP Request Tool node can only run itself — it can't
invoke Code nodes before/after. The same problem applied to
`get_product_details`, which needs `normalize_product_details` after it to
handle Odoo's `false`-as-empty sentinel.
**Fix:** Rebuilt both tools as standalone n8n workflows (with an "Execute
Workflow Trigger" defining input parameters), and wired them into the AI
Agent using **Call n8n Workflow Tool** nodes instead of plain HTTP Request
Tool nodes.
**Lesson:** Any tool with pre/post-processing logic needs the sub-workflow
tool pattern, not a single HTTP node — decide this at the architecture stage,
not after wiring the wrong node type.

### 6. Tool sub-workflows not Active/Published
**Symptom:** `Workflow is not active and cannot be executed` when the AI
Agent tried to call `Tool - Search Product`.
**Cause:** A saved workflow is not automatically callable by another
workflow — n8n requires it to be explicitly Active (or, in this
project-scoped instance, Published) before external Execute Workflow calls
are allowed.
**Fix:** Published both `Tool - Search Product` and `Tool - Get Product
Details` individually.
**Lesson:** Same "saved ≠ live" distinction as Production vs Test webhook
URLs in Project #1 — publishing/activation is a required, separate step,
easy to forget when a workflow "looks done" in the editor.

### 7. `product_id` type mismatch between agent and sub-workflow
**Symptom:** `Received tool input did not match expected schema — Expected
string, received number → at product_id`.
**Cause:** The AI Agent correctly extracts product IDs as numbers, but the
`Tool - Get Product Details` sub-workflow's input field was left as type
String by default.
**Fix:** Changed the Execute Workflow Trigger's `product_id` input field type
from String to Number, then republished.
**Lesson:** Sub-workflow input schemas need their types matched deliberately
against what the calling agent will actually send — don't leave defaults
unchecked.

### 8. Doubled port in `N8N_URL` (recurrence of a Project #1 bug class)
**Symptom:** Browser console showed repeated failed requests to
`localhost:5678:5678` (doubled), and the chat trigger silently failed to
register any execution at all — no error, just nothing happening.
**Cause:** `.env`'s `N8N_URL=http://localhost:5678` fed into
`N8N_HOST: ${N8N_URL:-localhost}` in `docker-compose.yml`, so `N8N_HOST`
became a full URL instead of a bare hostname — n8n then appended `:5678`
onto it again internally when constructing chat/webhook URLs. Same failure
signature as Project #1 Bug #1, but triggered via a different variable this
time (`N8N_URL`/`N8N_HOST` rather than `WEBHOOK_URL` directly).
**Fix:** Changed `.env`'s `N8N_URL` to a bare hostname (`localhost`, no
protocol or port), then `docker-compose restart n8n`.
**Lesson:** This class of bug — an env var expecting a bare value receiving
a full URL instead — can recur through any variable feeding into
`N8N_HOST`/`WEBHOOK_URL`, not just the ones fixed the first time. Worth
auditing every URL-shaped env var after any `.env` edit, not just the one
that broke before.

### 9. Gemini free-tier quota `limit: 0` on `gemini-2.0-flash`
**Symptom:** `429 Too Many Requests` with `limit: 0` for the specific model,
despite a working API key and enabled project.
**Cause:** Not a project/account misconfiguration — `gemini-2.0-flash`
appears to have aged out of the current free-tier model lineup as Google
rolled newer Flash generations forward through 2026. A `limit: 0` on one
specific model name, with the project otherwise healthy, indicates the model
itself isn't in the current free allocation, not a broken setup.
**Fix:** Switched to `gemini-2.5-flash`, which resolved immediately.
**Lesson:** Free-tier LLM API model lineups shift over time; a working
credential/project doesn't guarantee every model name is still
free-tier-eligible. Confirmed via the Chat Model node's own dropdown rather
than assuming.

### 10. Real rate-limit throttling during rapid manual testing
**Symptom:** `429 Too Many Requests`, `limit: 20`, with a short retry delay
given, hit repeatedly during back-to-back test messages.
**Cause:** Genuine free-tier RPM throttling — worth noting that a single
conversational turn with tool-calling costs *multiple* LLM calls internally
(one to decide whether to call a tool, another to compose the final reply
after the tool result returns), so quota drains faster than the number of
messages sent would suggest.
**Fix:** Waited for the quota window to clear; no config change needed. Not
a bug — an expected constraint of the free tier under rapid testing.
**Lesson:** Budget real cooldown time between rapid manual test batches, and
document this constraint honestly for anyone considering the free tier for
a live deployment.

### 11. Groq account provisioning error (external, unresolved)
**Symptom:** `console.groq.com` returned a persistent `"signup error"` with
a trace ID, across multiple retries.
**Cause:** Appears to be a known, recurring issue on Groq's backend
(corroborated by other users reporting the identical error across different
browsers/accounts), not something caused locally.
**Fix:** Switched to Google Gemini API instead of continuing to troubleshoot
a third-party outage. The switch cost only the Chat Model sub-node — no
other part of the architecture (tools, grounding prompt, memory, logging)
needed to change, which validated the "LLM provider is swappable" design
principle from the architecture doc in practice, not just in theory.
**Lesson:** When a third-party service's own signup flow is broken, the
efficient move is switching providers rather than extensively debugging
infrastructure outside your control — especially when the architecture was
deliberately designed to make that swap cheap.

### 12. Twilio `To` field double-prefixed with `whatsapp:`
**Symptom:** `Bad request... The 'To' number whatsapp:+923190704800 is not a
valid phone number.`
**Cause:** The `To` field's expression manually prepended `whatsapp:`, while
the node's own "To WhatsApp" toggle was also enabled — the toggle appears to
apply its own prefix, producing a doubled `whatsapp:whatsapp:+...` value
that Twilio correctly rejected. (Visually indistinguishable from a
well-formed number in the UI preview, only caught by comparing behavior
after removing the manual prefix.)
**Fix:** Removed the manual `whatsapp:` prefix from the `To` expression,
left the "To WhatsApp" toggle to handle it alone.

### 13. Twilio `From` field required a manual `whatsapp:` prefix
**Symptom:** After fixing #12, a *new* error on the same node:
`The 'From' number whatsapp:+14155238886 is not a valid phone number.`
**Cause:** The "To WhatsApp" toggle does **not** apply to the `From` field —
only `To`. `From` is a static field that needs the `whatsapp:` prefix typed
explicitly regardless of the toggle state.
**Fix:** Manually set `From` to `whatsapp:+14155238886`.
**Lesson:** n8n's Twilio node's "To WhatsApp" toggle is asymmetric — it only
affects `To`, not `From`. Not documented clearly in the node UI; only
confirmed by testing both fields independently after the first fix didn't
fully resolve the error.

### 14. `start_timer` Set node silently stripped the WhatsApp payload
**Symptom:** After inserting a `start_timer` node (for response-time
logging) between `whatsapp_incoming` and the AI Agent, both the Simple
Memory node and the AI Agent itself started failing — Memory with
`Key parameter is empty`, referencing a missing `body.WaId`.
**Cause:** n8n's Set node, by default, outputs *only* the fields explicitly
defined on it — it does not pass through the rest of the incoming payload
unless "Include Other Input Fields" is enabled. Adding `start_timer` (which
only defined `start_time`) silently dropped `body.Body` and `body.WaId` from
everything downstream.
**Fix:** Enabled "Include Other Input Fields" on the `start_timer` node, so
the full original Twilio payload passes through alongside the new
`start_time` field.
**Lesson:** Any Set/Edit Fields node inserted mid-chain for a side purpose
(timers, flags, computed fields) needs "Include Other Input Fields" enabled
by default unless the intent is genuinely to discard the rest of the
payload — otherwise it silently breaks every downstream node that expects
the original data shape. This is an easy, quiet bug: the workflow didn't
error at the point of insertion, only later, in unrelated-looking nodes.

### 15. Windows `cmd.exe` doesn't support bash heredoc syntax
**Symptom:** `<< 'EOF' was unexpected at this time`, followed by every SQL
line being interpreted as an invalid standalone command.
**Cause:** Heredoc (`<< 'EOF' ... EOF`) is bash/POSIX shell syntax; Windows
`cmd.exe` has no equivalent and doesn't understand it.
**Fix:** Ran the same command in the WSL Ubuntu terminal instead, where
heredoc syntax works natively — consistent with using WSL for the rest of
the Docker/git workflow in this project.
**Lesson:** Multi-line SQL/heredoc commands must run in WSL, not native
Windows cmd — worth defaulting to WSL for any Docker `exec` command
involving more than a single-line `-c` argument.

## Environment / operational notes

- **Free-tier LLM response time**: end-to-end WhatsApp reply latency
  averaged ~20-25 seconds on Gemini's free tier during testing, driven by
  multiple sequential LLM round-trips per turn (tool-routing decision +
  final composition) plus two Odoo JSON-RPC calls. Documented as a known
  limitation — a paid-tier model or faster provider would reduce this
  meaningfully for a production deployment.
- **Odoo external API is stateless**: unlike the session-cookie pattern
  needed for PDF report rendering in Project #1, standard JSON-RPC
  `execute_kw` calls require `db`/`uid`/`password` on every single call —
  there is no persistent login session to optimize or reuse.
- **`tool_called`, `matched_product_id`, `match_score` are not logged in
  v1**: n8n's AI Agent node (this version) doesn't natively expose which
  tools were called or their results at the top level — only the final
  `output` text. Rather than reverse-engineer this from reply text
  (unreliable), these three columns are left `NULL` in `conversation_logs`,
  honestly reflecting a real data gap rather than faking a value. Logging
  them properly would require either a newer n8n Agent node version with
  intermediate-steps support, or having the tool sub-workflows write partial
  log rows themselves — noted as a Phase 2 improvement.
- **Twilio WhatsApp Sandbox**: 5 outbound messages/day cap (trial account),
  and sandbox "join" membership lapses after inactivity and must be
  re-established with `join <keyword>` — both constraints from Project #1
  applied equally here and shaped how testing was paced.

## Architecture decisions worth naming explicitly

- **Sub-workflow tools over inline HTTP nodes**: chosen specifically because
  both tools need pre/post-processing logic (domain-building, result
  ranking, null-sentinel normalization) that a single HTTP Request Tool node
  cannot run. This does add per-call overhead (extra workflow invocation),
  a real and accepted tradeoff for correctness.
- **LLM provider swappability validated in practice, not just claimed**: the
  Groq outage forced an actual mid-build provider switch to Gemini. Only the
  Chat Model sub-node changed — tools, grounding prompt, memory, and logging
  were all untouched. This is genuine evidence for a claim worth making to
  clients, not just an architectural intention.
- **Honest NULLs over fabricated data**: `tool_called`, `matched_product_id`,
  and `match_score` are left NULL rather than populated with guessed or
  derived-from-text values, consistent with the documentation-honesty
  standard set in Project #1 (e.g., `match_score` vs. LLM `confidence`).
## Migration: Twilio Sandbox → Meta Cloud API (Direct)

This section documents migrating Project #2's WhatsApp channel off Twilio's
Sandbox (5 messages/day, 72-hour rejoin requirement — unworkable for a real
demo or client trial) onto Meta's official WhatsApp Business Cloud API
directly. This was a substantially harder migration than Project #1's
original Twilio setup: different payload shape, different auth model,
different webhook lifecycle, and one root cause that produced hours of
misleading symptoms before being correctly identified.

### 16. n8n cannot run two Webhook trigger nodes on the same path

**Symptom:** `{"code":0,"message":"Unused Respond to Webhook node found in
the workflow"}` when a GET-verification webhook and the live POST webhook
were both wired to the identical URL path.
**Cause:** Self-hosted n8n's routing engine does not cleanly support two
separate Webhook trigger nodes sharing one path, even with different HTTP
methods (GET vs POST) — despite this appearing to be a reasonable pattern
from the docs.
**Fix:** Single Webhook node, method temporarily swapped to GET only for
the one-time Meta verification handshake, then swapped back to POST for
live traffic. The GET-verification branch (token check + challenge-echo)
was disconnected afterward rather than kept live in parallel.
**Lesson:** Don't assume multi-trigger-same-path patterns from
documentation work identically on self-hosted Community Edition — verify
empirically before building around them.

### 17. Editing an active trigger's settings doesn't take effect until deactivate/reactivate

**Symptom:** After changing `whatsapp_incoming`'s HTTP Method from GET back
to POST and publishing, a direct curl POST to the production URL still
returned `404 — not registered for POST requests`, even though the editor
and exported JSON both correctly showed `POST`.
**Cause:** n8n registers a webhook's live listener at workflow
activation time. Changing a trigger node's parameters on an
already-active workflow and saving does not force n8n to tear down and
re-register the listener with the new settings — the old registration
persists underneath the new-looking configuration.
**Fix:** Fully deactivate the workflow, then reactivate (or
Unpublish → Publish) to force a clean re-registration matching current
settings.
**Lesson:** Any change to a trigger's core registration parameters (method,
path) on a live workflow needs a deactivate/reactivate cycle to actually
take effect — a simple save/publish is not sufficient.

### 18. `If1` guard: array vs string type mismatch silently misrouted real messages

**Symptom:** Executions either errored (`"Wrong type: '[object Object]' is
an object but was expecting a string"`) or silently routed real incoming
messages to the dead-end `No Operation` branch instead of the AI Agent.
**Cause:** The guard checked
`{{ $json.body.entry[0].changes[0].value.messages }}` with a **String**
"is not empty" comparison, but `messages` is an **array**, not a string.
Under strict type validation this either threw an error or evaluated
incorrectly depending on execution context.
**Fix:** Rebuilt the condition as a numeric check:
`{{ $json.body.entry[0].changes[0].value.messages ? $json.body.entry[0].changes[0].value.messages.length : 0 }}`,
type **Number**, operator **larger than 0**.
**Lesson:** Don't compare an array-shaped field against a string operator
just because "is not empty" sounds generically applicable — check the
actual data type the expression evaluates to.

**A tempting but broken alternative, tried and rejected:** stringifying the
whole payload and checking `.includes("messages")` as a supposedly
type-agnostic shortcut. This doesn't work — Meta's status-callback events
(sent/delivered/read receipts) also carry `"field": "messages"` at the
outer webhook envelope level even though they contain no real customer
text, so a substring check like this would let delivery receipts through
to the AI Agent just as readily as real messages. The array-length check
above is the correct guard; the stringify shortcut is a false fix that
happens to look like it works until a status callback actually arrives.

### 19. `assistant_reply`/`resolved` read from the wrong node after inserting the Meta send step

**Symptom:** `null value in column "resolved" of relation
"conversation_logs" violates not-null constraint`.
**Cause:** After replacing the Twilio node with an HTTP Request node
calling Meta's Graph API directly, `log_conversation`'s field mappings
still referenced `{{ $json.output }}` — which now meant "the output of the
immediately preceding node" (the HTTP Request node, which returns Meta's
API response metadata), not the AI Agent's reply text.
**Fix:** Changed references to explicitly name the source node:
`{{ $('AI Agent').item.json.output }}`.
**Lesson:** Inserting a new node into an existing chain can silently break
any downstream `$json` reference that assumed a specific previous node's
output shape — grep for bare `$json` references after restructuring a
pipeline, don't just trust that "it points at the last node" is still what
you want.

### 20. The real root cause: Meta withholds live webhook delivery while the app is Unpublished

**Symptom:** Everything appeared correctly configured — GET verification
succeeded, `messages` field showed Subscribed, manual curl POSTs to the
production webhook URL worked end-to-end — but real WhatsApp messages sent
from a phone never produced an n8n execution, even though Meta's own
"Check test webhooks" dashboard showed the message was received.
**Cause:** Meta's own documentation states it plainly, easy to miss on a
busy config page: *"Apps will only be able to receive test webhooks sent
from the app dashboard while the app is unpublished. No production data...
will be delivered unless the app has been published."* The "Check test
webhooks" panel only shows what Meta received from the phone — it is not
proof of delivery to your server. With the app in Development/Unpublished
mode, Meta deliberately does not forward real message webhooks to any
external URL, regardless of how correctly that URL is configured.
**Fix:** Switched App Mode from Development to Live (App Settings →
Basic). For a WhatsApp-only integration this required three baseline
fields to be completed before the toggle would allow it: a Category
selection, a Privacy Policy URL (a public GitHub repo URL satisfies this
for a portfolio project), and a 1024×1024 App Icon — no formal App Review
was required beyond that.
**Lesson:** When real end-user traffic silently fails to arrive despite
every technical layer checking out — DNS resolves, TLS handshake succeeds,
manual synthetic requests work, the tunnel is healthy, the receiving
platform's own dashboard shows the message was received — check the
sending platform's *publish/release* state before assuming the bug is
somewhere in your own stack. Several hours were spent chasing plausible
infrastructure theories (proxy headers, trailing slashes, WAF rules,
cookie settings) that were all consistent with "something isn't working"
but none of which actually explained "zero requests reach my edge at all"
as precisely as "the sender was never actually instructed to send."

### False leads chased and ruled out (for the record)

Worth naming these explicitly rather than pretending the debugging path
was direct — a few hours were spent on theories that had surface
plausibility but didn't hold up against the evidence once actually
checked:

- **`N8N_TRUST_PROXY` / `X-Forwarded-For` warnings** — real log noise, but
  cosmetic; unrelated to webhook delivery failing.
- **`WEBHOOK_URL` trailing slash** — ruled out by the fact that GET
  verification and manual curl POSTs both succeeded through the exact same
  URL before this was "fixed."
- **Cloudflare WAF blocking Meta's IPs** — directly disproven by checking
  Cloudflare's own Security Events log: zero firewall events, and more
  tellingly, zero requests of any kind reaching the edge at Meta's
  reported delivery timestamps — which pointed toward Meta never sending,
  not Cloudflare blocking.
- **Cookie/session strictness (`N8N_SECURE_COOKIE`)** — only affects the
  n8n UI's own browser session, irrelevant to incoming webhook POSTs.

The lesson generalizes: when several independently-plausible
infrastructure fixes all fail to change the observed symptom, that's a
signal to step back and check the sender's own send-conditions rather than
continuing to iterate on receiver-side configuration.

## RAG / Semantic Search Integration (Phase 2 — Supabase pgvector)

This section covers adding a `vector_search` tool on top of the existing
`search_product`/`get_product_details` pair — a semantic fallback for
queries that don't exact/substring-match anything in Odoo (synonyms,
natural-language descriptions, "something to make my office quieter"). The
architecture decision from the start was fallback, not replacement: the
two structured tools stay as the first line, `vector_search` only runs
when they don't resolve something cleanly. This section also covers a
later pre-launch review pass — running a 30-query golden dataset against
the system surfaced several issues that normal manual testing hadn't,
including two real Supabase security gaps neither manual review nor the
build-phase bugs below had caught.

### 21. Supabase "table created without RLS" warning — dismissed, later proven wrong

**Symptom:** Creating `product_embeddings` triggered a Supabase dialog:
*"This query creates a table without enabling Row Level Security. Clients
using anon or authenticated keys may be able to access
product_embeddings."*
**Cause (as understood at the time):** n8n's backend connects to Supabase
using the service_role key, which bypasses RLS entirely — reasoned that
since nothing else (no frontend) talks to this table, RLS was unnecessary
overhead.
**Fix (at the time):** Clicked "Run without RLS."
**This was wrong, and was corrected later — see #33.** The reasoning
missed that Supabase grants `anon`/`authenticated` roles table access by
default on new tables in the `public` schema. n8n's own connection
bypassing RLS says nothing about whether *other* clients holding the
project's public anon key can reach the table directly via the REST API.
Supabase's own Security Advisor flagged this as Critical once it was run
properly — not a theoretical gap, a real one.
**Lesson:** "Nothing else connects to this today" is not the same claim as
"nothing else *can* connect to this" — the first is a statement about
current usage, the second is what RLS actually controls. Don't use the
former to justify skipping the latter.

### 22. n8n Supabase credential: "couldn't connect with these settings"

**Symptom:** Saving the Supabase credential in n8n failed with "The
resource you are requesting could not be found."
**Cause:** The Host field was populated with the full REST API URL
(`https://<project-id>.supabase.co/rest/v1/`).
**Fix:** Changed Host to the base project URL
(`https://<project-id>.supabase.co`) — n8n appends `/rest/v1/` itself.
**Lesson:** Same class of mistake as a URL-shaped env var getting a value
that already includes a path/protocol it doesn't need — matches the
`N8N_HOST` doubling pattern from bugs #1/#8, just in a credential field
instead of an environment variable.

### 23. Embedding dimension mismatch — table built for 768, model outputs 3072

**Symptom:** The Supabase Vector Store node failed to insert data.
**Cause:** `product_embeddings` was created with `vector(768)`.
`gemini-embedding-001` (the actual embedding model in use) natively
outputs 3072 dimensions, and n8n's Embeddings Google Gemini node doesn't
expose a setting to reduce that.
**Fix:** Dropped and recreated `product_embeddings` and
`match_product_embeddings` with `vector(3072)`. Verified directly against
the table afterward (`SELECT vector_dims(embedding) FROM
product_embeddings LIMIT 1;` → `3072`) rather than trusting the fix was
correct just because the insert stopped erroring.
**Lesson:** Adapt the schema to the model's actual output, not the other
way around — and verify the dimension directly from the database once,
rather than inferring it from a summary of what was done. A later review
pass flagged this exact number as worth double-checking before trusting
an evaluation built on top of it; the direct query is what actually
closed the question.

### 24. Supabase Vector Store node requires an explicit Document sub-node

**Symptom:** The vector store insert failed during the sync workflow with
a missing-connection error.
**Cause:** n8n's LangChain vector store nodes require a connected Document
sub-node (to shape raw JSON into an embeddable document) in addition to
the Embeddings sub-node — only the Embeddings connection had been wired.
**Fix:** Added a Default Data Loader node, Type of Data → JSON, Mode →
Load Specific Data, Data mapped to `{{ $json.pageContent }}`, with a
`product` metadata field mapped to `{{ $json.metadata }}`.
**Lesson:** n8n's LangChain nodes are strict about required sub-node
connections — a node that looks fully configured in the canvas can still
be missing a mandatory wire that only surfaces as a runtime error, not a
save-time validation.

### 25. Custom RPC function name not recognized — PGRST202 + schema cache

**Symptom:** `Error searching for documents: PGRST202 Could not find the
function public.match_documents(filter, match_count, query_embedding) in
the schema cache.`
**Cause:** Two separate issues stacked: n8n's Supabase Vector Store node
defaults to calling a function named `match_documents`, but the actual
function was named `match_product_embeddings`; and even after setting the
correct name, PostgREST caches the database schema and didn't pick up the
function immediately.
**Fix:** Set **Query Name** explicitly to `match_product_embeddings` in
the node's options, then ran `NOTIFY pgrst, 'reload schema';` in Supabase
to force a cache reload.
**Lesson:** Custom-named RPC functions need to be told to n8n explicitly —
it won't infer the name. Schema-cache staleness recurred later in a
completely different client (#36) — worth treating as a general pattern
with Supabase/PostgREST: anything that changes the schema shape may need
an explicit cache invalidation, not just a successful `CREATE`/`ALTER`.

### 26. Vector Store node's output shape assumed wrong — metadata nesting

**Symptom:** `vector_search` returned 4 items, but every field was empty
or placeholder: `name: "Unknown"`, `list_price: 0`, `category:
"Uncategorized"`.
**Cause:** The Supabase Vector Store node wraps its output in a
`document` object; the parsing code was reading `doc.metadata.product`
directly instead of `doc.document.metadata.product`.
**Fix:** Updated the Code node to check both shapes defensively:
`const doc = json.document || json;` before reading metadata.
**Lesson:** Inspect the raw JSON output of an n8n LangChain node before
writing code against it — the shape differs depending on whether the node
is called directly or through an agent, and guessing the structure from
documentation or memory produced two wrong guesses in a row (this bug,
and #27 below, which the defensive code above didn't actually catch).

### 27. `similarity` field silently zero — defensive fallback masked a wrong key

**Symptom:** Every result from `vector_search` reported `similarity: 0`,
including results with a correct and sensibly-ordered product list.
**Cause:** The fix for #26 checked `json.similarity || doc.similarity ||
0` — but the actual field Supabase's node returns is `score`, at the top
level, next to `document`, not nested and not named `similarity` at all.
Both checked paths were always `undefined`, so the `|| 0` fallback won on
every single call. Routing still worked by coincidence of result
ordering, which is exactly why this went unnoticed through normal manual
testing — the agent was picking the right product, just not using the
score to do it.
**Fix:** Replaced the field name (`json.score`) and replaced `|| 0` with
`?? null`, so a future mismatch produces a visible `null` instead of a
value that looks like a legitimate (terrible) match score. Verified by
running the node in isolation and confirming real, correctly-ordered
similarity values (e.g. 0.767 → 0.700 → 0.697 → 0.628) before trusting it
again.
**Lesson:** A `|| 0` fallback on a numeric metric field is a fail-silent
antipattern — it doesn't just hide a bug, it produces a plausible-looking
wrong value (zero reads as "bad match," not as "field missing"), which is
worse than an error. This wasn't caught by executing real conversations
and eyeballing the replies; it was only caught by a deliberate pre-launch
check of the raw node output specifically because an evaluation was about
to be built on top of this number.

### 28. The fix for #27 wasn't actually live — sub-workflow edited but not republished

**Symptom:** After applying and verifying the #27 fix via "Execute step"
on the Code node directly, a real end-to-end WhatsApp conversation still
showed `similarity: 0` on every result.
**Cause:** "Execute step" on a node inside the editor runs the current
*draft* of that workflow. The AI Agent calls whatever version of
`Tool - Vector Search` is currently Published — a separate, later action
from saving a node's edit. The edit had been saved but the sub-workflow
itself had not been re-published, so the live call still executed the
pre-fix code.
**Fix:** Published `Tool - Vector Search` again (not just the main
workflow), then re-verified through a real execution from the main agent,
not just the sub-workflow's own test output.
**Lesson:** This is the same "saved ≠ live" gap as bug #6, but a new
variant of it: #6 was a sub-workflow that had never been published at
all; this is a sub-workflow that *was* published once, then edited later
without re-publishing. Verifying a fix by testing the node directly inside
its own editor is not the same as verifying it through the actual caller —
the only test that counts is the one that goes through the real call path.

### 29. `search_product` substring false positives — "bin" matching inside "cabinet"

**Symptom:** `search_product("bin")` returned 4 results with identical
`match_score: 1` — Large Cabinet, Pedal Bin, Cabinet with Doors, Desk
Combination — when only Pedal Bin was a real match.
**Cause:** Odoo's `ilike` domain filter does substring matching with no
word-boundary awareness. "bin" is a literal substring of "ca**bin**et" and
"com**bin**ation."
**Fix:** An initial proposed fix suggested a Postgres word-boundary regex
(`~* '\mquery\M'`) applied at the database level — **this was wrong**:
`search_product` queries Odoo through a JSON-RPC `search_read` domain, not
raw SQL, and Odoo's domain language doesn't expose a regex operator to
drop that into. The actual fix was applied in `Tool - Search Product`'s
`rank_results` Code node instead: Odoo still does the broad substring
fetch (cheap, no schema change), and the Code node now requires a
whole-word match (`\bword\b`) against the product name before a result is
kept, with an exact SKU/barcode substring match still scoring 1
regardless of the name text. Verified by re-running `search_product("bin")`
and confirming exactly one result.
**Lesson:** A fix has to match the actual query mechanism, not just the
symptom — "this looks like a job for regex" doesn't specify *where* the
regex can actually run. Odoo's domain language and raw Postgres are not
interchangeable, even though both ultimately execute against the same
database.

### 30. Sync workflow had no delete-before-insert — duplicate embeddings on re-sync

**Symptom:** None yet observed directly — caught during pre-launch review,
before it could cause a real duplication incident.
**Cause:** `Sync Odoo Products to Supabase`'s Supabase Vector Store node
runs in `mode: "insert"`, with no upsert key and no table-clearing step.
Re-running the sync (e.g. after editing product data in Odoo) would insert
a second full copy of every product rather than replacing the first.
**Fix:** Added a Postgres node (`Clear Old Embeddings`, running `DELETE
FROM product_embeddings;`) at the start of the workflow, before the Odoo
fetch. Verified by running the sync twice in a row and confirming the row
count stayed at the correct catalog size with no duplicate `odoo_id`s.
**Lesson:** A full-resync workflow with no delete-before-insert isn't
broken on its first run — it's a bug waiting for whatever makes someone
run it twice. Worth fixing before that second run happens by accident
during a demo, not just reactively after it does.

### 31. Catalog seed data had duplicate-named products — broke disambiguation

**Symptom:** None — caught by inspecting the synced catalog directly
during pre-launch review, not by a visible failure.
**Cause:** Three products shared the literal name "Customizable Desk"
(different SKUs, same price) and two shared "Conference Chair" (same
price). The agent's disambiguation rule ("list the matching product names
and ask the customer to clarify") has no real answer when two of the
listed names are identical text — there's nothing for the customer to
clarify *with*.
**Fix:** Renamed the five products in Odoo to distinguish them
(`Customizable Desk (Compact/Standard/Executive)`, `Conference Chair
(Black/Grey)`), then re-ran the sync to pick up the new names.
**Lesson:** Golden-dataset or eval review isn't just for catching code
bugs — it surfaces seed-data quality problems that unit-level testing of
individual tools would never catch, because each tool was working
correctly; the catalog itself was the problem.

### 32. `resolved` column relied on brittle string-matching

**Symptom:** None directly observed yet, but confirmed by inspection: the
original logic was `!output.includes("couldn't find") &&
!output.includes("team member")` — any LLM phrasing drift ("I don't have
that," "not in our catalog," "our team will reach out") would silently
flip `resolved` to `true` for a genuinely unresolved conversation.
**Cause:** Resolution status was being inferred from the customer-facing
reply text instead of being stated directly by the agent.
**Fix:** Added a line to the system prompt instructing the agent to append
a machine-readable tag (`[STATUS:RESOLVED]` / `[STATUS:ESCALATED]` /
`[STATUS:CLARIFYING]`) to the end of every reply, and a `parse_agent_status`
Code node that extracts the tag into a `resolution_status` field and
strips it from the text the customer actually receives. `log_conversation`
now reads `resolution_status` directly instead of pattern-matching prose.
Verified across all three states plus one deliberate edge case (an
out-of-stock product correctly tagged `RESOLVED`, not `ESCALATED`, since
the agent found the product and gave a real answer).
**Lesson:** Don't infer structured data from prose meant for a human
reader when the thing producing the prose can just emit the structured
data directly — a regex against LLM output is chasing a moving target
(phrasing varies with temperature); a tag the model is instructed to emit
doesn't have that problem.

### 33. Supabase Security Advisor caught two real gaps manual review missed

**Symptom:** Running Supabase's built-in Security Advisor (Database →
Advisors) during pre-launch review surfaced four findings, two of them
Critical/relevant: "RLS Disabled in Public" on `product_embeddings`, and
"RLS Policy Always True" on `conversation_logs`.
**Cause (conversation_logs):** Its RLS policy was `CREATE POLICY ... FOR
ALL USING (true)` with no `TO service_role` clause — in Postgres, a
policy with no `TO` applies to `PUBLIC`, i.e. every role, not just the
intended one. Combined with Supabase's default grants, this meant
`anon`/`authenticated` keys had unrestricted read/write on customer phone
numbers and message content despite RLS showing "enabled."
**Cause (product_embeddings):** RLS had never been enabled at all — see
#21, where this was dismissed as unnecessary.
**Fix:** Dropped and recreated `conversation_logs`'s policy scoped `TO
service_role`, revoked `anon`/`authenticated` grants on both tables,
enabled RLS on `product_embeddings` with the same scoped policy, and
separately pinned `match_product_embeddings`'s `search_path` to `public`
(a third, lower-severity Advisor finding — prevents the function from
being tricked into resolving an unqualified table name against a
different schema). All changes captured in `SQL/002_security_and_logging_fixes.sql`.
Verified with `SET ROLE anon; SELECT * FROM conversation_logs;` — now
correctly errors instead of returning rows.
**Lesson:** "RLS enabled" and "RLS restrictive" are different claims — a
policy can exist, show green in the dashboard, and still grant everyone
access if its `TO` clause is missing. Worth running Supabase's own
Advisor as a standing check, not just trusting that writing a policy at
all was sufficient; this is also the direct, concrete reversal of the
conclusion drawn in #21, written up there rather than quietly fixed.

### Non-bug observations worth documenting

- **LLM keyword extraction often does the semantic bridging before
  `vector_search` ever fires.** A customer message like "throw away
  tissues without touching the lid" never contains the word "bin," but
  Gemini extracted "bin" as the `search_product` query term itself —
  meaning many queries that look like they'd need the vector fallback
  don't actually reach it, because the LLM already closed the gap. Worth
  confirming which tool actually fired (via the sub-node's input panel,
  not just the final reply) before labeling any test case a "vector search
  success" or "structured search success."
- **Vector ranking is shallow because the embedded text is minimal.** Each
  product is embedded as `"Product: X | Category: Y | SKU: Z"` — no
  description, no use-case language. A query like "organize my pens and
  pencils on my desk" ranked an out-of-stock "Desk Stand with Screen"
  above a more genuinely useful "Cable Management Box," because the
  embedding has nothing to work with beyond the word "desk." Not fixed —
  documented as a known limitation; the real fix (enriching `pageContent`
  with descriptions) is deferred until it's shown to matter in practice.
- **Vector similarity scores are not a reliable match/no-match signal on
  their own.** A query with no real catalog match ("something to charge my
  phone") still returned scores of 0.55–0.65 against genuinely unrelated
  products. The agent's grounding rules in the system prompt — not a
  similarity threshold — are what prevent a mediocre vector match from
  becoming a hallucinated answer. Any future similarity-based filtering
  logic needs a threshold validated against real queries, not assumed from
  the score's general shape.
- **Per-query latency runs 11–25s**, driven by a ~350-word system prompt
  plus three tool descriptions plus a 6-message memory window being
  resent on every turn, on top of the actual tool-calling round trips.
  Measured directly from execution logs rather than estimated. A 30-query
  evaluation run costs roughly 180k tokens at this rate — worth knowing
  before running one, not discovering mid-run.

### Evaluation-process mistakes caught before they reached conclusions

Worth naming these the same way the Meta migration's false leads are named
above — they're genuinely part of how this was debugged, not just the
parts that worked:

- **Tool invocation was initially judged by eyeballing the trace diagram
  at default zoom**, which can visually hide the connection line between
  the AI Agent and a tool that was, in fact, called. This produced an
  incorrect conclusion that `vector_search` hadn't fired in a test where
  it actually had. Fix: always open the specific sub-node and check its
  Input panel directly — never infer tool invocation from the diagram's
  appearance alone.
- **A "couldn't find" reply was initially assumed to mean the vector
  fallback never ran**, without checking what the tool actually returned.
  In fact `vector_search` had fired, returned four candidates, and the
  agent had correctly judged none of them a real match — the right
  behavior, not a routing failure. Fix: before labeling any no-match reply
  a bug, check what every tool that ran actually returned, not just the
  final text.
