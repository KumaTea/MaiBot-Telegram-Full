# MaiBot Telegram Full

基于 [Telethon](https://codeberg.org/Lonami/Telethon)（MTProto）的
[MaiBot](https://github.com/Mai-with-u/MaiBot)
[Telegram](https://telegram.org) 全功能适配器。同时支持 **Bot 账号** 与 **用户账号**（userbot）。

[English](README.en.md)

## 功能一览

| 类别 | 内容 |
|---|---|
| 账号 | Bot Token；用户账号支持手机号验证码（在 WebUI 填写）、两步验证、直接使用现有 Telethon 会话文件 |
| 连接 | 断线自动重连（Telethon 快速重连 + 指数退避），重连或重启后补收离线期间的消息；支持 SOCKS / HTTP 代理 |
| 消息 | Telegram 格式 ⇄ markdown（粗体、斜体、删除线、剧透、代码、引用、链接、提及）；LaTeX 公式自动转为纯文本（`a²`）；回复、@、转发来源、频道与匿名管理员身份、论坛话题 |
| 媒体 | 图片、贴纸（附 `[贴纸 😂]` 提示）、语音、文件；**同一文件复用 MaiBot 已有识别结果，不重复下载与识别**；MaiBot 发出的表情若来自 Telegram，按原文件发送（原生贴纸 / 动图）；GIF 可转为真正的 GIF 供识别；链接预览，无预览时自动抓取网页标题与简介 |
| 投递节奏 | 编辑合并后通知、删除通知、等待对方输入完成、静默窗口、攒批（按条数 / 间隔 / 自适应）、Bot 账号历史轮询（只更新上下文，不触发回复） |
| 过滤 | 其他 Bot 的消息、发给其他 Bot 的命令（丢弃，或保留到下一条消息一起投递） |
| 互动 | 生成回复时显示一次“正在输入”；表情回应的接收与发送（含长按大表情）；内联按钮与回调 |
| 长文 | 超过 4096 字的消息拒绝发送并提示使用长文工具；发布到 Telegra.ph，可修改、可清空；用户账号可用 Telegram AI 摘要作为消息正文 |
| 高级 | 直接调用任意 MTProto 方法（默认关闭，带允许 / 禁止名单） |

## 安装

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/KumaTea/MaiBot-Telegram-Full.git kumatea_telegram-full
```

重启 MaiBot 后插件会被识别为**适配器**。Python 依赖由 MaiBot 按 `_manifest.json` 自动安装：
`telethon>=1.45,<2`、`cryptg`、`markdown-it-py`、`python-socks`、`Pillow`、`aiohttp`（手动安装可用 `requirements.txt`）。

> `cryptg` 让加解密快约 60 倍，已作为必需依赖。它为 glibc Linux（x86_64 / aarch64）、Windows、Apple Silicon macOS 提供预编译包；其他平台需要 Rust 才能编译。

## 快速开始

1. 在 <https://my.telegram.org> 申请 **API ID / API Hash**。MTProto 协议要求 Bot 账号也必须提供。
2. 在 WebUI 的插件配置（或插件目录的 `config.toml`）中填写 `account` 部分：
   - **Bot 账号**：`type = "bot"`，填写 `bot_token`（从 [@BotFather](https://t.me/BotFather) 获取）。
     在群里接收全部消息需要向 BotFather 发送 `/setprivacy` 关闭隐私模式，或把 Bot 设为管理员（接收表情回应必须是管理员）。
   - **用户账号**：`type = "user"`，填写 `phone`；账号开了两步验证就填写 `password`。
     首次启动时 Telegram 会把验证码发到你的其他设备，把它填入 `login_code` 并保存即可完成登录。
     也可以把现有的 Telethon 会话文件放到 `data/plugins/kumatea.telegram-full/<session_name>.session`，此时无需手机号。
3. 打开 `plugin.enabled` 并保存。日志出现 `Connected to Telegram as …` 即连接成功。
4. **用户账号请务必设置聊天名单**（见下节），否则 MaiBot 会在该账号所在的所有群里说话，并回复所有私聊。

### 聊天名单（MaiBot 统一适配器名单）

本适配器不自带黑白名单，而是使用 MaiBot 的统一适配器名单 `config/adapter_policy.toml`（也可在 WebUI 的适配器管理中编辑）。
例如只允许用户账号在一个群里说话、不接收私聊：

```toml
[[adapters]]
plugin_id = "kumatea.telegram-full"
account_id = "123456789"            # 账号的数字 ID，见日志 "Connected to Telegram as …"

[adapters.group]
list_type = "whitelist"
ids = ["-1001234567890"]             # 群 / 超级群 ID（带 -100 前缀）

[adapters.private]
list_type = "whitelist"
ids = []
```

被拒绝的聊天会被记住 5 分钟，期间不会为它下载任何媒体。

> 论坛话题的群号形如 `<群ID>::tg-topic::mt=<话题ID>`，白名单需要写到具体话题。

## 给 MaiBot 的工具与给插件的 API

| 工具（LLM 调用） | API（`kumatea.telegram-full.<名称>`） | 作用 |
|---|---|---|
| `telegram_react` | `react` | 给消息添加表情回应，可播放大动画 |
| `telegram_post_long_text` | `post_long_text` | 发布长文到 Telegra.ph 并发送链接 |
| `telegram_edit_long_text` | `edit_long_text` | 修改或清空已发布的 Telegraph 页面 |
| — | `list_long_texts` | 已发布的 Telegraph 页面 |
| `telegram_find_stickers` | `find_stickers` | 按 emoji 查找见过的贴纸（附识别描述） |
| `telegram_send_sticker` | `send_sticker` | 发送原生贴纸 |
| `telegram_get_message` | `get_message` | 读取一条消息（例如不在上下文里的旧消息） |
| `telegram_chat_info` | `get_chat_info` | 聊天名称、类型、成员数、简介 |
| `telegram_send_buttons` | `send_buttons` | 发送带内联按钮的消息（仅 Bot） |
| — | `register_callback_pattern` / `unregister_callback_pattern` | 登记要接收的按钮回调 |
| `telegram_raw_api` | `raw_invoke` | 直接调用 MTProto 方法（默认关闭） |

`telegram_react` 与 `telegram_post_long_text` 始终对 MaiBot 可见，其余工具按需加载。
另有 Hook `telegram_typing_indicator`：MaiBot 开始生成回复时显示一次“正在输入”。

## 配置参考

所有配置都可在 WebUI 修改并热更新；只有账号与连接相关的修改会触发重连。

<details>
<summary><b>account 账号</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `type` | `bot` | `bot` 或 `user` |
| `api_id` / `api_hash` | — | 必填，来自 my.telegram.org |
| `bot_token` | — | Bot 账号必填 |
| `phone` | — | 用户账号首次登录时必填，含国家码 |
| `password` | — | 两步验证密码；账号开启了两步验证却未填写时，登录会直接失败 |
| `login_code` | — | 登录验证码，适配器提示后填写并保存 |
| `session_name` | `telegram_full` | 会话文件名；更换账号时请改名 |

</details>

<details>
<summary><b>connection 连接</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `proxy` | — | `socks5://user:pass@host:port`、`socks4://…`、`http://…` |
| `reconnect_max_delay` | `300` | 重连退避上限（秒） |
| `flood_sleep_threshold` | `60` | FloodWait 自动等待上限（秒） |
| `telethon_log_level` | `WARNING` | Telethon 日志级别 |
| `catch_up` | `true` | 重连或重启后补收离线期间的消息（进度保存在会话文件中，类似 Bot API 的 update offset） |
| `catch_up_max_age` | `10` | 补收到的消息早于这么多分钟前发出时不单独触发回复，随下一条新消息一起投递，否则只作为上下文；`0` 不限 |

</details>

<details>
<summary><b>inbound 接收</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `ignore_bot_messages` | `true` | 丢弃其他 Bot 的消息 |
| `command_filter` | `other_bots` | `off` / `other_bots`（丢弃 `/cmd@其他bot`） / `all`（丢弃全部命令，MaiBot 插件命令也会失效） |
| `command_filter_mode` | `drop` | `drop` 丢弃；`stack` 不单独触发回复，随下一条普通消息一起投递 |
| `own_messages` | `context` | 用户账号在其他设备上亲自发的消息：作为麦麦自己说的话，或丢弃 |
| `reply_preview_length` | `200` | 被回复消息的预览长度 |
| `reactions` | `smart` | `off` / `context` / `smart`（本账号消息的回应与大表情以通知告知，其余进上下文） / `notice` |
| `callback_patterns` | `[]` | 要接收的按钮回调正则；MaiBot 自己发的按钮会自动登记 |

</details>

<details>
<summary><b>dispatch 投递节奏</b></summary>

MaiBot 自身会在新消息后等待约 1 秒并为积压消息评分，因此静默窗口与攒批默认关闭。

| 键 | 默认 | 说明 |
|---|---|---|
| `edit_debounce` | `3` | 编辑后等待多少秒无新编辑再通知 |
| `edit_notice` / `deletion_notice` | `true` | 告知编辑 / 删除（以通知形式，可能引发回复） |
| `silence_window` | `0` | 聊天静默多少秒后才投递 |
| `typing_hold` / `typing_max_hold` | `true` / `20` | 有人正在输入时暂缓投递（仅用户账号能收到输入状态） |
| `lazy_mode` | `off` | `off` / `count` / `interval` / `adaptive` |
| `lazy_count` / `lazy_interval` / `lazy_max_count` | `3` / `10` / `8` | 攒批参数 |
| `max_wait` | `60` | 任何消息最多等待的秒数 |
| `urgent_bypass` | `true` | @ 我、回复我、私聊跳过静默窗口与攒批 |
| `history_poll` | `auto` | 定期重读最近消息发现编辑与删除（`auto` = 仅 Bot），只更新上下文 |
| `poll_interval` / `poll_count` | `60` / `20` | 轮询间隔与每个聊天关注的最近条数；每条消息在约 1、3、7、15、31 个间隔时各重读一次，间隔逐次翻倍 |

</details>

<details>
<summary><b>outbound 发送</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `text_format` | `markdown` | `markdown` 或 `plain` |
| `quote_policy` | `core` | 由 MaiBot 决定是否引用，或 `never` |
| `reply_to_bots` | `no_quote` | 回复其他 Bot：`normal` / `no_quote` / `drop` |
| `link_preview` | `false` | 发送消息时显示链接预览 |
| `soft_length_warning` | `1000` | 文本宽度（中日韩字符计 2、emoji 计 3）超过此值时记录警告；超过 4096 一律拒绝 |
| `typing_indicator` | `true` | 生成回复时显示一次“正在输入” |
| `telegraph_author_name` / `telegraph_author_url` | 账号名 / t.me 链接 | Telegraph 署名 |
| `long_text_notice` | `消息过长，点击查看：` | 长文链接前的提示语 |
| `long_text_ai_summary` | `true` | 仅用户账号：用 Telegram AI 摘要作为长文消息正文（额度用尽时退回提示语） |

</details>

<details>
<summary><b>media 媒体</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `recognition_cache` | `true` | 同一文件复用 MaiBot 的识别结果，不再下载 |
| `native_resend` | `true` | MaiBot 发送的已知 Telegram 文件按原文件转发 |
| `sticker_emoji_hint` | `true` | 贴纸前附上 `[贴纸 😂]` |
| `animation` | `thumbnail` | GIF 与视频贴纸：`thumbnail` / `gif`（需要 PyAV） / `drop` |
| `install_pyav` | `false` | `gif` 模式下允许自动安装 PyAV（约 30–60 MB，自带 ffmpeg） |
| `video_thumbnail` | `false` | 为视频附缩略图供识别（消耗识图额度） |
| `link_preview` | `fetch` | `off` / `telegram`（仅用 Telegram 预览） / `fetch`（无预览时自行抓取） |
| `link_user_agents` | `googlebot, browser, curl, default` | 抓取网页时依次尝试的 UA；也可写完整 UA 字符串，`none` 表示不发送 |
| `link_timeout` | `4` | 每次请求超时（秒） |

</details>

<details>
<summary><b>advanced 高级</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `raw_api_enabled` | `false` | 开放原始 MTProto 调用；关闭时工具不会出现在 MaiBot 的工具列表中 |
| `raw_api_allow` | `["*"]` | 允许的方法（通配符，按 TL 名称，如 `messages.*`） |
| `raw_api_deny` | 见说明 | 默认禁止登录、账号、支付、通话、贴纸包管理，以及删除、退群、举报、拉黑、修改权限等方法 |
| `raw_api_max_result_chars` | `8000` | 返回结果的最大长度 |

</details>

## 从 exynos967/MaiBot-Telegram-Adapter 迁移

- **同一个 Bot Token 不能同时被两个适配器使用**：请先停用旧插件，或给新适配器换一个 Bot。
- 需要额外填写 `api_id` / `api_hash`。
- 平台名同为 `telegram`，群号与论坛话题格式相同，因此已有的聊天流、记忆与人物信息可以沿用。
  主配置中的 `platforms = ["telegram:<Bot ID>"]` 不需要修改（适配器会上报自身账号）。
- 消息 ID 改为 `群ID:消息ID` 以避免不同群之间冲突；回复迁移前的旧消息时，MaiBot 看不到被回复内容的预览。
- 旧插件的 `group_list` / `private_list` / `ban_user_id` 请改为 MaiBot 统一适配器名单。
- 旧插件的 `api_base`（自建 Bot API 服务器）不再需要；代理改用 `connection.proxy`。

## 注意事项

- **用户账号属于非官方客户端，存在被 Telegram 限制或封禁的风险。** 建议使用小号，并通过名单限制活动范围。
- **Telegraph 页面对任何拿到链接的人公开，且无法删除**；只能修改或清空。
- `telegram_raw_api` 可以做任何事，开启前请确认允许 / 禁止名单。所有调用都会以 WARNING 级别记入日志。
- 使用 Clash / mihomo / sing-box 等 fake-ip 代理时无需特别配置。

## 开发

```bash
uv sync                 # 开发依赖（含用于测试 GIF 转换的 PyAV）
uv run pytest -q
uv run ruff check .
```

代码结构见 [PLAN.md](PLAN.md)：`plugin.py` 只负责组件声明，功能都在 `tg_full/` 中，Telethon 只在 `tg_full/backend/` 中被导入。

## 许可证

[GPL-3.0-or-later](LICENSE)

图标 `assets/icon.svg` 改自 [m8rge 的 Telegram 图标](https://gist.github.com/m8rge/4c2b36369c9f936c02ee883ca8ec89f1)，MIT 许可证（全文见该文件内的注释）。
