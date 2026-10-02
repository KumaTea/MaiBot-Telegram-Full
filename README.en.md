# MaiBot Telegram Full

A full-featured [Telegram](https://telegram.org) adapter for [MaiBot](https://github.com/Mai-with-u/MaiBot),
built on [Telethon](https://codeberg.org/Lonami/Telethon) (MTProto). It works with **bot accounts** and
**user accounts** (userbots).

[中文](README.md)

## Features

| Area | What you get |
|---|---|
| Accounts | Bot token; user accounts with a login code entered in the WebUI, two-step verification, or an existing Telethon session file |
| Connection | Automatic reconnection (Telethon's retries plus exponential backoff), catching up on messages missed while offline; SOCKS / HTTP proxies |
| Messages | Telegram formatting ⇄ markdown (bold, italic, strike, spoiler, code, quotes, links, mentions); LaTeX turned into plain text (`a²`); replies, mentions, forward origins, channel and anonymous-admin identities, forum topics |
| Media | Photos, stickers (with a `[贴纸 😂]` hint), voice, files. **A file MaiBot already recognized is neither downloaded nor recognized again.** Known Telegram files MaiBot sends back are resent natively (real stickers / GIFs). GIFs can be converted to real GIFs for recognition. Link previews, with page title and description fetched when Telegram has none |
| Delivery timing | Debounced edit notices, deletion notices, waiting while someone types, silence window, batching (by count, interval or chat activity), history polling for bots (context only, never triggers a reply) |
| Filters | Messages from other bots; commands for other bots (dropped, or delivered with the next message) |
| Interaction | "typing…" once while MaiBot writes a reply; reactions in and out (including long-press big reactions); inline buttons and callbacks |
| Long text | Messages over 4096 characters are refused with a pointer to the long-text tool; publishing to Telegra.ph with editing and clearing; user accounts (or bots with the user account helper) can use Telegram's AI summary as the message body |
| Advanced | Call any MTProto method directly (off by default, with allow / deny lists) |

## Installation

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/KumaTea/MaiBot-Telegram-Full.git kumatea_telegram-full
```

Restart MaiBot; the plugin is recognized as an **adapter**. MaiBot installs the Python dependencies from
`_manifest.json`: `telethon>=1.45,<2`, `cryptg`, `markdown-it-py`, `python-socks`, `Pillow`, `aiohttp`
(`requirements.txt` for manual installs).

> `cryptg` makes encryption about 60× faster and is required. Prebuilt wheels exist for glibc Linux
> (x86_64 / aarch64), Windows and Apple-silicon macOS; elsewhere it needs Rust to build.

## Quick start

1. Get an **API ID / API hash** at <https://my.telegram.org>. MTProto needs them for bots too.
2. Fill in the `account` section in the WebUI (or `config.toml` in the plugin folder):
   - **Bot:** `type = "bot"` and `bot_token` from [@BotFather](https://t.me/BotFather). To see all
     group messages, disable privacy mode (`/setprivacy`) or make the bot an admin (reactions need admin).
     Optional: turn on `user_helper` and fill in a user account as below. The bot can then use the two
     things bots cannot call: AI summaries for long texts and Telegram's own link previews. The helper
     does nothing else (a summary briefly puts a note in its own Saved Messages and deletes it), takes
     part in no chat and never reads its chats. If its login fails, the bot keeps working.
   - **User account:** `type = "user"`, `phone`, and `password` if two-step verification is on. On
     first start Telegram sends a code to your other devices; enter it in `login_code` and save.
     Alternatively put an existing Telethon session at `data/plugins/kumatea.telegram-full/<session_name>.session`.
3. Turn on `plugin.enabled`. `Connected to Telegram as …` in the log means it is online.
4. **For user accounts, set a chat allow list** (below), or MaiBot will talk in every group the account
   is in and answer every private chat.

### Chat allow / block lists

The adapter uses MaiBot's unified adapter policy (`config/adapter_policy.toml`, also editable in the
WebUI). For example, one group only and no private chats:

```toml
[[adapters]]
plugin_id = "kumatea.telegram-full"
account_id = "123456789"            # the account's numeric id, see "Connected to Telegram as …"

[adapters.group]
list_type = "whitelist"
ids = ["-1001234567890"]

[adapters.private]
list_type = "whitelist"
ids = []
```

Rejected chats are remembered for 5 minutes, so no media is downloaded for them. Forum topics have
group ids like `<chat id>::tg-topic::mt=<topic id>` and must be listed per topic.

## Tools for MaiBot and APIs for plugins

| Tool (LLM) | API (`kumatea.telegram-full.<name>`) | Purpose |
|---|---|---|
| `telegram_react` | `react` | react to a message, optionally with the big animation |
| `telegram_post_long_text` | `post_long_text` | publish a long text to Telegra.ph and send the link |
| `telegram_edit_long_text` | `edit_long_text` | edit or clear a published page |
| — | `list_long_texts` | pages published so far |
| `telegram_find_stickers` | `find_stickers` | stickers seen in chats, by emoji, with descriptions |
| `telegram_send_sticker` | `send_sticker` | send a native sticker |
| `telegram_get_message` | `get_message` | read one message, e.g. an old quoted one |
| `telegram_chat_info` | `get_chat_info` | chat name, type, member count, description |
| `telegram_send_buttons` | `send_buttons` | message with inline buttons (bots only) |
| — | `register_callback_pattern` / `unregister_callback_pattern` | accept button callbacks |
| `telegram_raw_api` | `raw_invoke` | any MTProto method (off by default) |

`telegram_react` and `telegram_post_long_text` are always visible to MaiBot; the others are loaded on
demand. The hook `telegram_typing_indicator` shows "typing…" once when MaiBot starts a reply.

## Configuration

Every option has an English label in the WebUI, and all of them hot-reload; only account and
connection changes reconnect. Section overview (see [the Chinese README](README.md#配置参考) for the full
table):

- `account`: type, api_id / api_hash, bot_token, user account helper (bots), phone, password, login_code,
  session_name
- `connection`: proxy, reconnect backoff, FloodWait limit, Telethon log level, catching up on missed messages
  (missed messages older than `catch_up_max_age` minutes, default 10, join the next new message instead of
  each triggering a reply)
- `inbound`: bot messages, command filter and mode, own messages, reply preview, reactions, callback patterns
- `dispatch`: edit debounce, edit / deletion notices, silence window, typing hold, lazy push, max wait,
  mention bypass, history polling with doubling gaps per message (MaiBot already waits about 1 s and scores the backlog, so the silence
  window and lazy push are off by default)
- `outbound`: markdown / plain, quote policy, replying to bots, link previews, length warning, typing
  indicator, Telegraph author, long-text notice and AI summary
- `media`: recognition reuse, native resend, sticker hint, animations (GIF / thumbnail / drop), video
  thumbnails, link previews, User-Agent chain and whether private addresses may be fetched
- `advanced`: raw MTProto calls and their allow / deny lists (read-only methods by default)

## Migrating from exynos967/MaiBot-Telegram-Adapter

- **One bot token cannot be used by two adapters at once**: disable the old plugin or use another bot.
- `api_id` / `api_hash` are needed in addition.
- The platform is still `telegram` and group / topic ids use the same format, so chat streams, memory and
  person records carry over. `platforms = ["telegram:<bot id>"]` in the main config can stay.
- Message ids are now `chat_id:msg_id` to avoid clashes between chats; replies to messages from before
  the migration show no preview of the quoted text.
- Replace the old `group_list` / `private_list` / `ban_user_id` with MaiBot's adapter policy.
- `api_base` (self-hosted Bot API) is no longer needed; use `connection.proxy` for proxies.

## Notes

- **User accounts are unofficial clients and can get limited or banned by Telegram.** Prefer a secondary
  account and restrict it with the allow list.
- **Telegraph pages are public to anyone with the link and cannot be deleted**, only edited or cleared.
- `telegram_raw_api` can do anything; review the allow / deny lists before enabling it. Every call is
  logged at WARNING level.
- Link previews do not fetch loopback / LAN / link-local addresses unless `media.link_allow_private` is
  on. Fake-ip proxies (Clash / mihomo / sing-box, 198.18.0.0/15) need no special setup.

## Development

```bash
uv sync                 # runtime and dev dependencies
uv run pytest -q
uv run ruff check .
```

See [PLAN.md](PLAN.md) for the design: `plugin.py` only declares components, everything else lives in
`tg_full/`, and Telethon is only imported in `tg_full/backend/`.

## License

[GPL-3.0-or-later](LICENSE)

The icon `assets/icon.svg` is adapted from [m8rge's Telegram icon](https://gist.github.com/m8rge/4c2b36369c9f936c02ee883ca8ec89f1),
MIT License (full text in a comment inside the file).
