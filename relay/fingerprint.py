"""内容指纹：双方必须使用完全一致的规范化口径。"""
import hashlib
import json


def canonical(payload) -> str:
    """规范化 JSON：键排序、无多余空白、不允许 NaN，保证字节级一致。"""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(payload) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def short(fp: str | None) -> str:
    return (fp or "")[:10]
