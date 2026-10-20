# Virtual Persona Core

**English** · [Русский](README.ru.md)

A platform for "living" characters: bots that remember the person they talk to, live their
own lives between messages and message you first. A persona is a YAML file with a
character, memory and a set of modules. The same persona works in Telegram, in the web
interface and in the phone app, can remind you of things, keep your to-do list and teach
you, and — at the owner's request — drive their browser.

Replies come from any OpenAI-compatible model with an API key, from an LLM web chat in the
bot's browser (DeepSeek, Qwen, Claude, ChatGPT and more) or from a local model in Ollama —
so the bot can run without any API keys at all.

Backend — Python (FastAPI, ChromaDB, python-telegram-bot), web — React (`web/`), tray
panel — Tauri (`desktop/`). The main platform is macOS; Windows is supported but less
tested.

https://github.com/user-attachments/assets/aa87b619-6564-4703-81d8-762ad478f123

## What it can do

- **Telegram, web and phone.** In Telegram — private chats and groups (by a trigger word or a
  reply to the bot's message), commands, long code is sent as files. On the web — chats with
  all personas, dossier, the persona's room, settings, a persona editor, voice mode. On
  Android — an app with the same chats, voice, camera and notifications while it is closed;
  the phone connects to the computer over an encrypted channel.
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
  notes ("add to my notes …", "what's in my notes?" — each persona keeps its own, shown on
  the home page), inventory, courses with quizzes, web search.
- **Control mode.** On the owner's command the bot opens sites and apps, searches on sites,
  clicks buttons, types text, reads pages, carries out multi-step tasks ("order a pizza")
  and replays recorded scenarios. Actions are confirmed. Task steps spare your web chat
  accounts: when a site fails, they go to sites without an account (duck.ai, Google AI Mode
  signed out) or to the one that has rested longest, not to the main one.
- **Screen and camera.** "Look at my screen" — the persona looks at the computer's screen;
  the camera button in the chat — at a photo from the phone or a webcam. Only the local
  model in Ollama sees the picture; the persona gets its description.
- **Files from the computer.** "Open the file report on physics", "send me the file about the
  tax deduction" — the bot finds the file on the computer the core runs on: the web there opens
  it, the phone app and Telegram get the file itself.
- **Voice activation.** Say a persona's name — "Connor, what's the weather?" — and it answers
  out loud: on the computer the core runs on and on the phone, even with the screen off. Phone
  commands work by voice too: "Connor, turn on the flashlight".
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
shows their status and a live log, and listens for persona names with its own microphone
([Voice activation](#voice-activation)). Build instructions — in
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
| `LOCAL_LLM_DEVICE` | where the Ollama work runs: `pc` (default) or `phone` — the phone app's Gemma takes the reply-path tasks and the main reply when it falls back to the local model; background tasks, images and private pages stay on the computer's Ollama |
| `LTM_MODEL_PROVIDER` | a separate provider for fact extraction; empty — the persona's chain |
| `API_HOST`, `API_PORT` | backend address: `127.0.0.1:8000` |
| `API_TOKEN` | API password; the web asks for it on sign-in. Empty — no password, but only for requests through `localhost` |
| `API_CORS_ORIGINS`, `API_ALLOWED_HOSTS` | whose pages and which host names may reach the API — see [API](#api) |
| `VPC_DATA_DIR` | the data folder — one for everything: memory, your personas, state; `data/` by default. `DATA_DIR` is the old name of the same setting |
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
| `/notes` | your notes (`notes`) |
| `/remind`, `/reminders`, `/cancel_reminder` | reminders (`reminder`) |
| `/inventory`, `/add_inventory` | inventory (`inventory`) |
| `/learn <topic>`, `/stop_learning` | a learning course (`learning`) |
| `/files`, `/reset_files` | uploaded files (`file_upload`) |
| `/erase N`, `/context`, `/resetall`, `/reset_diary` | owner only: erase the last N messages, the prompt context as a file, all memory, the diary |

In parentheses — the persona module the command needs. Commands go through the same filters
as messages: blocks, limits, moderation, muting.

### Web interface

- **Start** — how to use it: where to begin, special features, the phone, screen and camera,
  a control mode cheat sheet, Telegram commands.
- **Home** — a calendar and a "while you were away" feed: initiatives, diary, reminders.
- **Chat** — all chats as a list, then the chat with a persona, voice mode, camera, skins; in
  control mode — answer buttons, a task card and the agent's live browser window.
- **Room** — the persona's live scene: where it is, what it is doing, its things;
  a picture-in-picture window.
- **Personas** — create (form with the YAML next to it), edit YAML, rename, copy, color.
- **Skins** — looks for the chat, dossier and room: library, upload, generation.
- **Settings** — language (ru/en), notifications, developer mode, location, time zone,
  provider keys, web chats; "Phone" — connect a phone by QR code and the API token for
  connecting by address.
- **Persona dossier** — memory, reminders and to-dos, initiative, control mode, learning,
  files, persona settings (models, generation, modules).

The web finds the backend through `VITE_API_URL` (`http://127.0.0.1:8000` by default). If
`API_TOKEN` is set, the web asks for it on sign-in. Without a backend you can continue on
demo data after 3 seconds — the interface switches to live data by itself once the backend
is up.

### Phone app (Android)

The web interface is also built as an Android app. The backend keeps running on your
computer, the phone connects to it: the same chats and history, voice and camera, control
mode with the live browser window, persona dossiers and settings. Background messages (reminders, initiative)
reach every device — the computer and the phone each get their own copy.

**Phone commands.** In the app's chat (typed or spoken) and by the persona's name
([Voice activation](#voice-activation)): "set an alarm for 7 on weekdays",
"wake me up in 20 minutes", "set a timer for 10 minutes", "show my alarms", "open Telegram",
"turn on the flashlight" — in Russian too. The phone does it itself: alarms and timers go to
the system Clock app, apps are opened by name. The persona replies based on the result and
says honestly when something did not work (no such app, or the clock app cannot take a
date). The phone cannot delete or turn off alarms — it opens the alarm list instead. In
control mode, commands are still meant for the computer; the phone also leaves alone anything
said "on the computer". Works without a connection to the core too.

**Connecting — VPC Link.** The phone and the computer pair once by a QR code and from then
on recognize each other by keys: no VPN on the phone, no accounts, no passwords. Everything
is encrypted end to end ([Noise](https://noiseprotocol.org), the same protocol family as
WireGuard), and the API itself stays on `127.0.0.1` — the channel carries requests to it.

1. On the computer: Settings → Phone → **Connect a phone**. A one-time QR code appears
   (valid for 10 minutes).
2. In the app: **Scan QR code**. Done — the phone shows up in the list on the computer,
   where it can be unpaired at any time.

Paths, tried in this order:

- **the same Wi-Fi** — directly, on port `LINK_PORT` (8766). If the macOS firewall is on,
  allow incoming connections for Python;
- **away from home** — through a relay: a tiny server (`app/link/relay.py`, one file,
  needs `pip install websockets`) on any machine both devices can reach. It only forwards
  ciphertext: it cannot read or forge anything. On the server:
  `python3 relay.py --port 8443 --host-key <password>` (put it behind nginx/caddy with TLS,
  or pass `--cert`/`--key`); in the core `.env`: `LINK_RELAY_URL=wss://<server>` and
  `LINK_RELAY_KEY=<password>`. Without a relay the phone works only on the same Wi-Fi.

The computer must stay awake while you use the app.

**Connecting by address** (fallback): `API_HOST=0.0.0.0`, `API_ALLOWED_HOSTS=192.168.1.5`
(the computer's address), `API_TOKEN`; in the app — `192.168.1.5:8000` and the token. The
token is shown on the computer: Settings → Phone → "API token", with "Show" and "Copy"
buttons. The port in the address is the API port (`API_PORT`, 8000), not `LINK_PORT`: that
one is for VPC Link only. The traffic is not encrypted here, so VPC Link is the better choice. Without `API_TOKEN` the
backend refuses requests that don't come through `localhost` (403). If `API_CORS_ORIGINS`
is set, add `https://localhost` to it — that is the app's origin.

**Building the APK** needs Android Studio (it brings the Android SDK and Java):

```bash
scripts/fetch_wake_model.sh   # once: speech recogniser for voice activation (~80 MB, not in git)
cd web
npm ci
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"  # macOS
export ANDROID_HOME="$HOME/Library/Android/sdk"
npm run apk
```

The file is `web/android/app/build/outputs/apk/debug/app-debug.apk`: copy it to the phone
and install it (allow installing apps from this source), or `adb install` it. On Windows
the last step is `cd web/android && gradlew assembleDebug` after `npm run build`,
`npm run phone-voice` and `npx cap sync android`.

**Notifications while the app is closed** (connection by address only, for now): Settings →
Connection → **Notifications when the app is closed**. The app then keeps a small background
service (a silent "Core connection" notification in the shade). The service keeps a request
to the core open (`GET /api/inbox` with `wait`): the core answers as soon as a message
appears, or with an empty reply every 5 minutes if there is none, and the service opens a new
request right away. Android keeps the network on for a service with a notification even in
deep sleep, and the incoming reply wakes the phone, so reminders and persona messages arrive
within seconds; tapping one opens that persona's chat. A reply lost on the way is sent again.

The shade shows the time of the last reply: "Connected to … · core replied at 23:58". If it is
older than 6 minutes, the connection is stuck. The "Test" button in the app's Settings shows
a real Android notification. No Google/Firebase push is involved — it works while the phone
can reach the computer; without a connection the service retries up to once a minute, less
often while the phone sleeps. On Honor, Huawei and Xiaomi phones allow the app to run in the
background and set its battery usage to "Unrestricted", or the system will put it to sleep.
Over VPC Link the switch is hidden for now.

**Camera.** The camera button next to "+" in the chat opens the phone's camera (Android asks
for permission). The photo is scaled down and sent to the core, where only the local model
sees it — see [Screen and camera](#screen-and-camera).

**Voice mode** in the app uses Android's own speech recognition and text-to-speech (the
WebView has no Web Speech API): the phone needs a speech recognition service — usually the
Google app ("Speech Services by Google"), another engine works too — and a text-to-speech
engine (usually "Speech Services by Google" as well). The first tap on the microphone asks
for permission to record audio. Without a recognition service you type instead and the
persona still answers with voice; without a text-to-speech engine replies are text only. The
speech language is the interface language (Russian or English); for offline recognition,
download that language in the phone's voice input settings.

**Without the core.** When the phone can't reach the computer, the app answers by itself: with a
cloud model if the persona's API keys were copied to the phone, otherwise with Gemma downloaded
to the phone (Settings → "Without the core"). The persona's description, facts and recent
chat come with it from the core; the conversation is kept on the phone and goes into the core's
history once it is reachable again. New personas can be created on the phone too: the form is
the same, the persona is answered by Gemma from its full description and moves to the core as
soon as it is reachable — under a new id with a `_2` suffix if that id is taken there. After that
it is an ordinary persona: the core writes its short description for Gemma, gives it the API
keys and so on.

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
| `short_prompt` | a short card for the phone's Gemma; without it the core writes one itself |
| `intellect` | intelligence tier — see below |
| `conversation_style` | how often a reply ends with a question — see below |
| `llm` | the persona's models — see [Models](#models) |
| `world_binding` | `real_world` or `fictional_universe`; without the key the model decides from `system_prompt` |
| `room` | things, a pet and places in the persona's room |
| `start_greeting`, `pre_reply_text` | Telegram: the `/start` text (`{name}` — the person's name); a phrase before a long reply |
| `max_docs`, `max_file_size_mb` | uploaded file limits: 3 documents, 10 MB |

Modes by speaker — files next to the persona's YAML, in a `<persona>_modes/` folder:
`master.yaml` for a special person (`special_users`), `mortal.yaml` for everyone else. Each has
its own `system_prompt` (and optionally `short_prompt`) used instead of the common one — in
replies, reminders, proactive and daily-rhythm messages and in the phone's replies without the
core. No file
for a mode — the common `system_prompt`. On the web the default speaker (`web_user`, the
core's app) is the special person; an explicit other `user_id` is matched against
`special_users`.

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
| `notes` | the user's notes: on unless `notes: false` |
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

For the first 10–20 minutes after the core starts, personas don't write on their own:
initiative, daily rhythm, life and lessons stay quiet (`BOT_STARTUP_QUIET_MIN`, `0` — no
quiet). Replies to you are not delayed. Reminders due after the start arrive on time; those
missed while the bot was off come after the quiet period.

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

## Screen and camera

A persona can look at the computer's screen or at a camera photo. Only the local model in
Ollama sees the picture — it never leaves the computer — and the persona answers with its
usual model from the text description. You need a local model with vision: `gemma4:e2b`
works (`ollama show <model>` lists `vision` among its capabilities).

- **"Look at my screen"** — "what's on my screen?", "check the screen, what is this error?",
  "посмотри на экран". It captures the main screen of the computer the core runs on, even
  when you ask from the phone; the screenshot is scaled down to 1600 px on the long side.
  Works on the web, in the app and in Telegram — there for the owner only. It does not work
  in control mode (there "screen" means the agent's page) or from skins.
- **Camera** — the camera button next to "+" in the chat: the phone's camera in the app, a
  webcam with a preview in the browser. A picture attached with "+" is handled as before: by a
  cloud model with vision if one is set up, otherwise by the local one.

On macOS the screen can be captured only with permission. On the first request the system
asks it for the app the core was started from (Terminal, iTerm, VS Code or the tray panel).
Turn it on in System Settings → Privacy & Security → Screen & System Audio Recording and
restart that app. Until the permission is granted, or while Ollama is not running, the chat
shows what to do instead of a reply.

## Voice activation

Say a persona's name and it answers out loud — "Connor, what's the weather?" — with no
browser open. On the computer the tray panel listens: "Listen for name" in its menu or window
(details below). Speech is recognised offline
by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) with a small Russian streaming model
(~28 MB, downloaded from HuggingFace into `data/models/wake-ru` on first start). The names are
each persona's `features.trigger_words` — personas without them are not voice-addressable.
Names are matched by sound, so "коннер" still wakes Connor; the name has to come at the
start of the phrase ("hey Connor, …" works, "we talked to Connor yesterday" does not).

- **Name + request** — "Коннор, поставь будильник на семь" goes straight to the persona.
- **Name alone** — a short chime, then the next phrase is the request (8 s to start talking).
- **A reply that ends with a question** — answer within 6 s without the name.

The request goes into the same chat as the web interface, so the exchange shows up there.
While the persona is speaking the microphone is muted, so it doesn't wake itself up.

**On the computer** you need `pip install -e ".[voice]"` in the Python or venv the core runs in.
Then turn on "Listen for name" in the tray panel's menu or in its window. The panel runs the
listener as its own process (`python -m app.voice.wake_listener`), so the microphone belongs to
the panel. It listens whether or not the bot is running; the bot is only needed for the answer.
The panel shows the state: listening and to which names, waiting for the bot, no microphone
access, or which `pip` command to run. If the panel hasn't been switched yet, it follows
`WAKE_LISTEN=1` in `.env`.

Without the panel, the core can listen itself: `WAKE_LISTEN=1` in `.env` (other settings are in
`.env.example`), then restart the core. A core started by the panel doesn't listen — the panel
does. A core started from a terminal hands the microphone over to the panel's listener while that
one is running. `GET /api/voice/wake` shows who is listening (`where`: `panel` or `core`), the
names, what was heard last and any error. By default it takes the laptop's built-in microphone rather than the system
default: a Bluetooth headset mic would switch the headset into call mode. Replies are spoken
with `say` on macOS (PowerShell's System.Speech on Windows and espeak on Linux — untested).

**On the phone** — Settings → Connection → "Listen for name" (connection by address and
token, like background notifications). The app listens with the same model and rules, even
with the screen off, and answers through the phone's speech synthesis; it keeps an ongoing
notification with an "Off" button. It costs battery. On Honor and other phones with aggressive
battery saving, set the app's battery usage to manual management, otherwise the system may stop
it with the screen off. When the phone and the computer hear the same phrase, only one of them
answers (`voice` in `POST /api/chat`), and while the voice chat mode is open on either, the
computer's listener pauses.

Phone commands work by voice too — "Connor, turn on the flashlight", "…set a timer for five
minutes", "…open Telegram": the service runs the same parser as the chat
(`web/src/phone/actions.ts`, bundled by `npm run phone-voice` into `assets/phone-voice.js` and run
in a headless JS engine), the phone does the action and the persona answers from the result. The
flashlight works with the screen off. Alarms, timers and opening apps start other apps' screens,
which Android allows from the background only with the "Display over other apps" permission —
the button for it is under "Listen for name"; without it the persona says it couldn't.

On macOS the first start asks for microphone access for the app that listens: the tray panel
("Virtual Persona"), or the terminal the core was started from when the core listens itself.
If access was denied, the microphone returns pure zeros and the panel, the log and
`/api/voice/wake` say so: turn the app on in System Settings → Privacy & Security →
Microphone. The listener reopens the microphone every few seconds and picks the permission up
on its own.

## Files from the computer

The owner can ask for any file on the computer the core runs on — by name or by what is inside.
The search uses the system index (Spotlight on macOS, Windows Search on Windows, `locate` on
Linux); without an index it walks the home folder by file name.

- **How to ask:** "open the file lab report 1", "open the presentation on physics", "send me
  the pdf of the lease", "open the latest file from Downloads", "send the file from the folder
  smad", "open report.docx". "About …" / "on …" also searches inside files. Russian works too:
  «открой файл отчёт по лабе», «пришли файл про налоговый вычет».
- **Narrowing by folders:** "open the manual in db in LabS7" — the bot finds the folder LabS7 (the
  name may be inexact: LabsS7 matches), db inside it, and searches only there. The order of the
  folders doesn't matter. With folders the word "file" is optional: then the bot takes the request
  only if the folders and a matching file exist ("open search in youtube" goes on as usual). It
  also knows document words: «методичка» (a manual) matches «УП_…» and «Методические указания».
- **Work number:** "the report for lab 1", "for the first lab" — the number is matched as a whole
  number (in the name or a folder: `lab1`, not `lab12` or `lab2`); files with another number stay
  out of the list.
- **A full path:** "open ~/Documents/LabsS7/km" — on the core the folder opens, the phone and
  Telegram get a list of its files; "the report in ~/Documents/…" searches only there. A file
  name sent on its own ("km_lab1_report.pdf") is a request too, if such a file exists.
- **What happens:** in the web opened on the core's computer the file opens in its default app;
  in the phone app and in Telegram the file is sent to the chat (up to 50 MB). In the app the
  "Save" button puts it into Downloads/Virtual Persona and opens it.
- **Several matches** — a numbered list; reply with the number ("yes" means the first, "no"
  cancels). Only the list's stub goes into the dialog history, not the names of other files.
- **Not touched:** system and hidden folders (`~/Library` except iCloud Drive and cloud drives,
  `.ssh`, AppData…), the core's data folder, keys, certificates and password databases.
  Programs and scripts are never opened from a message — the bot shows them in their folder.
- **Who:** the owner only; in Telegram only in a private chat, plus the groups listed in
  `features.file_access.allowed_chats` (everyone in such a group sees the file); not from skins.
  Every action is logged to `data/<context>/file_access/audit.jsonl`. Turn it off for a persona
  with `features.file_access: false`.

## Control mode

The bot works with the owner's browser and apps. The mode is turned on per chat and turns
itself off after 30 minutes of inactivity; a bot restart does not reset it.

![Control mode: "order a pizza" — the agent builds the order and stops before payment](https://github.com/user-attachments/assets/386e6a3d-9ac5-4714-8799-2ed87aa278ef)

- **Enable for a persona:** dossier → "Control mode" or `features.computer_control` in YAML.
- **Enter:** "enter control mode".
- **Exit:** "exit control mode".

While the mode is on, reminders, to-dos, inventory and learning stay silent so they don't
compete with commands.

**Typos.** "enter contorl mode", "opne youtube", "scrol down" and a command typed in the
wrong keyboard layout are understood right away, without the model. Opening, scrolling and
hovering run as usual; a corrected click, typing and the rest wait for "yes". With the mode
off, "open youtube", "click …", "scroll …" get an immediate answer: "Control mode is off —
turn it on and run it?". "Yes" turns the mode on and runs the command.

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

**On the web chat** the mode is visible right away: the header shows a "Control" badge and
"Browser", "Stop" and "Exit" buttons. "Browser" opens a live window of the tab the agent works
in, next to the chat. The agent's questions come with buttons: "Yes" and "No", choices, "Drop
the task". A task ("order a pizza") is shown as a card: the plan with checkmarks, the step log
and the status update in place.

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
`deepseek`, `qwen`, `claude`, `zai`, `chatgpt`, `kimi`, `google` (AI Mode), `google_anon`
(AI Mode signed out, in a separate context of the bot's browser), `duckai` (no account).

```env
WEBCHAT_SITES=deepseek,qwen
```

or per persona:

```yaml
llm:
  primary: webchat:deepseek
  webchat_mode: {deepseek: headless}   # headless | hidden | headed
```

Every dialog has its own persistent chat on the site (each Telegram chat and web chat of a
persona), so the model never sees other conversations. A new chat is opened only when the old
one is unavailable, and no more than 3 times an hour and 10 a day per site
(`WEBCHAT_NEW_CHATS_PER_HOUR`, `WEBCHAT_NEW_CHATS_PER_DAY`): sites treat batches of fresh
chats as spam. `/clear` and clearing the history on the web start a new chat on the site. If
a site suspends the account, the bot recognizes it and stays away until the suspension ends.
An answer takes 10–60 seconds, without streaming. Sites don't welcome automation.

Automation — task agent steps and background work — writes to a site no more than once
every 20–30 seconds and no more than 60 messages an hour, and no more than 20 to a site with
your account (all except `duckai` and `google_anon`). The count is shared by all personas,
because they use the same account. Replies to your own messages don't wait. Configured in
`.env`: `WEBCHAT_AUTO_GAP_SEC`, `WEBCHAT_AUTO_JITTER_SEC`, `WEBCHAT_AUTO_PER_HOUR`,
`WEBCHAT_AUTO_PER_HOUR_ACCOUNT`. A chat that has already seen the task agent's rules and an
unchanged page gets a short reference to them instead of the full text.

If the task agent's site (`cc_provider`) or the main web chat doesn't answer, the step does
not fall through the usual chain to a site with your account. Sites without an account go
first, then the site with an account that has gone longest without a message. Sites in
quarantine, at the hourly limit, excluded by the persona, sites of the visible browser
(`chatgpt`, `claude`) and those the prompt doesn't fit are skipped. The chosen site stays with
the task to the end; after that come API providers with keys. If nobody answers, the task
stops and waits for "continue".

`google_anon` never clicks through a captcha or opens a window to solve it: the site just goes
quiet for 6 hours. Its cookies (without sign-in cookies) are kept in `data/webchat_contexts/`.

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
| GET | `/api/personas/{p}/inbox` | background messages: reminders, lessons, initiative; "typing" and messages from other devices the persona is answering now (`pending`) |
| GET | `/api/changes` | which personas changed (generation, history, background message); with `since`, `epoch` and `wait` it waits for a change — the web then polls their inbox at once |
| GET | `/api/phone-llm/next` | the phone waits for a Gemma job (`LOCAL_LLM_DEVICE=phone`); the answer goes back via `POST /api/phone-llm/result` |
| GET | `/api/inbox` | new background messages of all personas — for the app's notification service; with `wait` it waits up to `wait` seconds for a message |
| GET | `/api/personas/{p}/state` | the persona's state and its life feed |
| GET | `/api/providers` | providers, keys, the active one |
| GET, POST, DELETE | `/api/link`, `/api/link/pair`, `/api/link/devices/{id}` | phones over VPC Link: list, pairing QR code, unpairing |
| GET | `/api/token` | `API_TOKEN` for connecting a phone by address (403 over VPC Link) |
| GET, POST | `/api/voice/wake`, `/api/voice/wake/hold`, `/api/voice/wake/report` | voice activation on the core's computer: who is listening (the panel or the core), names, last heard, error; pause while a voice chat is open; the panel's listener reports itself once a second |
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
- with `API_TOKEN` it requires `Authorization: Bearer <token>`; without it, it refuses
  requests that come by another host name or through a proxy (403).

Using it from a phone — see [Phone app](#phone-app-android). Opening the web over a
network — set `API_TOKEN`, `API_HOST`, `API_CORS_ORIGINS` and build the web with the right
`VITE_API_URL`.

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
| `app/features/` | modules: reminders, to-dos, learning, initiative, rhythm, search, control mode, browser, web chats, screenshots |
| `app/api/` | FastAPI: server, settings, security, skins, room |
| `app/link/` | VPC Link: the encrypted channel to the phone (Noise), the relay |
| `app/voice/` | voice activation: listening for a persona's name (sherpa-onnx), matching names by sound |
| `app/personas/` | the built-in test persona (Connor) |
| `data/personas/` | your personas (outside git) |
| `web/` | the web interface (React, Vite); `web/android/` — the Android app (Capacitor) |
| `desktop/` | the tray panel (Tauri) |
| `scripts/` | tests, benchmarks, utilities |
| `.env.example`, `.env.config` | template of personal settings; non-secret defaults |

The path of one message — filters, memory, search, model, markers, saving — can be read in
`BotInstance.process_message`.

## Tests

Tests are `scripts/test_*.py` scripts (111), without pytest; Arrodes tests are in its own
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
ranges) and run the tests. Voice activation on the computer is an optional extra:
`pip install -e ".[voice]"` (sherpa-onnx, sounddevice) — into the same Python or venv the core
runs in.

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
