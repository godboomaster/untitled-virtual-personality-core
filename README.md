# Virtual Persona Core

**English** · [Русский](README.ru.md)

A platform for "living" characters: bots that remember the person they talk to, live their
own lives between messages and message you first. A persona is a YAML file with a
character, memory and a set of modules. The same persona works in Telegram and in the web
interface, can remind you of things, keep your to-do list and teach you, and — at the
owner's request — drive their browser.

Replies come from any OpenAI-compatible model with an API key, from an LLM web chat in the
bot's browser (DeepSeek, Qwen, Claude, ChatGPT and more) or from a local model in Ollama —
so the bot can run without any API keys at all.

Backend — Python (FastAPI, ChromaDB, python-telegram-bot), web — React (`web/`), tray
panel — Tauri (`desktop/`). The main platform is macOS; Windows is supported but less
tested.

https://github.com/user-attachments/assets/aa87b619-6564-4703-81d8-762ad478f123

## What it can do

- **Telegram and web.** In Telegram — private chats and groups (by a trigger word or a reply
  to the bot's message), commands, long code is sent as files. On the web — chats with all
  personas, dossier, the persona's room, settings, a persona editor, voice mode.
- **Memory.** Recent chat messages, facts about the person (the model extracts them by
  itself, merges duplicates and resolves contradictions), a chat dossier, the persona's own
  diary, files with full-text search.
- **Life between messages.** A persona has a mood, energy, an activity and a place, its own
  world with people and storylines, and events while you are silent. When you come back,
  you'll hear what it has been up to.
- **Initiative and daily rhythm.** The persona writes first when there is a reason, and
  learns what you reply to. It says good morning, sends you to bed at night, warns you about
  rain.
- **Intelligence tier.** A human answers an everyday question like a human, not with an
  assistant's wall of text; a forest spirit answers with a gesture. This is set by the tier:
  `primitive`, `normal`, `bot`.
- **Household.** Reminders ("tomorrow at 12", "every Friday at 6 pm"), a to-do list,
  inventory, courses with quizzes, web search.
- **Control mode.** On the owner's command the bot opens sites and apps, searches on sites,
  clicks buttons, types text, reads pages, carries out multi-step tasks ("order a pizza")
  and replays recorded scenarios. Actions are confirmed.
- **No keys needed.** LLM web chats in the bot's browser or a local model in Ollama.
- **Add-ons.** Example — Arrodes: a persona that knows a book, with hybrid search over its
  text.

## Quick start

You need Python 3.11+ and, for the web, Node.js 20.19+ or 22.12+. Web chats and control mode
need a Chromium-based browser (Chrome, Edge, Brave, Opera, Yandex Browser, Vivaldi). Ollama
is optional. `requirements.txt` installs exact tested package versions on Linux, Windows
and macOS 14+ on Apple Silicon. Intel Macs and macOS 13 have no builds of these torch and
onnxruntime versions.

```bash
git clone <repo-url> virtual-persona-core
cd virtual-persona-core

python3 -m venv .venv
source .venv/bin/activate                    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .

cp .env.example .env                         # empty template of personal settings
python -m app.main api                       # backend: http://127.0.0.1:8000
```

The web interface — in a second terminal:

```bash
cd web
npm ci
npm run dev                                  # http://localhost:5173
```

A fresh install chooses nothing for you: no keys, no primary provider, no models, and the
only persona is the test persona Connor. Open "Settings" in the web, add a provider key and
enter a model — a provider without a model is not used. The same can be set in `.env`:
`<PROVIDER>_API_KEY` and `<PROVIDER>_MODEL`.

**Telegram.** Create a bot with [@BotFather](https://t.me/BotFather), put the token into
`.env` as `<PERSONA>_BOT_TOKEN` (for example, `CONNOR_BOT_TOKEN`) and run
`python -m app.main connor` or `python -m app.main all`.

**The tray panel** (macOS and Windows) starts, restarts and stops the backend and the web,
shows their status and a live log. Build instructions — in
[desktop/README.md](desktop/README.md).

**Docker** — `docker compose up -d --build`, details in the [Docker](#docker) section.

## Configuration

Secrets and personal settings live in `.env` (not in git). Non-secret defaults live in
`.env.config` (in git). A value from `.env` wins over `.env.config`, and a process
environment variable wins over both. Much can be changed from the web: "Settings" holds
provider keys, time zone and location; a persona's dossier holds its modules.

Replies need at least one source: a provider with a key and a model, a web chat, or Ollama
with a chosen and downloaded model. There are no default models — a human picks them.

| Variable | What it sets |
|---|---|
| `<PROVIDER>_API_KEY` | provider key: `ZAI`, `OPENAI`, `ANTHROPIC`, `GROQ`, `DEEPSEEK`, `KIMI`, `GOOGLE`, `MIMO`, `HF`. Spare keys — `_API_KEY_1`, `_API_KEY_2`… |
| `<PROVIDER>_MODEL` | the provider's model; without it the provider is not used |
| `ACTIVE_PROVIDER` | primary provider; empty or without a key — the first provider with a key and a model |
| `<PERSONA>_BOT_TOKEN` | the persona's Telegram bot token (YAML file name in upper case) |
| `OWNER_USER_ID` | the owner's Telegram ID: owner commands and control mode |
| `TIMEZONE` | the user's time zone (IANA, e.g. `Europe/Berlin`); empty — the system one |
| `WEBCHAT_SITES` | web chats in order, e.g. `deepseek,qwen` |
| `OLLAMA_URL`, `OLLAMA_MODEL` | local model: address (`http://localhost:11434`) and model, e.g. `gemma4:e2b` |
| `LOCAL_LLM_BACKEND` | who does the internal work (classification, extraction): `ollama` or `webchat` |
| `LTM_MODEL_PROVIDER` | a separate provider for fact extraction; empty — the persona's chain |
| `API_HOST`, `API_PORT` | backend address: `127.0.0.1:8000` |
| `API_TOKEN` | API password; the web asks for it on sign-in. Empty — no password |
| `API_CORS_ORIGINS`, `API_ALLOWED_HOSTS` | whose pages and which host names may reach the API — see [API](#api) |
| `VPC_DATA_DIR` | folder for data and your personas, `data/` by default |
| `RATE_LIMIT_DEFAULT`, `RATE_WINDOW` | Telegram message limit (6 per 3600 s) for personas with `rate_limit` |

Everything else is in `.env.example`, with comments.

### Where the data lives

Your personas — in `data/personas/`. A persona's memory in Telegram — in `data/<persona>/`,
on the web — in `data/api_<persona>/`: these are different conversations and different
memories. The same folders hold the persona's life state, diary, reminders, to-dos and the
control mode log (`computer_control/audit.jsonl`). The `data/` folder never goes into git.

The backend logs to the terminal; on the web the log is visible in developer mode. When the
panel starts the bot, the log is also kept in `logs/` for 14 days.

## Running

```bash
python -m app.main             # menu: personas with tokens, all bots, API
python -m app.main api         # backend for the web and the panel — no Telegram tokens needed
python -m app.main connor      # Telegram bot of one persona
python -m app.main all         # Telegram bots of all personas that have a token
BOT_TARGET=all python -m app.main
```

The web and Telegram are separate processes: run either one on its own or both at once.

On Windows the same command also manages the bot's browser windows: agent windows are
visible only in control mode, the rest stay off-screen. To turn this off — `VPC_WIN_WINDOWS=0`.

### Telegram

In a private chat the bot answers everything. In a group — replies to its own messages and
messages that start with a word from `trigger_words` (the persona id by default). Messages
that arrived while the bot was off are skipped.

| Command | What it does |
|---|---|
| `/start`, `/help` | greeting and a list of features; `/start` in a private chat starts the conversation over, like `/clear` |
| `/clear` | clear this chat's history: the messages and the LLM web chat thread (in a group — admins) |
| `/stats`, `/last N` | memory counters; the last N messages of the chat |
| `/reset`, `/forget <text>` | forget all your facts; forget one fact |
| `/relations` | who is who in the chat |
| `/ratelimits` | rate limit status |
| `/ltm_privacy [smart\|strict]`, `/ltm_export` | what to remember about you; your facts as a file in a private chat |
| `/web` | web search in this chat on/off (`web_search`) |
| `/todo`, `/add_todo` | to-do list (`todo`) |
| `/remind`, `/reminders`, `/cancel_reminder` | reminders (`reminder`) |
| `/inventory`, `/add_inventory` | inventory (`inventory`) |
| `/learn <topic>`, `/stop_learning` | a learning course (`learning`) |
| `/files`, `/reset_files` | uploaded files (`file_upload`) |
| `/erase N`, `/context`, `/resetall`, `/reset_diary` | owner only: erase the last N messages, the prompt context as a file, all memory, the diary |

In parentheses — the persona module the command needs. Commands go through the same filters
as messages: blocks, limits, moderation, muting.

### Web interface

- **Start** — an introduction and a control mode command cheat sheet.
- **Home** — a calendar and a "while you were away" feed: initiatives, diary, reminders.
- **Chat** — all chats as a list, then the chat with a persona, voice mode, skins.
- **Room** — the persona's live scene: where it is, what it is doing, its things;
  a picture-in-picture window.
- **Personas** — create (form with the YAML next to it), edit YAML, rename, copy, color.
- **Skins** — looks for the chat, dossier and room: library, upload, generation.
- **Settings** — language (ru/en), notifications, developer mode, location, time zone,
  provider keys, web chats.
- **Persona dossier** — memory, reminders and to-dos, initiative, control mode, learning,
  files, persona settings (models, generation, modules).

The web finds the backend through `VITE_API_URL` (`http://127.0.0.1:8000` by default). If
`API_TOKEN` is set, the web asks for it on sign-in. Without a backend you can continue on
demo data after 3 seconds — the interface switches to live data by itself once the backend
is up.

## Personas

The project ships one built-in persona — the test persona **Connor**
(`app/personas/connor.yaml`): an RK800 android, a students' assistant, tier `bot`, with
control mode. It has no providers or models of its own and answers with whatever is chosen
in "Settings". The Arrodes add-on (a separate repository) adds the `arrodes` persona.

Your personas live in `data/personas/` and never go into git. A persona there with the same
id as a built-in one overrides the built-in one.

The reply language follows the language of the person; the language of background messages
follows the language of `system_prompt`.

### Creating a persona

- **On the web:** "Personas" → "Create". The form is on the left, the resulting YAML on the
  right.
- **As a file:** put `<id>.yaml` into `data/personas/`. The web sees it right away; a
  Telegram bot needs a `<ID>_BOT_TOKEN` token and a restart.

A persona is any YAML with a non-empty `system_prompt`. The id is the file name: Latin
letters, digits, `_` and `-`, up to 64 characters. Reserved: `tg`, `default`, `skins`,
`skin_gen`, `settings_probe`, `persona_drafts`, `personas` and anything starting with `api_`.

Everything changed from the web is written to `data/personas/`. The first edit of a built-in
persona creates its copy there — the file in `app/personas/` does not change. A built-in
persona cannot be deleted (it can be muted or copied); deleting the copy resets the persona
to the built-in version.

Edits apply to the web bot immediately, except for a few keys — for those the web says that
a restart is needed. A Telegram bot is a separate process: it sees only muting (`muted`)
right away, everything else after a restart.

### Persona YAML

```yaml
id: helper
name: Helper
description: A polite assistant
color: '#38b6a5'                 # color on the web; without it — from the palette by id
stm_size: 50                     # how many recent chat messages to remember verbatim

system_prompt: |
  You are a helpful assistant. Answer briefly and to the point.

settings:
  temperature: 0.7
  max_tokens: 2000
  top_p: 0.9

intellect:
  tier: bot                      # primitive | normal | bot

features:
  trigger_words: [helper]        # trigger words in groups, in lower case
  web_search: true
  file_upload: true
  self_memory: true              # the persona's diary
  todo: true
  reminder: true
  inventory: true
  learning: true
  proactive: true                # initiative
  rhythm: true                   # morning, night, weather
  life: true                     # life between messages

llm:
  primary: groq                  # a provider, local, webchat or webchat:<site>
  fallback: [deepseek, local]
```

Without `features` a persona keeps conversation and memory. Top-level keys:

| Key | What it sets |
|---|---|
| `id`, `name`, `description`, `color` | name, description, color on the web |
| `system_prompt` | the character — the only required field |
| `settings` | `temperature`, `max_tokens`, `top_p`, `split_messages` (reply in several messages) |
| `stm_size` | size of the recent message buffer; `STM_SIZE` from `.env.config` by default |
| `special_users` | special people: `id` (`${VARIABLE}` works), `aliases`, `greeting`, `behavior` |
| `intellect` | intelligence tier — see below |
| `conversation_style` | how often a reply ends with a question — see below |
| `llm` | the persona's models — see [Models](#models) |
| `world_binding` | `real_world` or `fictional_universe`; without the key the model decides from `system_prompt` |
| `room` | things, a pet and places in the persona's room |
| `start_greeting`, `pre_reply_text` | Telegram: the `/start` text (`{name}` — the person's name); a phrase before a long reply |
| `max_docs`, `max_file_size_mb` | uploaded file limits: 3 documents, 10 MB |

Modules in `features`:

| Key | What it enables |
|---|---|
| `owner` | the owner's Telegram ID; without it — `OWNER_USER_ID` |
| `trigger_words`, `allowed_dm_users`, `blocked_users` | triggers in groups; who may write in private (empty — everyone); a block list |
| `rate_limit`, `moderation`, `punish_block` | message limit, moderation, blocking via the `[PUNISH:BLOCK]` marker (Telegram only) |
| `web_search` | internet search; memory and files take priority |
| `file_upload` | document upload (docx, pdf, pptx, xlsx and more) with full-text search |
| `self_memory` | the persona's diary: conversation episodes and notes |
| `todo`, `reminder`, `inventory` | to-dos, reminders, inventory |
| `learning` | "teach me X" courses; as a dict — `quiz_every`, lesson intervals |
| `proactive` | initiative — see below |
| `rhythm` | morning greeting, "time to sleep", weather |
| `life` | life between messages; per layer — `state_engine`, `world_lore`, `external_stimuli` |
| `muted` | muting: the persona stays silent, reminders don't arrive |
| `light_context` | a trimmed prompt for weak models; turns on by itself with a local primary model |
| `computer_control` | control mode — see [Control mode](#control-mode) |
| `addons` | the persona's add-ons, e.g. `[arrodes_book]` |
| `export_server`, `restore_memory` | Telegram: HTTP memory export by token; restore from a dump when memory is empty |

### Intelligence tier

```yaml
intellect:
  tier: normal                   # primitive | normal | bot
  overrides:                     # for unusual personas
    self_memory_mode: null       # none | primitive | full
    world_lore_enabled: null
    help_response_style: null    # action_only | casual_human | full_assistant
```

| | `primitive` | `normal` | `bot` |
|---|---|---|---|
| Who | an animal, a spirit, a simple robot | a human | high intelligence |
| A request for help | an action or a gesture, no explanations | short, everyday | a full breakdown: calculations, code, follow-up questions |
| Diary | flashes of impressions | full | full |
| A world with people and storylines | no | yes | yes |

`normal` is not "dumber" than `bot` — it is a different way of helping. Without an
`intellect` block the tier rules don't apply at all.

### Questions at the end of a reply

Models love to end every reply with "And you?". The platform limits this:

```yaml
conversation_style:
  question_frequency: rare       # none | rare | natural | frequent
```

The default is `rare`: a question only when it matters, and never in two replies in a row.
A reply over the limit is rewritten once. `natural` and `frequent` are for personas whose
character is to ask questions.

### Initiative

```yaml
features:
  proactive:
    enabled: true
    check_interval_minutes: 30
    silence_threshold_minutes: 180   # silence after which the persona may write
    initiative_probability: 0.3
    max_daily_initiatives: 5
    initiative_hours: "09:00-23:00"  # window; without it — around the clock
    adaptive_threshold: true         # threshold follows how often the person writes
    feedback_enabled: true           # more of what gets replies
```

The persona writes first when there is a reason: it remembered a conversation, a task, an
event of its own life, or wants advice. The hours window and the threshold can be changed in
the dossier, "Initiative" tab.

### Daily rhythm

```yaml
features:
  rhythm:
    enabled: true
    morning_greeting: {window_start: 5, window_end: 12, min_gap_hours: 4}
    sleep_nudge: {bedtime_hour: 0, active_within_minutes: 120}
    weather_alerts: {rain_lead_hours: 3, temp_delta_c: 8}
```

In the morning — a greeting when you sit down at the computer or write for the first time
that day. At night — "time to sleep", but only if you were active recently. Weather
(Open-Meteo) — if a location is set in "Settings".

### Life between messages

```yaml
features:
  life:
    enabled: true
    tick_interval_minutes: 20    # how often the state changes
    events_per_day: [1, 3]
    max_active_storylines: 2
```

The state (mood, energy, activity, place) changes by itself; the persona's world — people,
places, storylines — grows out of `system_prompt` and conversations. Frequent draft steps are
done by the local model, text for the person — by the main one, in the persona's voice.
Without Ollama the state changes by simple rules.

![The persona's room: day turns into night, the persona goes about its business](https://github.com/user-attachments/assets/30a20272-bc16-4841-b893-3c5c22122983)

A persona from a made-up world (`fictional_universe`) never sees the internet. A persona from
the real world (`real_world`) can get `external_stimuli` — news on its interests.

## Control mode

The bot works with the owner's browser and apps. The mode is turned on per chat and turns
itself off after 30 minutes of inactivity; a bot restart does not reset it.

![Control mode: "order a pizza" — the agent builds the order and stops before payment](https://github.com/user-attachments/assets/386e6a3d-9ac5-4714-8799-2ed87aa278ef)

- **Enable for a persona:** dossier → "Control mode" or `features.computer_control` in YAML.
- **Enter:** "enter control mode".
- **Exit:** "exit control mode".

While the mode is on, reminders, to-dos, inventory and learning stay silent so they don't
compete with commands.

**Who can.** Only the owner (`owner` or `OWNER_USER_ID`) and `allowed_users` from the YAML.
On the web the user is the owner, but commands from a skin are never executed. For everyone
else there is no control mode: their messages go into ordinary conversation.

**What it can do:**

| | Example |
|---|---|
| sites and apps | "open youtube", "launch …" |
| search on a site | "play interstellar on youtube", "search for … on <site>" |
| clicks and hover | "click download", "hover over the menu" |
| typing and keys | "type coffee into the search field", "send", "press space" |
| tabs and scrolling | "close the … tab", "go back", "scroll to drinks" |
| player | "pause", "quieter", "mute", "open the second result" |
| reading | "what's on the page?", "send a screenshot", "read the page" |
| downloads | "download the report" |
| tasks | "order a pizza" or "task: …" — the agent takes the steps itself, asks you to choose, never presses pay |
| scenarios | "remember this scenario as pizza order", later — just "pizza order" |
| browser | "fix the browser" — show the bot's window to sign in to a site or pass a captcha |

The full cheat sheet in English and Russian is on the web, in the "Start" section.

**Confirmation.** Before an action the bot asks; the answer is "yes" or "no". Only the person
who asked can confirm; the question lives for 5 minutes, for keys — one minute.
`confirm: false` removes the questions, but payment, deletion, typing into password, email
and phone fields, and actions suggested by the model itself are always confirmed. "Stop"
cancels.

**Privacy.** On sign-in, payment and banking pages and on sites from `private_hosts`,
screenshots and page text go neither to cloud models nor to web chats. Passwords, codes,
cards, emails and phone numbers are stored masked in the log and are never written into
scenarios — the bot asks for them on replay. The log is
`data/<context>/computer_control/audit.jsonl`; old entries are cleaned by
`python -m scripts.scrub_cc_audit`.

**Browser.** The bot starts its own Chromium with a separate profile and does not touch your
usual browser. There are two windows:

- **visible** (port 9222) — for commands; starts when you enter the mode and shuts down
  after 20 minutes of inactivity;
- **hidden** (port 9223) — for LLM web chats and search.

You need to sign in to the web chats once in the bot's profile: say "fix the browser" or
press the button in the panel — the hidden window becomes visible. If connecting to Chromium
fails, macOS has a fallback via AppleScript and Safari. On Linux the mode works partially.

```yaml
features:
  computer_control:
    confirm: true
    allowed_users: []              # besides the owner
    idle_exit_min: 30              # 0 — never turn off
    allow_domains: []              # empty — any http(s)
    private_hosts: []              # plus built-in sign-in and payment signs
    sites:                         # quick names: "open youtube"
      youtube: youtube.com
    search:                        # "play X on youtube": search template and first result
      youtube:
        url: https://www.youtube.com/results?search_query={q}
        first: /watch\?v=[\w-]{11}
    apps:                          # "launch notes"; a string or per OS
      notes: Notes
      chrome: {darwin: Google Chrome, win32: chrome}
    tasks:                         # named commands: shell or recipe:<id>
      music: {darwin: 'shortcuts run "Music"'}
    scenarios: true
    task_agent: true
```

Values from `tasks` run in a shell. So access to the API is access to the computer: keep the
backend on `127.0.0.1` and set `API_TOKEN` if you expose it to a network.

## Models

### API providers

All of them go through an OpenAI-compatible API: `zai`, `openai`, `anthropic`, `groq`,
`deepseek`, `kimi`, `google`, `mimo`, `hf`. Each has a key `<PROVIDER>_API_KEY` and a model
`<PROVIDER>_MODEL`; a custom API address — `<PROVIDER>_BASE_URL`, the official one by
default.

There are no default models. The model is entered in "Settings", in the "model" field under
the provider key, with suggestions from a list. A provider with a key but no model is marked
"no model" and is not used. A persona can have its own provider model: dossier → "Provider
models".

If a provider doesn't answer, the bot tries the next one. The order: the primary provider,
the persona's `llm.fallback`, the other providers with keys in the order of the list above,
web chats, the local model. Without internet the bot goes straight to the local model.
Several keys of one provider are rotated, starting with the last working one.

### Web chats

The prompt goes into a chat on the site, and the answer is read from the page. Sites:
`deepseek`, `qwen`, `claude`, `zai`, `chatgpt`, `kimi`, `google` (AI Mode), `duckai` (no
account).

```env
WEBCHAT_SITES=deepseek,qwen
```

or per persona:

```yaml
llm:
  primary: webchat:deepseek
  webchat_mode: {deepseek: headless}   # headless | hidden | headed
```

Each site gets one persistent chat per channel: sites treat batches of fresh chats as spam.
An answer takes 10–60 seconds, without streaming. Sites don't welcome automation.

Automation — task agent steps and background work — writes to a site no more than once
every 20–30 seconds and no more than 60 messages an hour. The count is shared by all
personas, because they use the same account. Replies to your own messages don't wait.
Configured in `.env`: `WEBCHAT_AUTO_GAP_SEC`, `WEBCHAT_AUTO_JITTER_SEC`,
`WEBCHAT_AUTO_PER_HOUR`. A chat that has already seen the task agent's rules and an unchanged
page gets a short reference to them instead of the full text.

### Local model

```bash
ollama pull gemma4:e2b
```

Then enter the model in "Settings" (the Ollama row) or in `OLLAMA_MODEL` — without it Ollama
is not used. Ollama does the internal work: classification, drafts of the persona's life,
intent parsing. A persona with `llm.primary: local` answers entirely with the local model,
in a lightweight mode.

### Persona models

```yaml
llm:
  primary: deepseek                # provider | local | webchat | webchat:<site>
  fallback: [groq, webchat, local]
  exclude: [hf]                    # never use these
  models: {groq: llama-3.1-8b-instant}
```

## API

`python -m app.main api` starts FastAPI on `http://127.0.0.1:8000`. All endpoints with
descriptions are in Swagger: `/docs`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | the backend is alive (no password) |
| GET, POST | `/api/personas` | list of personas; create a persona from YAML |
| GET, PUT | `/api/personas/{p}/yaml` | the persona's YAML |
| POST | `/api/chat`, `/api/chat/stream` | a message to a persona; with streaming (SSE) |
| GET | `/api/chat/history` | chat history |
| POST | `/api/chat/clear` | erase the persona's memory (a copy is kept for 7 days) |
| GET | `/api/personas/{p}/inbox` | background messages: reminders, lessons, initiative |
| GET | `/api/personas/{p}/state` | the persona's state and its life feed |
| GET | `/api/providers` | providers, keys, the active one |
| GET | `/api/system/status`, `/api/logs` | the bot's browsers; the log — for the panel and developer mode |

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"persona": "connor", "message": "Hi!"}'
```

**Whoever is on the web is the owner.** That is why the API is closed to other pages:

- by default it listens only on `127.0.0.1`;
- it accepts requests only from its own pages (`localhost` and `127.0.0.1` on any port);
  another site is refused before anything runs. The list is replaced by `API_CORS_ORIGINS`;
- it checks the host name — protection against DNS rebinding. Your own names go into
  `API_ALLOWED_HOSTS`;
- with `API_TOKEN` it requires `Authorization: Bearer <token>`.

Opening the web from a phone or over a network — set `API_TOKEN`, `API_HOST`,
`API_CORS_ORIGINS` and build the web with the right `VITE_API_URL`.

## Docker

`docker-compose.yml` has three services:

| Service | What it runs | Address |
|---|---|---|
| `api` | the backend, `python -m app.main api` | http://127.0.0.1:8000 |
| `web` | the built web interface (nginx) | http://127.0.0.1:5173 |
| `bots` | Telegram bots of all personas with `<PERSONA>_BOT_TOKEN` in `.env` (profile `telegram`) | — |

```bash
cp .env.example .env                              # provider keys, tokens
docker compose up -d --build                      # API + web
docker compose --profile telegram up -d --build   # API + web + Telegram bots
docker compose logs -f api                        # backend log
docker compose down                               # stop (data stays)
```

Ports are open only on the host's `127.0.0.1`. Other ports are set in `.env`: `API_PORT` and
`WEB_PORT`. The API address is baked into the web at build time, so after changing
`API_PORT` you need a rebuild (`--build`).

Where the data is kept:

- **`.env`** — the host's file, mounted into the container. Keys and settings saved from the
  web are written to it as well. Without `.env` the container won't start.
- **Your personas and memory** — the `vpc_data` volume (`data/personas/` and memory), separate
  from the `data/` of a local run. Built-in personas are in the image. To work with your
  local personas and memory, replace `vpc_data:/app/data` with `./data:/app/data` in
  `docker-compose.yml` and don't run the bot locally and in Docker at the same time.
- **Hugging Face models** — the `hf_cache` volume. The memory model is already in the image;
  the models for Arrodes' book search are downloaded on first use.

Notes:

- **Ollama** is taken from the host: `http://host.docker.internal:11434`, another address —
  `DOCKER_OLLAMA_URL` in `.env`. `OLLAMA_URL` from `.env` is not used inside the container:
  `localhost` there is the container itself.
- **The time zone** in the container is UTC. Set `TIMEZONE` in `.env`, otherwise reminders
  and day boundaries will shift.
- **There is no browser in the container:** control mode, web chats and browser search work
  only in a local run.
- The API in the container listens on `0.0.0.0` — otherwise the port mapping can't reach it —
  and checks the host name (`API_ALLOWED_HOSTS`, `localhost` by default). The log warning
  about binding to a non-loopback address is expected: from outside, the API is reachable
  only from the host's `127.0.0.1`.

## Add-ons

An add-on is a Python package that plugs in through entry points:

- `virtual_persona.addons` — the add-on class;
- `virtual_persona.personas` — a folder with the YAML of its personas.

A persona enables an add-on with the `features.addons` list. An add-on adds a block to the
prompt (`build_context`) and fixes or cleans the reply (`repair`, `postprocess`). A broken
or missing add-on is skipped with a warning in the log.

**Arrodes** — a persona who knows "Lord of Mysteries", in a separate repository: search over
the book's text (vectors, BM25, reranking), a glossary in the prompt, references to
fragments `[ФN]`. Install it into the same Python as the core, after the core:

```bash
pip install -e .                          # the core — first
pip install -e <Arrodes folder>
```

The book database lives in the Arrodes folder, the persona's memory — in the core's data.
Building the database and checks — in the Arrodes README.

## Project layout

| Path | What's inside |
|---|---|
| `app/main.py` | entry point: menu, Telegram bots, API |
| `app/bot_instance.py` | `BotInstance` — one persona: memory, models, modules, message handling |
| `app/telegram_bot.py` | Telegram commands and messages |
| `app/core/` | persona, memory (ChromaDB), model router, persona life, intelligence tiers, add-ons |
| `app/features/` | modules: reminders, to-dos, learning, initiative, rhythm, search, control mode, browser, web chats |
| `app/api/` | FastAPI: server, settings, security, skins, room |
| `app/personas/` | the built-in test persona (Connor) |
| `data/personas/` | your personas (outside git) |
| `web/` | the web interface (React, Vite) |
| `desktop/` | the tray panel (Tauri) |
| `scripts/` | tests, benchmarks, utilities |
| `.env.example`, `.env.config` | template of personal settings; non-secret defaults |

The path of one message — filters, memory, search, model, markers, saving — can be read in
`BotInstance.process_message`.

## Tests

Tests are `scripts/test_*.py` scripts (92), without pytest; Arrodes tests are in its own
repository. Run them from the project root:

```bash
OLLAMA_URL=http://127.0.0.1:9 python -m scripts.test_computer_control
```

`OLLAMA_URL` points to a dead port so the test doesn't load a real model. Each check prints
`[OK]` or `[FAIL]`, and a summary at the end. Some scripts exit with code 0 even on failures,
so look for `[FAIL]` in the output, not at the exit code. Telegram tests need
`python-telegram-bot`, some browser tests need Node.js and Playwright with Chromium.

## Dependencies

**Python.** `pyproject.toml` holds version ranges: the lower bound is what the project is
tested on, the upper one is the next major version. `requirements.txt` is a lock file with
exact versions for all platforms, built by [uv](https://docs.astral.sh/uv/) from
`pyproject.toml`. To update packages, adjust the ranges, rebuild the lock with the command
from the header of `requirements.txt` (with `--upgrade` — to the newest versions within the
ranges) and run the tests.

**Web and panel.** Exact versions are in `web/package-lock.json` and
`desktop/package-lock.json`. `npm ci` installs exactly those; `npm install` may update the
lock.

## License

The project's code is distributed under the [Mozilla Public License 2.0](LICENSE) (MPL-2.0).

- Anyone can use it for anything, including commercial and closed-source products.
- Whoever distributes modified files of the project must open their source code under the
  same MPL-2.0. Your own code that only uses the project can stay closed.

The license does not cover third-party material in the repository: the "Lord of Mysteries"
characters in the web demo data (`web/src/mockData.ts`). The rights to them belong to their
owners.
