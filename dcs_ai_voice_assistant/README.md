# AI Voice Assistant for Odoo 20 Community

`dcs_ai_voice_assistant` · version 20.0.1.0.0 · by Devscodespace · LGPL-3

Talk to Odoo or type to it. The assistant answers questions from your real ERP data and prepares new records
and changes, which it only carries out after you confirm them. It runs as the signed-in user, so Odoo's access
rights, record rules and multi-company rules apply to everything it does. Every operation is written to an
audit log.

## Odoo 20 notes

Port of the 19.0 build; features, data model, security model and UI are the same. Changes:

* Access rights and record rules are in `security/ir.access.csv` (Odoo 20's merged format): group rows grant
  access (own records for users, all records read-only for AI administrators), rows without a group are the
  multi-company restrictions.
* System parameters read and written with Odoo 20's typed `get_str` / `set_str`.
* Frontend rewritten for OWL 3: the assistant is a global `Plugin` (`services.add`), components use
  `usePlugin`, `useProps`, `proxy` and `signal.ref()`, templates use `this.` and `t-out`.
* Icons are Material Symbols (`<i class="oi" data-icon="…"/>`); Font Awesome is not loaded in Odoo 20.
* Odoo 20 does not load demo data by default: use `--with-demo` if you want sample data to try the assistant.

---

## Features

| Area | What the assistant can do |
|---|---|
| **Voice** | Microphone button with listening, transcribing, thinking and speaking indicators; automatic stop after a pause; answers read aloud with pause/stop controls. Speech-to-text and text-to-speech run either in the browser (free) or through OpenAI on the server. |
| **Chat** | Chat window available on every screen (floating button and top-bar icon) and as a full-page app; message history; tables and record cards with clickable rows that open the Odoo record; suggestions; clear conversation. |
| **Questions** | Lists, lookups, totals, rankings and summaries: top customers, revenue by month, open pipeline, follow-ups, low/out-of-stock products, receivables, overdue invoices, monthly financial summary. |
| **Create** | Contacts (customers, vendors, companies), CRM leads/opportunities, products, sales quotations with lines, purchase orders/RFQs with lines, draft customer invoices/vendor bills, scheduled activities. |
| **Update** | Any whitelisted field of contacts, leads (stage, salesperson, expected closing date...), products, quotations, purchase orders and draft invoices; add/change/remove quotation and RFQ lines; confirm quotations and RFQs; post invoices; mark leads won/lost; inventory adjustments (stock managers). |
| **Safety** | Proposals with a preview (old → new values), duplicate warnings and a Confirm/Cancel card; the user can also just say "yes" or "no". Results are reported only after Odoo has committed them. |

The apps used by a question (CRM, Sales, Purchase, Inventory, Invoicing) are optional: the assistant only offers
what is installed **and** what the current user may access.

---

## Installation

1. Copy `dcs_ai_voice_assistant` into a folder on your Odoo addons path.
2. Make sure the Python package `requests` is installed (it is part of Odoo's standard requirements).
3. Restart Odoo, update the Apps list and install **AI Voice Assistant**.

Requirements: Odoo 20.0 Community (no Enterprise modules are used). Required modules: `base`, `base_setup`,
`web`, `mail`. Recommended: Contacts, CRM, Sales, Purchase, Inventory, Invoicing.

Microphone access requires the Odoo site to be served over **HTTPS** (or `localhost`); browsers block the
microphone on plain HTTP.

---

## Configuration

**Settings › General Settings › AI Voice Assistant** (also *AI Assistant › Configuration › Settings*):

| Setting | Notes |
|---|---|
| AI provider | **OpenAI (Responses API)** or **OpenAI-compatible (Chat Completions)** for OpenRouter, Groq, Mistral, Together, Azure OpenAI v1, a local Ollama/vLLM... The model must support tool (function) calling. |
| AI model | Default `gpt-5.6-luna`. Any current tool-calling model works. |
| API endpoint | Only for OpenAI-compatible providers, e.g. `https://openrouter.ai/api/v1`. |
| API key | Stored server-side in system parameters (administrators only), never sent to the browser. Leave the field empty to keep the saved key. **Test connection** checks key and model. |
| Temperature, max response tokens, conversation memory, language | Language "auto" answers in the user's language. |
| Voice commands | Speech-to-text: *Browser* (Web Speech API, Chrome/Edge/Safari) or *OpenAI transcription* (default model `gpt-transcribe`). Text-to-speech: *Browser* or *OpenAI speech* (model and voice selectable). A separate speech API key can be set when the AI provider is not OpenAI. |
| Text chat / Record creation / Record updates | Switch off any capability; disabled tools are not even offered to the model. |
| Floating button, read answers aloud, log data queries, retention, additional instructions | |

### Who can use it

Two groups under *AI Voice Assistant*: **User** (all internal users by default, through *Internal User*) and
**Administrator** (reads all conversations and the whole operation log). To restrict the assistant to some users,
remove the *AI Voice Assistant / User* implication from *Internal User* and assign the group explicitly.

---

## Usage

* Click the round button (bottom-right) or the microphone icon in the top bar, or open the **AI Assistant** app
  for a full-screen view.
* Type and press Enter, or click the microphone, speak, and pause; the request is sent automatically.
* When the assistant proposes a change, check the preview and click **Confirm**, or say/type "yes". Say "no" or
  click **Cancel** to drop it. A changed request ("yes but make it 5 units") goes back to the assistant, which
  prepares a new proposal. Unanswered proposals expire after 30 minutes.
* Click a row in any table to open the record.
* Use the speaker icon to have every answer read aloud; the play button under a message reads just that one.

### Sample prompts

**Sales** – "What are today's total sales?" · "Show me the top 10 customers by sales." · "Which products generated
the highest revenue this year?" · "Compare this month's sales with last month." · "Create a quotation for Azure
Interior with 5 Office Chairs and 2 Large Desks." · "Add 3 monitor stands to S00042." · "Confirm quotation S00042."

**CRM** – "How many open opportunities do we have?" · "Which leads need follow-up?" · "What is our total pipeline
value?" · "Show opportunities assigned to me." · "Create an opportunity 'Office refit' for Deco Addict worth
12,000." · "Change the stage of the XYZ opportunity to Qualified." · "Change the salesperson of this lead to
Ankit." · "Schedule a call with Gemini Furniture next Tuesday."

**Contacts** – "Create a new customer named ABC Technologies." · "Add a vendor called Steel Supplies, email
sales@steel.example." · "Update ABC Technologies' phone number to 9876543210."

**Purchase** – "Create a purchase order to Wood Corner for 20 desk pads." · "What is the status of P00012?" ·
"Which RFQs are still waiting for confirmation?"

**Inventory** – "Which products are running low on stock?" · "What is the current stock of Office Chair?" ·
"Which products are out of stock?" · "Set the on-hand quantity of Desk Pad to 50."

**Accounting** – "How much money do customers owe us?" · "Show overdue customer invoices." · "What is this month's
total revenue?" · "Generate a monthly financial summary." · "Create an invoice for ABC Technologies for ₹50,000
for consulting services."

### Example conversations (figures are illustrative)

```
You:  Show me the top 5 customers.
AI:   Here are your top 5 customers by confirmed sales. Gemini Furniture leads with $23,963 untaxed.
      [table: customer · orders · untaxed amount — rows open the customer]
You:  What about their outstanding invoices?
AI:   (reuses the same five customers) Two of them have open invoices, $4,150 in total …

You:  Create a new customer named ABC Technologies.
AI:   I can create the customer ABC Technologies. Should I proceed?
      [card: Create contact "ABC Technologies" · Name · Type: Customer · Confirm / Cancel]
You:  Yes.
AI:   ✓ Created contact "ABC Technologies".   [Open ABC Technologies]

You:  Create a quotation.
AI:   For which customer would you like to create the quotation?

You:  Update ABC Technologies' phone number to 9876543210.
AI:   I'll change the phone of ABC Technologies. Confirm?
      [card: Phone  — → 9876543210 · Confirm / Cancel]
```

---

## How it works

```
 browser (OWL)                         Odoo server (user's environment, never sudo for business data)
 ─────────────                         ────────────────────────────────────────────────────────────────
 mic ─► Web Speech API or ──► /dcs_ai/transcribe ─► speech provider
        MediaRecorder
 text ─────────────────────► /dcs_ai/chat ─► dcs.ai.executor
                                              │  1. store message
                                              │  2. pending proposal + "yes"/"no"?  ─► execute / cancel (no AI involved)
                                              │  3. model + tool definitions (only allowed, installed, permitted tools)
                                              │  4. tool calls ─► action_service (validated ORM operations)
                                              │        read tools  → real rows + display blocks
                                              │        prepare_*   → dcs.ai.action (pending), nothing written
                                              │  5. answer + tables/cards + proposals
 Confirm ──────────────────► /dcs_ai/action/decide ─► dcs.ai.action._execute()  (savepoint, as the user)
 speaker ◄── speechSynthesis or ◄── /dcs_ai/speak
```

* **No arbitrary code.** The model can only call the tools in `services/intent_service.py`. Objects are a
  whitelist (`OBJECTS` in `services/action_service.py`); fields are checked against the model metadata and the
  user's field-level groups; filters use a fixed operator set; writable fields are whitelisted per object;
  password/token-like fields and binary fields are refused.
* **Confirmation is enforced by the server.** Write tools only create a pending `dcs.ai.action`. Execution
  happens through the Confirm button or a plain yes/no reply that the server recognises itself, never because the
  model decided so. Before execution the proposal is re-checked (owner, not expired, record still exists) and run
  in a savepoint; the result message is generated from the actual outcome.
* **Figures do not pass through the model.** Tables and summary cards are built from database values and shown
  as-is; the model only writes the short commentary around them and is instructed never to invent numbers.

### Data model

| Model | Purpose |
|---|---|
| `dcs.ai.conversation` | One chat per user and company (history used as the model's memory). |
| `dcs.ai.message` | User/assistant messages, tool calls and tool results, display blocks. |
| `dcs.ai.action` | Audit log and proposals: user, company, operation, model, record, request text, preview, state (pending/done/failed/cancelled/expired), error, decision channel, timestamps. Users can read their own entries but cannot create, edit or delete them. |
| `dcs.ai.provider` | Configuration access and provider factory (abstract). |
| `dcs.ai.executor` | The execution engine (abstract). |

### Security summary

* All business reads and writes use the requesting user's environment; the module refuses to run in superuser
  mode. `sudo()` is used only to read the module's configuration (system parameters) and to write its own audit
  log entries, which are stamped with the acting user.
* Access rights, record rules, field groups and multi-company rules apply; the company selection of the web client
  is honoured and validated against the user's allowed companies.
* Conversations and operations are private per user (record rules); AI administrators can read all.
* API keys never leave the server; the settings form never sends the saved key back to the browser.
* Text inside records is treated as data; the model is told to ignore instructions found in record content.

### Privacy

Questions, conversation history and the data returned by tools are sent to the configured AI provider; recordings
are sent to the configured speech provider (or processed by the browser's speech service with the *Browser*
options). The OpenAI Responses API stores responses for chaining within one turn according to OpenAI's data
policy. Choose a provider and data agreement that fits your company's requirements. Nothing is sent to
Devscodespace.

---

## Extending

**Another AI provider** – subclass `BaseLLMProvider` in `services/ai_service.py`, implement `chat()` returning an
`LLMResponse`, decorate with `@register_provider`; it appears in the settings automatically.

**Another object** – add an entry to `OBJECTS` in `services/action_service.py` (model, label, default fields, and
optional `create`/`update` whitelists). Search, totals, details, create and update work immediately.

**Another tool** – write a function decorated with `@tool("name", write=...)` in `action_service.py`, add its JSON
schema in `intent_service.build_tools()`, and for write tools a branch in `execute_action()`.

---

## Tests

```
odoo-bin -d <db> -i dcs_ai_voice_assistant --test-tags /dcs_ai_voice_assistant --stop-after-init
```

46 tests cover: provider request/response formats and error mapping (HTTP mocked), the yes/no classifier, every
read tool, proposals and execution (contacts, leads, quotations, updates, duplicates, ambiguity, expiry, failures),
the full executor loop with a scripted model (confirmation by voice reply and by button, follow-up context, bounded
tool loop, provider errors, "model claims success" without an executed action), access rights, record rules,
multi-company isolation, audit-log immutability, settings and the JSON routes. Tests that need an optional app
are skipped when it is not installed.

---

## Limitations

* The quality of understanding depends on the chosen model; small local models may pick tools less reliably.
* Browser speech recognition is not available in Firefox; use the OpenAI transcription option there.
* Summaries add amounts in the company currency (`*_signed` fields); per-currency totals are reported for sales.
* Records with lot/serial tracking cannot be adjusted by voice; use Inventory.
* Proposal summaries and result messages are generated in English; the model's answers follow the user's language.
