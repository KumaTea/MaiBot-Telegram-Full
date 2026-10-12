# MaiBot Telegram Full

基于 [Telethon](https://codeberg.org/Lonami/Telethon) (MTProto) 的
[MaiBot](https://github.com/Mai-with-u/MaiBot)
[Telegram](https://telegram.org) 全功能适配器。

本适配器支持 Bot 与用户账号 (userbot)。
相较于已存在的 Telegram 插件，本适配器面向重度用户，并不以轻量为设计目标，依赖全功能
Telethon 运行库，且基于 Telegram 使用 MP4 处理动图的机制，依赖
[PyAV](https://github.com/pyav-org/pyav) 媒体处理库。在内存等资源足够的情况下，通过
[cryptg](https://github.com/cher-nov/cryptg) 库的加速，可以取得比 HTTP Bot API 显著提高的性能体验。

## 功能

| 类别 | 内容 |
|---|---|
| 账号 | 支持 Bot 与用户账号 (userbot) |
| 连接 | 断线自动重连，原生支持代理 |
| 消息 | 支持 markdown 格式；回复、@、转发来源、频道与匿名管理员身份、论坛话题 |
| 媒体 | 图片、贴纸、语音、文件；复用已有识别结果；来自 Telegram 的按原文件发送；GIF 转码；链接预览 |
| 投递节奏 | 编辑、删除通知，等待输入，静默窗口，积攒推送，历史轮询 |
| 过滤 | 其他 Bot，发给其他 Bot 的命令 |
| 互动 | 显示正在输入；表情回应；按钮消息 |
| 长文 | 发布到 Telegra.ph；Telegram AI 摘要 |
| 高级 | 调用 MTProto 方法 |

## 安装

在 [MaiBot 插件市场](https://plugins.maibot.chat) 搜索 **`Telegram Full`** 安装。

手动安装：

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/KumaTea/MaiBot-Telegram-Full.git kumatea_telegram-full
```

`cryptg` 与 `av` (PyAV 用于 GIF 转码) 为必需依赖，未提供预编译的平台可能安装失败。

## 快速开始

1. 在 <https://my.telegram.org> 申请 API ID / API Hash。Bot 账号也必须提供。
2. 在 WebUI 的插件配置填写 `account` 部分：
   - Bot 账号：`type = "bot"`，填写 `bot_token`。
     在群里接收全部消息需要向 BotFather 发送 `/setprivacy` 关闭隐私模式，或把 Bot 设为管理员（管理员才能即时收到表情回应，否则由历史轮询稍后发现）
     可选：打开 `user_helper` 并按下一条填写用户账号，Bot 就能使用长文 AI 摘要和 Telegram 链接预览这两项 Bot 无法调用的功能。
     该用户账号只做这两项（摘要会在它自己的收藏夹里临时发一条并立即删除），不在任何聊天收发消息，也不读取它的聊天；登录失败不影响 Bot。
   - 用户账号：`type = "user"`，填写 `phone`；若启用两步验证则填写 `password`。
     首次启动时 Telegram 会发送验证码到其他设备，填入 `login_code` 并保存即可完成登录。
3. 打开 `plugin.enabled` 并保存。日志出现 `Connected to Telegram as …` 即连接成功。
4. 用户账号请务必设置聊天名单，否则 MaiBot 会在该账号所在的所有群里说话，并回复所有私聊。

### 聊天名单（MaiBot 统一适配器名单）

本适配器不自带黑白名单，而使用 MaiBot 的统一适配器名单 `config/adapter_policy.toml`（可在 WebUI 的适配器管理中编辑）。
例如只允许用户账号在一个群里说话、不接收私聊：

```toml
[[adapters]]
plugin_id = "kumatea.telegram-full"
account_id = "123456789"            # 账号的数字 ID，见日志 "Connected to Telegram as …"

[adapters.group]
list_type = "whitelist"
ids = ["-1001234567890"]             # 群 / 超级群 ID（-100 前缀）

[adapters.private]
list_type = "whitelist"
ids = []
```

## Exposed API

| 工具 | API | 作用 |
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
