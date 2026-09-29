import json
from pathlib import Path

from tg_full.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_is_adapter():
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["plugin_type"] == "adapter"
    names = {d["name"] for d in manifest["dependencies"] if d["type"] == "python_package"}
    assert "telethon" in names


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
