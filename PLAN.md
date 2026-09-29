# MaiBot Telegram Full — Development Plan

Revised, phased version of `plan.txt`. `plan.txt` stays as the original requirements;
every item below is tagged with its original number (`[R7]` = requirement 7) so nothing gets lost.

Reference versions checked on 2026-09-29:

| Component | Version | Notes |
|---|---|---|
| MaiBot (test container `maim-bot-core` on 10.3.3.5) | 1.2.5 | Python 3.13, Debian 13, no ffmpeg |
| maibot-plugin-sdk | 2.8.1 in container, 2.8.2 upstream | manifest v2 |
| Telethon v1 | 1.45.0 (PyPI) | current layer has everything below |
| Telethon v2 | unreleased | only a `v2` git branch; needs Rust + maturin to build, Python ≥ 3.12 |

---

## 1. Findings that change the original plan

These come from reading the SDK, MaiBot Host source and Telethon's TL schema. Each one confirms,
narrows or corrects a requirement.

**Confirmed as written**

- **[R1] Adapter type:** supported. Set `"plugin_type": "adapter"` in `_manifest.json`. Adapters
  load in the trusted (builtin) supervisor, appear in WebUI adapter management, and can be taken
  offline and brought back as a group.
- **[R3] Dependencies:** manifest `dependencies: [{"type": "python_package", "name": "telethon",
  "version_spec": ">=1.40,<2"}]`. The Host installs missing packages with `uv pip`, so users don't
  run pip themselves.
- **[R3] Telethon's optional packages:**
  - `cryptg` is declared as a **hard dependency**. It is about 60× faster than Telethon's libssl
    fallback (4 ms vs 262 ms per MiB of AES-IGE, measured), and every MTProto byte is encrypted.
    Wheels exist for CPython 3.8–3.14 on glibc Linux (x86_64/aarch64), Windows and macOS arm64,
    which covers MaiBot's Docker, Windows and Apple-silicon setups. Elsewhere (Intel macOS,
    musl/Alpine, 32-bit ARM) pip would need Rust to build it.
  - `Pillow` is also declared (sticker conversion); the Host already ships it.
  - `aiohttp` is already in the Host.
  - `hachoir` (media metadata such as voice/video duration for uploads) gets added in Phase 3 if needed.
- **[R24] Async:** the plugin runs in its own Runner subprocess with its own event loop. Telethon runs
  on that loop. The rule is no blocking calls: SQLite and file I/O go through `asyncio.to_thread`.
- **Chat allow/block lists:** the Host applies a single adapter policy (`config/adapter_policy.toml`,
  editable in WebUI) to every inbound `route_message`. We drop the original adapter's own
  whitelist/blacklist and keep only adapter-specific filters (bots, commands, …).

**Narrowed or corrected**

- **[R2] Telethon v2 cannot be a hard requirement yet.** It's unpublished, and building it needs a
  Rust toolchain that the MaiBot Docker image doesn't have. Decision: **v1 only**. Telethon calls
  stay isolated in `backend/` so a later migration is cheap (Phase 8).
- **[R6] Bot accounts also need `api_id` / `api_hash`** (from my.telegram.org). MTProto requires them
  for every login. This is a new config field compared with the original adapter.
- **One bot token, one consumer.** The original adapter (Bot API long polling) is enabled in the test
  container for bot `8960184148`. When a bot is logged in over MTProto, updates stop reaching Bot API
  `getUpdates`. So we can't test with the same bot while the original adapter is running (see §6).
- **[R15] MTProto has no Bot-API `file_id`.** Media is referenced by
  `InputPhoto/InputDocument(id, access_hash, file_reference)`, and `file_reference` expires. We
  store `(id, access_hash, file_reference, origin chat, origin msg_id)`, and when we get
  `FILE_REFERENCE_EXPIRED` we re-fetch the origin message to refresh it. Telethon v1's
  `utils.pack_bot_file_id` can still produce Bot-API-style strings to show to the core or LLM.
- **[R15.1] Recognition caching is possible.** The Host skips image and emoji recognition when a
  segment arrives with non-empty `data`. The Host's `Images` table stores `image_hash (sha256) →
  description`. The flow:
  1. The first time a media item arrives, download it, hash it, record
     `file_unique_id → sha256` locally, and send the bytes.
  2. On later arrivals, look up the sha256, read the description from `Images` through `ctx.db`, and
     if there is one, send `data="[图片：…]"` + `hash` without downloading anything.
- **[R16] Stickers:** the Host has no `sticker` segment type (the original adapter's `sticker` segment
  degrades to a `[sticker]` DictComponent). Stickers go in as `emoji` segments, with the sticker's emoji
  passed as a hint in text or `additional_config`. On the way out, if the core sends an emoji whose
  sha256 matches a sticker we've already seen, we resend it natively by document reference instead of
  uploading an image.
- **[R17] GIF/animation:** Telegram provides a static JPEG thumbnail for animations, video stickers and
  animated stickers. The default is to use that thumbnail, so no ffmpeg is needed. Optional opt-in:
  real GIF conversion through PyAV (`av` wheel, which bundles ffmpeg, about 30–60 MB). The adapter
  installs it itself only when the user turns on a config switch, which serves as the approval. The
  other option is to discard animations.
- **[R9] Typing detection is userbot-only.** Bots never receive `UpdateUserTyping` or
  `UpdateChannelUserTyping`.
- **[R11] Bots can't call `messages.getHistory`**, but they can call `messages.getMessages` or
  `channels.getMessages` **by ID**. Polling therefore re-fetches the IDs of the last *n* known messages.
  From the results we can detect:
  - edits, from a changed `edit_date`
  - reactions
  - deletions, when a `MessageEmpty` comes back
- **[R20] Reactions:**
  - Userbots get `UpdateMessageReactions` including `recent_reactions[].big`, so long-press ("big")
    reactions can be detected.
  - Bots get `UpdateBotMessageReaction` only when they are admin, and it has no `big` flag.
  - Both account types can send reactions with `big=True`.
- **[R26] Length limits use two different metrics:**
  - **Hard limit (Telegram's rule):** 4096 UTF-16 code units of the *parsed* text, meaning after
    markdown has been turned into entities. Captions are limited to 1024 (2048 for Premium). Longer
    text is rejected with an error.
  - **Soft limit (our "greedy" width):** each character is weighted, CJK = 2 and emoji = 3. Above 1000
    we return a warning suggesting a split or Telegraph.
- **[R26.3] `messages.summarizeText(peer, id)` exists** in Telethon's current layer, but it
  summarizes an *existing message*, not arbitrary text. It's probably user-only and possibly Premium.
  It stays experimental and optional.
- **[R27] `ctx.db` only works on the Host's fixed models** (`Messages`, `Images`, …); there's no custom
  table or key-value store. Adapter state (media refs, sticker index, Telegraph account, callback
  patterns, poll cursors) lives in our own SQLite file under `ctx.paths.data_dir`. We only read the
  Host `Images` table (for R15.1).
- **[R29] Callback queries exist only for bot accounts.**
- **[R19] "Core is thinking" signal:** the `maisaka.planner.before_request` hook (OBSERVE mode) carries
  `session_id`. We map `session_id` to a chat and send `typing` once.
- **Message IDs:** the Host stores whatever `external_message_id` the gateway returns as the message's
  ID, and it resolves replies by `message_id` **globally** (no session filter). Plain Telegram IDs
  collide across chats, so every ID we give the Host is `"{chat_id}:{msg_id}"`, both inbound and
  outbound.
- **Notices:** the Host has an `is_notify` channel ("[事件-xxx]" style notices, which NapCat already
  uses). Edits, deletions and reactions can travel as notices. Phase 4 starts with a spike to confirm
  exactly how the Host treats notices, meaning whether they trigger the planner.

---

## 2. Architecture

```
MaiBot Host
   ▲  route_message / update_state          │ @MessageGateway(duplex) send
   │  hooks (planner.before_request)        │ @Tool (LLM)  @API (plugins)
┌──┴────────────────────────────────────────▼───────────────────────────┐
│ plugin.py            lifecycle, component declarations only           │
│ ├── config/          pydantic config model (WebUI schema, i18n)       │
│ ├── backend/         all Telethon calls isolated here (v1)            │
│ ├── inbound/         normalize → filters → dispatcher → codec → Host  │
│ ├── outbound/        Host msg → markdown/latex/length → send plan     │
│ ├── media/           refs, recognition cache, stickers, gif, previews │
│ ├── features/        typing, reactions, callbacks, telegraph, raw API │
│ ├── store/           async SQLite (data_dir)                          │
│ └── login/           bot token / user phone+code+2FA flows            │
└───────────────────────────────────────────────────────────────────────┘
```

Proposed layout ([R28]):

```
plugin.py                    # MaiBotPlugin subclass + create_plugin(); declarations and wiring only
_manifest.json  config.toml  README.md  requirements.txt (for manual installs)
tg_full/                     # package; relative imports only
  constants.py
  config.py                  # TelegramFullConfig (sections below)
  ids.py                     # message/group id encoding (topics, "-100" aliases, chat:msg)
  store.py                   # aiosqlite-free: sqlite3 + asyncio.to_thread, schema migrations
  backend/
    models.py                # plain dataclasses: TgMessage, TgPeer, TgMedia, … (no telethon types leak out)
    client.py                # connect/login/reconnect, send_*, get_messages, invoke_raw
    convert.py               # telethon Message/Update → models
  login.py
  inbound/
    normalize.py             # backend event → TgMessage (incl. forward/linked-channel identity)
    filters.py               # commands [R12], bots [R13]
    dispatcher.py            # per-chat scheduler [R7–R11]
    codec.py                 # TgMessage → Host MessageDict
    markdown.py              # entities → markdown [R22]
  outbound/
    codec.py                 # Host MessageDict → SendPlan
    markdown.py              # markdown → entities; latex → plain [R22.1]
    length.py                # [R26]
    reply_policy.py          # [R14, R21]
  media/
    refs.py  recognition_cache.py  stickers.py  animation.py  link_preview.py
  features/
    typing_indicator.py  reactions.py  callbacks.py  telegraph.py  raw_api.py
tests/                       # pytest, fake backend, fake ctx
scripts/deploy.sh            # rsync to LXC plugins dir + restart/reload
```

**Main config sections** (all hot-reloadable where it's safe):

- `plugin`: enabled, …
- `account`:
  - `type` = `bot` | `user`
  - `api_id`, `api_hash`
  - `bot_token`
  - `phone`, `password`
  - `login_code` (input field, see Phase 2)
- `connection`: proxy, reconnect settings, flood-wait ceiling
- `inbound`:
  - command filter level and mode [R12]
  - drop bot messages [R13]
  - edit debounce [R7], silence window [R8], typing wait [R9]
  - lazy push [R10], bot polling [R11]
- `outbound`:
  - markdown or plain [R22]
  - quote policy [R21], no-reply-to-bots [R14]
  - length limits [R26]
- `media`: recognition cache [R15.1], sticker handling [R16], GIF strategy [R17], link preview + UA chain [R18]
- `features`:
  - typing indicator [R19], reactions [R20]
  - callbacks [R29], Telegraph [R26.1]
  - raw API switch [R4.1], off by default

---

## 3. Phases

Each phase ends with something testable on the LXC container. Unit tests run against a fake backend,
so most logic is tested without Telegram.

### Phase 0: Scaffolding (no Telegram yet) — ✅ done 2026-09-29
- Repo layout above, `_manifest.json` (`plugin_type: adapter`, `python_package` deps, capabilities),
  `create_plugin()`, empty lifecycle.
- Dev env: `uv` project with `maibot-plugin-sdk==2.8.*`, `telethon>=1.40,<2`, pytest, ruff, pyright.
- `store.py` with a versioned schema.
- `scripts/deploy.sh`: rsync the plugin into `/home/kuma/MaiBot/data/MaiMBot/plugins/<dir>` on
  10.3.3.5, trigger a reload, tail the logs.
- **Done when:** the plugin loads in the container as an *adapter*, shows up in WebUI, and the config
  schema renders.

### Phase 1: Bot-account gateway MVP ([R5], [R6-bot], [R22], [R25], [R26 basic]) — ✅ live-tested 2026-09-29 (outbound formatting still to verify live)
- v1 backend: bot login, session file in `ctx.paths.data_dir` [R6.3], Telethon auto-reconnect [R5].
  Connection-state callbacks drive `gateway.update_state(ready=…)`, backed by a watchdog task.
- Inbound text messages:
  - entities → markdown [R22]
  - reply component
  - mention detection
  - group and topic IDs (compatible with the original adapter's `::tg-topic::` scheme so existing
    MaiBot sessions carry over)
  - `chat:msg` message IDs
- Identity handling:
  - forwarded messages: "B (forwarded from A)" [R25]
  - posts from a linked channel or anonymous admin are attributed to the channel or chat (`sender_chat`),
    not "Telegram" [R25.1]
- Outbound text:
  - markdown → entities [R22]
  - LaTeX → plain text or Unicode [R22.1]
  - hard and soft length limits [R26]
  - core-controlled reply
  - images, emoji and voice by bytes
- `platform = "telegram"`, same as the original adapter, so the existing `[bot] platforms =
  ["telegram:<id>"]` still works.
- **Done when:** the bot chats in a test group and in DMs through MaiBot (text, replies, images).

### Phase 2: User-account login ([R6], [R6.1], [R6.2])
- Phone login. Pre-configured 2FA password; if 2FA is required and no password is set, fail with a
  clear error [R6.1].
- **Login code input [R6.2]**, recommended approach:
  1. On first start the adapter requests a code and reports "waiting for login code" (log + gateway
     state metadata).
  2. The user types the code into the `account.login_code` field in the **MaiBot WebUI plugin
     config**.
  3. `on_config_update` picks it up and finishes `sign_in`, then clears the field.

  This needs no extra ports or container exposure. Two alternatives:
  - `python -m tg_full.login` CLI (via `docker exec`) as a fallback
  - QR-code login (`client.qr_login()`) with the `tg://login?token=` URL printed as a terminal QR

  A temporary web page is possible but would need a port mapped in Docker, so it isn't the default.
- **Done when:** a user account logs in once, restarts without asking again, and passes all Phase 1
  checks.

### Phase 3: Media pipeline ([R15], [R15.1], [R16], [R17], [R18])
- `media/refs.py`: persist file refs + origin, refresh on `FILE_REFERENCE_EXPIRED`, resend by reference.
- `recognition_cache.py`: `file_unique_id` → sha256 → Host `Images.description` → send without
  downloading.
- Stickers:
  - index by document id, with emoji and set name
  - send inbound stickers as emoji with an emoji hint
  - native resend when a core emoji hash matches a known sticker
  - LLM tools: "list stickers for 😂", "send sticker"
- Animations: thumbnail by default; optional PyAV (installed by the adapter on opt-in); or discard.
- Voice: bytes for ASR (the Host does transcription). Documents: `file` segment carrying name, size
  and mime.
- Link previews: first `MessageMediaWebPage` (title, description, site), otherwise a light HTTP fetch
  of `<title>` and OpenGraph tags. UA chain is configurable, default GoogleBot → desktop browser →
  curl → none.
- **Done when:** a repeated image or sticker isn't downloaded again (checked in logs), the bot sends a
  known sticker natively, and a GIF arrives as a static image.

### Phase 4: Inbound dispatcher ([R7], [R8], [R9], [R10], [R11], [R12], [R13])
- **Spike first:**
  - confirm how the Host treats `is_notify` messages and `maisaka.context.append`, i.e. whether they
    trigger the planner
  - confirm what happens when a batch of `route_message` calls lands at once
  - use the results to decide how edits, deletions and "stacked" commands are represented
- Per-chat scheduler, testable with a fake clock:
  - edit debounce, `n` seconds [R7]
  - silence window: no new, edited or deleted messages for `n` seconds before dispatch [R8]
  - typing hold, userbot only, with a maximum hold time so nobody can block us forever [R9]
  - lazy push: by count, by interval, or adaptive (starts from a baseline, follows an EMA of the
    chat's message rate, clamped to min/max) [R10]
- Bot polling [R11]: re-fetch the IDs of the last *n* messages at an interval. It only updates state
  (edits, reactions, deletions as notices or context) and never triggers a reply on its own [R11.2].
- Filters:
  - commands, level 1 (`/cmd@otherbot`) or level 2 (all), in mode `drop` or `stack` [R12]
  - bot messages dropped regardless of Telegram's bot-to-bot setting [R13]
- **Done when:** a burst of messages or edits produces one dispatch, and polling surfaces an edit made
  on another client without producing a reply.

### Phase 5: Interaction ([R14], [R19], [R20], [R21], [R29])
- Typing indicator: planner hook → `SetTyping` once per planning round, no refresh loop [R19].
- Reactions:
  - inbound, including big-reaction detection for userbots, as notices or context
  - outbound through a Tool (`telegram_react`, with a `big` option) [R20]
- Quote policy [R21]:
  - the core decides whether to quote (reply component / `set_reply`), matching the original adapter
  - adapter overrides: `always` / `core` / `never`, plus a toggle to never quote bot messages
  - also exposed as a Tool argument
- Bots [R14]:
  - method 2, the default, strips the quote when the target is a bot
  - method 1 drops the reply altogether, not recommended
- Callbacks [R29]: dropped by default. An API and Tool (`register_callback_pattern(regex)`) turn
  matching callback data into inbound messages. Includes a Tool to send inline keyboards, since the
  core needs a way to create buttons in the first place.
- **Done when:** "typing…" appears once while MaiBot plans, reactions work both ways, a registered
  button press reaches the core.

### Phase 6: Capabilities exposed to the core ([R4], [R4.1], [R4.2])
- **Tools (for the LLM):** a small, safe set, mostly from phases 3–5.
  - `send_sticker`, `react`, `send_with_buttons`, `post_telegraph`, `get_message`
  - each description is short and states risks where there are any
- **APIs (for other plugins):** the same operations, plus `get_chat_info` and `resolve_stream`.
- **Raw invoke (`telegram_raw_invoke`):**
  - **disabled by default**; enabled with `features.raw_api.enabled` plus an allow-list or deny-list of
    TL method names (defaults deny `auth.*`, `account.*`, `payments.*` and deletions)
  - input is a TL method name such as `messages.GetHistoryRequest` or `messages.getHistory`, plus JSON
    params
  - the adapter resolves the class in `telethon.tl.functions`, converts peer-like params with
    `get_input_entity`, validates the arguments against the constructor, runs it, and returns
    `to_dict()` (made JSON-safe)
  - the Tool description tells the agent this is risky and to check the Telethon/TL docs
    (tl.telethon.dev) first
- The manifest `capabilities` list declares every Host capability the plugin uses.
- **Done when:** the tools show up in MaiBot's tool list; raw invoke is refused when off and works for
  a harmless read call when on.

### Phase 7: Long text ([R26.1], [R26.2], [R26.3])
- Telegraph client:
  - create the account once and store its token in the store
  - markdown → Telegraph node JSON, limited to the allowed tags
  - `post_telegraph` Tool/API; its description says posts are **public, permanent and can't be
    edited**
- Link + short summary:
  - default is an extractive summary (first paragraph, truncated); no LLM keys
  - optional experimental `messages.summarizeText` for user accounts
- **Done when:** a 5000-character reply is refused with guidance, and posting it to Telegraph returns
  a link plus a summary.

### Phase 8: Telethon v2 migration ([R2]), deferred
- Starts once v2 is published on PyPI.
- Port `backend/` to v2 (a migration, not a second backend), or add thin compatibility shims if
  they turn out cheap.
- Rerun the whole test suite.

### Phase 9: Polish
- README (zh-CN + en), config i18n, migration notes from the original adapter, packaging for
  Mai-with-u/plugin-repo.

---

## 4. Testing

- **Unit tests (pytest, run locally with uv):**
  - markdown ⇄ entities, LaTeX sanitizing, length metrics
  - ID encoding
  - dispatcher timing with a fake clock
  - codecs, using a fake backend and a fake `ctx`
- **Integration:** run `scripts/deploy.sh` to the `maim-bot-core` container on 10.3.3.5 and chat with
  a test account in a test group. Logs are at `/home/kuma/MaiBot/data/MaiMBot/logs`.
- No Node.js needed.

## 5. Risks

- Unofficial MTProto userbots can get accounts limited or banned. The README should say so.
- Flood waits: the global send path respects `FloodWaitError` with a ceiling.
- Host APIs change fast (SDK 2.8.x); pin `sdk.min_version` and the host range in the manifest.

## 6. Decisions (2026-09-29)

- **Telethon:** v1 only for now. Telethon calls stay isolated in `backend/`, mainly so a later
  migration is cheap. No dual-backend abstraction, because that would double the code. When v2
  ships, Phase 8 becomes a migration or compatibility fix.
- **Test account:** a separate test bot (@rbevbot) in test group `-1001214803045`. The original
  adapter keeps running on its own bot. Secrets live only in the plugin's `config.toml` on the
  server (gitignored), never in the repo.
- **api_id/api_hash:** for testing, reuse the ones the `tgbot` container on the LXC already uses.
  The adapter config asks users for their own, and the field is required for both account types.
- **Login code:** entered through the WebUI config field `account.login_code`. A CLI helper is the
  fallback.
- **cryptg:** hard dependency in the manifest (see §1, [R3]). The adapter logs a warning at startup
  if it is missing anyway.
