import json
from pathlib import Path

from tg_full.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_is_adapter():
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["plugin_type"] == "adapter"
    names = {d["name"] for d in manifest["dependencies"] if d["type"] == "python_package"}
    assert "telethon" in names
    from tg_full.constants import VERSION

    assert manifest["version"] == VERSION


def test_plugin_loads_like_maibot_runner(plugin_module):
    plugin = plugin_module.create_plugin()
    components = plugin.get_components()
    gateways = [c for c in components if c["name"] == "telegram_full_gateway"]
    assert gateways, components
    defaults = plugin.get_default_config()
    assert defaults["plugin"]["config_version"]
    assert defaults["plugin"]["enabled"] is False
    schema = plugin.get_webui_config_schema()
    assert schema


async def test_store_roundtrip(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    await store.open()
    await store.set_json("k", {"a": 1})
    assert await store.get_json("k") == {"a": 1}
    await store.record_message(-1, 5, 7, True, is_outgoing=False, routed=False)
    assert (await store.get_message_meta(-1, 5))["sender_is_bot"] is True
    assert await store.is_known_to_core(-1, 5) is False
    await store.mark_routed(-1, 5)
    assert await store.is_known_to_core(-1, 5) is True
    # A later non-routed upsert must not clear the routed flag.
    await store.record_message(-1, 5, 7, True, is_outgoing=False, routed=False)
    assert await store.is_known_to_core(-1, 5) is True
    await store.close()
    # Reopening keeps the schema version and data.
    store = Store(tmp_path / "s.sqlite3")
    await store.open()
    assert await store.get_json("k") == {"a": 1}
    await store.close()


def test_backend_only_uses_names_that_exist_in_installed_telethon():
    """Guards against Telegram layer changes (e.g. KeyboardButtonCallback vanished in layer 229)."""
    import re

    import telethon.tl.functions as tl_functions
    import telethon.tl.types as tl_types
    from telethon import errors

    for source in (ROOT / "tg_full" / "backend").glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for name in re.findall(r"\btypes\.([A-Z]\w+)", text):
            assert hasattr(tl_types, name), f"{source.name}: types.{name}"
        for module, name in re.findall(r"\bfunctions\.(\w+)\.(\w+)", text):
            assert hasattr(getattr(tl_functions, module), name), f"{source.name}: functions.{module}.{name}"
        for name in re.findall(r"\berrors\.([A-Z]\w+)", text):
            assert hasattr(errors, name), f"{source.name}: errors.{name}"
