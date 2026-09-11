# Technical Journal — AI Inventory Assistant (WhatsApp + Odoo + Gemini)

This log documents the real debugging process behind Project #2: an AI agent
that answers live stock/pricing questions over WhatsApp, grounded in Odoo
inventory data. As with Project #1, it's included deliberately — the bugs
below, and how they were diagnosed, are a more honest picture of building
production automation than the finished workflow diagram alone.

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
