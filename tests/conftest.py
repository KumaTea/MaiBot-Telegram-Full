from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tg_full.backend.models import Message, Peer  # noqa: E402

ME = Peer(6466094355, "bot", "Mai", "rbevbot")
GROUP = Peer(-1001214803045, "supergroup", "Test Group")
ALICE = Peer(1001, "user", "Alice", "alice")
OTHER_BOT = Peer(2002, "bot", "Other Bot", "otherbot")


def make_message(text: str = "hi", **kwargs) -> Message:
    defaults = {
        "chat": GROUP,
        "id": 10,
        "date": datetime.now(timezone.utc),
        "sender": ALICE,
        "outgoing": False,
        "text": text,
    }
    defaults.update(kwargs)
    return Message(**defaults)


@pytest.fixture
def plugin_module() -> ModuleType:
    """Import plugin.py the way MaiBot's runner does (a synthetic package over the plugin dir)."""
    name = "_maibot_plugin_kumatea_telegram_full"
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugin.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
