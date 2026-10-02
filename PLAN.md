# MaiBot Telegram Full — Development Plan

Revised, phased version of `plan.txt`. `plan.txt` stays as the original requirements;
every item below is tagged with its original number (`[R7]` = requirement 7) so nothing gets lost.

Reference versions checked on 2026-09-29:

| Component | Version | Notes |
|---|---|---|
| MaiBot (test container `maim-bot-core` on 10.3.3.5) | 1.2.5 | Python 3.13, Debian 13, no ffmpeg |
| maibot-plugin-sdk | 2.8.1 in container, 2.8.2 upstream | manifest v2 |
| Telethon v1 | 1.45.0 (PyPI), **required ≥ 1.45** | layer 229 restructured inline keyboards (`KeyboardInlineButton`); everything else the backend uses exists since 1.40 |
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
- **[R3] Telethon version:** `>=1.45,<2`. Layer 229 (Telethon 1.45.0) replaced
  `KeyboardButtonCallback` / `KeyboardButtonRow` with `KeyboardInlineButton` +
  `InlineButtonType*`. Checked against the 1.40–1.45 wheels. A test asserts that every
  `types.*` / `functions.*` / `errors.*` name the backend uses exists in the installed Telethon.
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
  animated stickers. GIFs are common on Telegram, so the default is real GIF conversion through PyAV
  (`av` wheel, which bundles ffmpeg, about 30–60 MB), declared as a regular dependency. (Earlier the
  adapter installed it at runtime behind a switch; the plugin-repo review asked for declared
  dependencies instead.) The thumbnail and discarding animations remain as options.
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
- Catch-up (added 2026-10-02, live-tested with @rbevbot): `catch_up=True` resumes from the update
  state Telethon keeps in the session file (saved about once a minute), like the Bot API offset.
  A `TelegramClient` subclass also calls `catch_up()` after Telethon's in-sender reconnects, which
  otherwise only ping. Repeats are skipped via `msg_meta`; messages older than
  `connection.catch_up_max_age` minutes do not trigger a reply on their own (stacked like R12).
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

### Phase 2: User-account login ([R6], [R6.1], [R6.2]) — ✅ done 2026-09-29
- Phone login. Pre-configured 2FA password; if 2FA is required and no password is set, fail with a
  clear error [R6.1].
- **Login code input [R6.2]**, recommended approach:
  1. On first start the adapter requests a code and reports "waiting for login code" (log + gateway
     state metadata).
  2. The user types the code into the `account.login_code` field in the **MaiBot WebUI plugin
     config**.
  3. `on_config_update` picks it up and finishes `sign_in`. Plugins cannot write their own config
     (`component.update_plugin_config` is reserved for the built-in plugin manager), so the field is
     not cleared. Instead the adapter remembers used codes and ignores the value present at startup.

  This needs no extra ports or container exposure. Two alternatives:
  - `python -m tg_full.login` CLI (via `docker exec`) as a fallback
  - QR-code login (`client.qr_login()`) with the `tg://login?token=` URL printed as a terminal QR

  A temporary web page is possible but would need a port mapped in Docker, so it isn't the default.
- An existing Telethon session file can be dropped into the plugin data dir and selected with
  `account.session_name`; `account.phone` is then optional.
- `inbound.own_messages` (default `context`): messages typed by a human on another device logged
  into the same account reach MaiBot as the bot's own lines. The Host supports this ("guided_reply").
  Telethon never dispatches updates caused by the adapter's own requests, so replies aren't echoed.
- **User accounts are in many chats.** MaiBot's adapter policy (`config/adapter_policy.toml`, scoped
  by `plugin_id` + `account_id`) must whitelist the chats the userbot should talk in. The adapter
  caches "blocked" verdicts for 5 minutes, so it doesn't download media for chats MaiBot would
  reject anyway. Caveat: the policy matches the MaiBot group id, so a forum topic
  (`<chat>::tg-topic::mt=<n>`) is *not* covered by whitelisting `<chat>`. This needs handling (e.g.
  a Host change, or an adapter option) before forum groups are supported properly.
- **Done when:** a user account logs in once, restarts without asking again, and passes all Phase 1
  checks.
- **Status (2026-09-29): ✅ done.** Verified on the test container with @Kuma_AI:
  - log in from an existing session file (`kuma_ai`)
  - mention, and reply to the bot's answer
  - messages typed manually on the phone reach MaiBot as its own lines without triggering a reply
  - the login-code flow with a fresh session (`kuma_ai_2`): code requested, entered in the WebUI,
    logged in about 10 s later
  - the blocked-chat cache (logs "Chat … is blocked by MaiBot's adapter policy")

### Phase 3: Media pipeline ([R15], [R15.1], [R16], [R17], [R18]) — ✅ done 2026-09-29 (native resend verified by tests only)
As built:
- `media/cache.py` + store table `media`: every file given to MaiBot is recorded as
  `(Telegram file, variant full/thumb/gif) → sha256 of the bytes MaiBot got → file reference
  (id, access_hash, file_reference) + origin message + sticker emoji/set`.
  - **Inbound [R15.1]:** when the same Telegram file appears again and MaiBot's `Images` table
    already has a description for that sha256, the segment carries `[图片：…]` / `[表情包: …]` and
    no bytes. MaiBot skips recognition, and the adapter skips the download.
    Needs the `database.get` capability.
  - **Outbound [R15, R16]:** an image or emoji from MaiBot whose hash is a known Telegram file is
    resent by reference, so stickers and GIFs stay native and nothing is uploaded. An expired
    `file_reference` is refreshed by re-reading the origin message once; if that fails, the adapter
    uploads the bytes instead.
- **Stickers [R16]:** static ones are sent as the webp itself; Lottie and video stickers use a real
  thumbnail (≥100 px, never the tiny inline preview). Prefixed with `[贴纸 😂]` (configurable).
- **Animations [R17]:** `media.animation`
  - `gif` (default): a real GIF, 12 frames from the first 6 s, ≤320 px, which MaiBot's emoji system
    stitches into frames for its vision model. Uses PyAV; falls back to the thumbnail if conversion
    fails or PyAV cannot be imported.
  - `thumbnail`
  - `drop`: marker only.
- `media.video_thumbnail` (off by default) attaches a video's thumbnail for recognition.
- **Link previews [R18]:** Telegram's web page preview first. Otherwise (`media.link_preview =
  fetch`) the adapter fetches the page:
  - reads OpenGraph / `<title>` / meta description
  - User-Agent chain, default googlebot → browser → curl → default; `none` sends no UA, and raw UA
    strings are accepted
  - charset from the header or `<meta charset>`
  - results are cached for 24 h (negative results too)
  - http(s) only. Hosts resolving to loopback / private / link-local / other non-global addresses
    are refused (SSRF, from the plugin-repo review), checked in the connector's resolver and on every
    redirect hop, which the adapter follows itself. Fake-ip placeholders (198.18.0.0/15, common
    among Telegram users) are allowed, although Python does not count them as global; behind such a
    proxy the real address is resolved by the proxy and cannot be checked. `media.link_allow_private`
    (off by default) turns the check off. Refusals are not cached.
- **Moved to Phase 6:** the sticker *tools* for the LLM ("find stickers for 😂", "send sticker").
  They need the same stream → chat resolution as the other tools. The index they need is already
  stored.
- **Done when:** a repeated image or sticker isn't downloaded again, a known sticker is sent
  natively, a GIF arrives as a static image (or as a GIF in `gif` mode), and a link without a
  Telegram preview gets a fetched summary.

### Phase 4: Inbound dispatcher ([R7], [R8], [R9], [R10], [R11], [R12], [R13]) — ✅ done 2026-09-30 (bot polling verified by tests only)
**Spike results** (MaiBot 1.2.5):
- `is_notify` messages run the full receive chain and enter Maisaka's message cache. They count as
  pending external messages in its 读空气 score, and a new message can interrupt a running planner.
  So **notices can trigger a reply**. NapCat uses them for recalls, pokes and the like, as plain
  text like "X 撤回了一条消息".
- `maisaka.context.append` appends straight to Maisaka's in-memory chat history, with no cache and
  no scheduling, so it **never triggers**. It is not persisted, and it needs the stream id
  (resolved via `chat.get_all_streams`, filtered by account).
- MaiBot batches on its own: after an interrupting message it waits about 1 s, then scores the
  backlog (threshold 80, "等待更多消息"). The adapter's silence window and lazy push add to that wait.

**As built** (`inbound/dispatcher.py`, `inbound/pipeline.py`, config section `dispatch` / 投递节奏):
- Per-chat queue plus task. The decision `next_action(state, now, settings)` is a pure function,
  tested with a fake clock. Nothing waits longer than `max_wait` (60 s).
- **[R7] Edits:**
  - an edit to a still-queued message replaces it
  - an edit to a message MaiBot has seen is debounced (`edit_debounce`, 3 s), then sent as a
    notice "Alice 编辑了消息: 「old」→「new」" (may trigger)
  - edits whose content is unchanged (reactions, pins, …) are ignored
- **Deletions:**
  - a still-queued message is dropped
  - for a message MaiBot has seen, a notice "X发送的一条消息被删除了: 「…」"
  - user accounts get deletions without a chat id for private chats and basic groups; they are
    resolved from the store (one shared id sequence)
- **[R8] Silence window:** off by default, because MaiBot already waits. Edits and deletions count
  as activity.
- **[R9] Typing hold:** on by default for user accounts (bots never receive typing). Any typing,
  recording or uploading action holds the queue, up to `typing_max_hold` (20 s).
- **[R10] Lazy push:** `off` (default) / `count` / `interval` / `adaptive`. Adaptive batch size is
  about one message per 10 s of the chat's recent pace (EMA of intervals), starting at
  `lazy_count` and capped at `lazy_max_count`.
- **Mentions and private chats** skip the silence window and lazy push (`urgent_bypass`); the
  typing hold still applies.
- **[R11] History polling:** `auto` means bots only. It watches the last `poll_count` messages
  per chat that MaiBot saw in the past hour. Each is re-read when its age passes 1, 3, 7, 15 and
  31 × `poll_interval` (gaps double, about 5 reads instead of 60), so quiet chats stop costing
  requests. Edits and deletions it finds go through `maisaka.context.append` only and never
  trigger a reply [R11.2].
- **[R12] `command_filter_mode = stack`:** filtered commands wait for the next real message and
  are delivered with it. If none arrives within `max_wait`, they go to MaiBot as context only.
- New capabilities: `chat.get_all_streams`, `maisaka.context.append`. Store schema v4 adds
  sender name, text snapshot and topic id to `msg_meta`.
- **Done when:** a burst of messages or edits produces one dispatch, and polling surfaces an edit made
  on another client without producing a reply.

### Phase 5: Interaction ([R14], [R19], [R20], [R21], [R29]) — ✅ done 2026-09-30
As built:
- **Typing indicator [R19]:**
  - an OBSERVE hook on `maisaka.replyer.before_request`: when MaiBot starts generating a reply
    (attempt 1), send `SetTyping` once, with no refresh loop (`outbound.typing_indicator`)
  - The planner hook was rejected on purpose. It would show "typing…" on every planning round,
    even when MaiBot decides not to reply, and it ships the whole prompt context on each call.
- **Reactions in [R20]** (`inbound.reactions`, default `smart`):
  - reactions on the account's own messages, and long-press "big" reactions, become notices (may
    trigger a reply); others become context lines. `off` / `context` / `notice` are also available.
  - user accounts: the latest reactors from `UpdateMessageReactions` (entries newer than 2 minutes
    and not seen before, including the `big` flag); counts when Telegram hides who reacted
  - bots: `UpdateBotMessageReaction` (the bot must be an admin), with no `big` flag
  - only for messages MaiBot has seen
- **Reactions out [R20]:** Tool `telegram_react(msg_id, emoji, big)` (visible) and API `react`.
  Rejected emoji return the list of standard reactions.
- **Quote control [R14, R21]:** already native. MaiBot's `reply` tool has `set_quote`; the adapter
  honours it, with the overrides `outbound.quote_policy = never` and `outbound.reply_to_bots`.
  No extra tool needed.
- **Buttons [R29]** (bot accounts only):
  - Tool `telegram_send_buttons(text, buttons)` (deferred) and API `send_buttons`; data of buttons
    MaiBot sends is registered automatically
  - other presses are accepted only when their data matches `inbound.callback_patterns` or a
    pattern registered through API `register_callback_pattern` (`""` accepts all)
  - accepted presses reach MaiBot as an ordinary message "[点击了按钮「label」]" (not a notice), so
    MaiBot can answer; every press is answered so the button stops spinning
- `streams.py`: MaiBot stream ↔ Telegram chat mapping (filtered by account), shared by tools, hooks
  and context updates
- **Done when:** "typing…" appears once while MaiBot generates, reactions work both ways, and a
  registered button press reaches the core.

### Phase 6: Capabilities exposed to the core ([R4], [R4.1], [R4.2]) — ✅ done 2026-09-30
As built. Tools are for the LLM; APIs are for other plugins, called as `kumatea.telegram-full.<name>`.

| Tool | API | What it does |
|---|---|---|
| `telegram_react` (visible) | `react` | reaction on a message (Phase 5) |
| `telegram_send_buttons` (deferred) | `send_buttons` | inline keyboard, bots only (Phase 5) |
| `telegram_find_stickers` (deferred) | `find_stickers` | stickers seen in chats, by emoji, with MaiBot's descriptions |
| `telegram_send_sticker` (deferred) | `send_sticker` | resend a known sticker natively by reference |
| `telegram_get_message` (deferred) | `get_message` | read one message (text, sender, time, reply target), e.g. an old quoted one |
| `telegram_chat_info` (deferred) | `get_chat_info` | title, type, username, member count, description |
| `telegram_raw_api` (deferred, **off**) | `raw_invoke` | any MTProto method [R4.1, R4.2] |
| — | `register_callback_pattern` / `unregister_callback_pattern` | button callbacks (Phase 5) |

- **Raw MTProto** (`backend/raw_api.py`, config section `advanced` / 高级):
  - off by default. The SDK always registers tools as enabled, so the plugin disables the tool
    via `component.disable` on every load and config change, and `raw_invoke` refuses while
    `advanced.raw_api_enabled` is off. The LLM only sees the tool while the switch is on.
  - input: TL method name (`messages.getHistory`, `messages.GetHistoryRequest`, …) plus JSON params
    (snake_case or camelCase; nested TL objects as `{"_": "Type", …}` like Telethon's `to_dict()`;
    `"$chat"` means the current chat). Telethon resolves peers given by id or username.
  - allow / deny glob lists on the canonical name. The default allow list is read-only
    (`*.get*`, `*.search*`, `*.check*`, `contacts.resolve*`); an empty allow list allows nothing.
    The default deny list covers auth, account, payments, phone and sticker-set management, deletes,
    leaves, reports, blocks, admin/ban/creator edits and `messages.getBotCallbackAnswer` (presses a
    button).
  - result: JSON-safe `to_dict()` (bytes as base64, dates ISO), truncated to
    `raw_api_max_result_chars`. Every call is logged at WARNING level as an audit trail.
  - the tool description tells the LLM it is high-risk and to check https://tl.telethon.dev first
- New capabilities: `component.enable`, `component.disable`.
- **Done when:** the tools show up in MaiBot's tool list; raw invoke is refused when off and works for
  a harmless read call when on.

### Phase 7: Long text ([R26.1], [R26.2], [R26.3]) — ✅ done 2026-09-30
As built (`outbound/telegraph.py`):
- **Corrections to the requirement text:**
  - Telegraph pages *can* be edited (`editPage` with the creating account's token), but not
    deleted. Implemented:
    - every published page is recorded with the token that created it
    - Tool `telegram_edit_long_text(page, markdown, title, clear)` (deferred; the post tool's
      description and result point to it) and APIs `edit_long_text` / `list_long_texts`
    - `clear=true` overwrites title and body with a placeholder ("（已清空）"), the closest thing to
      deleting
    - only recorded pages can be edited; any other page is refused without asking Telegraph, and
      the error lists the editable pages
  - **Cocoon AI summary** (`messages.summarizeText(peer, id)`), tested 2026-09-30 from @Kuma_AI
    (not Premium):
    - it summarizes the *message text*; a link-only message gets a summary of the placeholder
      itself ("The message is a placeholder … link to a Telegra.ph article …"), not of the article
    - summaries for new messages come back immediately. A cached one stays in the API after the
      linked page changes, although the app then hides it.
    - the non-Premium quota is tiny: `SUMMARY_FLOOD_PREMIUM` (406) after about 5 calls
- **Chat message:**
  - bots always send `outbound.long_text_notice` ("消息过长，点击查看：") plus the link; no excerpt
  - user accounts with `outbound.long_text_ai_summary` (default on): the article's first ≤4000
    UTF-16 units go to the account's Saved Messages, `summarizeText` is called once (with
    `to_lang=zh` for Chinese text), the temporary message is deleted, and the chat gets
    "summary + link"
  - quota used up or any failure falls back to the notice
  - verified 2026-09-30: a Chinese summary came back and the temporary message was deleted. The
    fallback was verified earlier, while the quota was used up.
- **Tool `telegram_post_long_text(title, markdown)`** (visible, because the "too long" error names
  it) and **API `post_long_text`**:
  - publishes the page, then sends the notice or AI summary plus the link (link preview on, so
    Telegram shows Instant View)
  - the API can also return just the link (`send_link=False` or no `stream_id`) [R26.2]
- **Account [R26.1]:** created once on first use and stored. Author name and link default to
  the account's name and t.me link (`outbound.telegraph_author_*`). An invalid token triggers one
  re-creation.
- **Content:** the same markdown parser as messages, with single newlines as `<br>`, mapped to
  Telegraph's tags (headings → h3/h4, tables → `a | b` lines, spoilers → plain text,
  unsupported tags unwrapped). Raw HTML embeds (iframe / video) are unwrapped and links keep only
  http(s) / mailto / tg URLs, because the LLM's input includes other people's messages. LaTeX is
  converted as in messages. 64 KB limit checked.
- Messages over 4096 are still refused (as required); the error and the >1000 warning now name
  the tool.
- **Done when:** a 5000-character reply is refused with guidance, and posting it to Telegraph returns
  a link plus a summary.

### Phase 8: Telethon v2 migration ([R2]), deferred
- Starts once v2 is published on PyPI.
- Port `backend/` to v2 (a migration, not a second backend), or add thin compatibility shims if
  they turn out cheap.
- Rerun the whole test suite.

### Phase 9: Polish — ✅ done 2026-09-30
- README.md (zh-CN) and README.en.md:
  - features, install, quick start, the adapter-policy allow list (a must for user accounts)
  - tools and APIs, the full config reference, migration from exynos967's adapter, cautions,
    development
- CHANGELOG.md, requirements.txt (manual installs), LICENSE (GPL-3.0-or-later, as MaiBot)
- manifest: `license`, `urls.documentation`, `display.icon` (lucide `send`, #229ED9) and
  `changelog`. MaiBot's validator accepts it.
- **Publishing checklist** (https://docs.mai-mai.org/plugin/submission, checked 2026-09-30):

  | Requirement | Status |
  |---|---|
  | public repo; root `_manifest.json` v2, `plugin.py` with `create_plugin()`, `LICENSE`, `README.md` | ✅ |
  | stable id without spaces / path characters | ✅ `kumatea.telegram-full`, no clash in the index |
  | x.y.z versions; no patch-level upper bounds on `host_application` / `sdk` | ✅ `1.2.0–1.99.99`, `2.8.0–2.99.99` |
  | `author {name, url}`; `urls.repository` HTTPS without `.git` | ✅ |
  | only needed capabilities | ✅ all five are used |
  | tested in a real MaiBot | ✅ test container (MaiBot 1.2.5) |
  | release tag = manifest version | to do: `v0.1.0` on the commit that contains the manifest |

  To submit: open a plugin-repo issue with the "Add Plugin / 添加插件" template, plugin id
  `kumatea.telegram-full` and the repository URL.

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
