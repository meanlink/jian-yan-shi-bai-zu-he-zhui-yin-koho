"""最小 JSON HTTP 客户端，基于标准库，支持超时与连接失败区分。"""
import json
import urllib.error
import urllib.request


class TransportError(RuntimeError):
    """网络层失败：连接拒绝、超时、重置——此时无法判断对端是否已收到。"""


def request(method, url, body=None, timeout=2.0):
    """返回 (http_status, dict)；网络层异常抛 TransportError。"""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransportError(str(e)) from e
