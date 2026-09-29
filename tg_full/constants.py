"""Shared constants."""

PLATFORM = "telegram"
GATEWAY_NAME = "telegram_full_gateway"
PROTOCOL = "mtproto"
CONFIG_VERSION = "0.1.0"

# Telegram limits, counted in UTF-16 code units after entity parsing.
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

STORE_FILENAME = "telegram_full.sqlite3"
