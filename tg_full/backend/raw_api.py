"""Raw MTProto calls through Telethon (requirements R4.1 / R4.2). Off by default.

A call names a TL method and gives JSON parameters, e.g.::

    method = "messages.getHistory"          # or "messages.GetHistoryRequest"
    params = {"peer": -1001234567890, "limit": 5, "offset_id": 0, "offset_date": 0,
              "add_offset": 0, "max_id": 0, "min_id": 0, "hash": 0}

* Parameter names may be snake_case (Telethon) or camelCase (TL docs).
* Nested TL objects are written like Telethon's ``to_dict()`` output: ``{"_": "ReactionEmoji",
  "emoticon": "👍"}``. Bytes can be ``{"_": "bytes", "base64": "…"}`` or a plain string.
* Peers, users and channels can be given as ids or usernames: Telethon resolves them when the
  request is sent.
* Every method is checked against allow / deny glob lists on the canonical ``namespace.method``
  name (TL spelling, e.g. ``messages.deleteMessages``). The default allow list only has read-only
  methods; an empty allow list allows nothing.
"""

from __future__ import annotations

import base64
import datetime
import fnmatch
import inspect
import json
import re
from typing import Any

from telethon.tl import functions, types
from telethon.tl.tlobject import TLObject, TLRequest


class RawApiError(ValueError):
    pass


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def resolve_method(name: str) -> tuple[type[TLRequest], str]:
    """Telethon request class and canonical ``namespace.method`` name for ``name``."""
    raw = name.strip().removeprefix("functions.")
    namespace, _, method = raw.rpartition(".")
    method = method.removesuffix("Request")
    if not method:
        raise RawApiError(f"Invalid method name: {name!r}")
    class_name = method[0].upper() + method[1:] + "Request"
    module = getattr(functions, namespace, None) if namespace else functions
    request_class = getattr(module, class_name, None) if module is not None else None
    if not (inspect.isclass(request_class) and issubclass(request_class, TLRequest)):
        raise RawApiError(f"Unknown MTProto method: {name!r}. Check https://tl.telethon.dev for the exact name.")
    canonical = f"{namespace}.{method[0].lower()}{method[1:]}" if namespace else method[0].lower() + method[1:]
    return request_class, canonical


def is_allowed(canonical: str, allow: list[str], deny: list[str]) -> bool:
    if any(fnmatch.fnmatchcase(canonical, pattern) for pattern in deny):
        return False
    return any(fnmatch.fnmatchcase(canonical, pattern) for pattern in allow)


def _signature_text(cls: type) -> str:
    params = [p for p in inspect.signature(cls.__init__).parameters.values() if p.name != "self"]
    return ", ".join(f"{p.name}{'' if p.default is inspect.Parameter.empty else '?'}" for p in params)


def _build_value(value: Any, annotation: str = "") -> Any:
    if isinstance(value, list):
        return [_build_value(item, annotation) for item in value]
    if isinstance(value, dict):
        kind = value.get("_")
        if kind == "bytes":
            return base64.b64decode(value.get("base64") or "")
        if isinstance(kind, str):
            cls = getattr(types, kind, None)
            if not (inspect.isclass(cls) and issubclass(cls, TLObject)):
                raise RawApiError(f"Unknown TL type {kind!r}")
            return build_object(cls, {k: v for k, v in value.items() if k != "_"})
        raise RawApiError("Nested objects need a \"_\" key naming their TL type, e.g. {\"_\": \"ReactionEmoji\"}")
    if isinstance(value, str) and "bytes" in annotation:
        return value.encode()
    return value


def build_object(cls: type, params: dict[str, Any]) -> Any:
    signature = inspect.signature(cls.__init__)
    kwargs = {}
    for key, value in params.items():
        name = key if key in signature.parameters else _snake(key)
        if name not in signature.parameters or name == "self":
            raise RawApiError(f"{cls.__name__} has no parameter {key!r}. Parameters: {_signature_text(cls)}")
        annotation = str(signature.parameters[name].annotation)
        kwargs[name] = _build_value(value, annotation)
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise RawApiError(f"{cls.__name__}({_signature_text(cls)}): {exc}") from exc


def build_request(method: str, params: dict[str, Any] | None, allow: list[str], deny: list[str]) -> tuple[TLRequest, str]:
    request_class, canonical = resolve_method(method)
    if not is_allowed(canonical, allow, deny):
        raise RawApiError(
            f"{canonical} is blocked by the adapter's raw API allow / deny lists "
            "(advanced.raw_api_allow / raw_api_deny; by default only read-only methods are allowed)"
        )
    if params is not None and not isinstance(params, dict):
        raise RawApiError("params must be a JSON object")
    return build_object(request_class, params or {}), canonical


def _json_safe(value: Any) -> Any:
    if isinstance(value, TLObject):
        return _json_safe(value.to_dict())
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bytes):
        return {"_": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def result_to_json(result: Any, max_chars: int) -> tuple[str, bool]:
    """JSON text of a result, and whether it was truncated to ``max_chars``."""
    text = json.dumps(_json_safe(result), ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
