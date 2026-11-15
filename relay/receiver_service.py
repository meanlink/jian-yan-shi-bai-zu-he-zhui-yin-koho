"""接收端 HTTP 服务：模拟外部“接收系统”。

接口：
  POST /api/receive        幂等接收；重复投递不重复落库；同 ID 内容不符返回 409 冲突
  GET  /api/facts          全部接收事实（对账用）
  GET  /api/facts/{id}     单条事实，接收方主动查询“到底收没收到”
  GET  /api/receipts/{id}  该投递已签发的回执记录
  POST /api/chaos          演示故障注入 {name, enabled}
  GET  /ui                 接收端页面
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .fingerprint import fingerprint
from .receiver_store import ReceiverStore

RECEIVE_PATH = "/api/receive"


class ReceiverHandler(BaseHTTPRequestHandler):
    server_version = "RelayReceiver/1.0"

    # 共享在 server 对象上
    @property
    def store(self) -> ReceiverStore:
        return self.server.store

    def log_message(self, fmt, *args):  # 安静一点，演示脚本输出更清晰
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send_json(200, {"status": "ok", "role": "receiver"})
        if path == "/api/facts":
            if self.store.chaos_on("drop_facts"):
                return self._send_json(503, {"detail": "事实库暂时不可用（故障注入）"})
            return self._send_json(200, {"facts": self.store.list_facts()})
        if path.startswith("/api/facts/"):
            if self.store.chaos_on("drop_facts"):
                return self._send_json(503, {"detail": "事实库暂时不可用（故障注入）"})
            delivery_id = path.rsplit("/", 1)[1]
            fact = self.store.fact(delivery_id)
            if not fact:
                return self._send_json(404, {"detail": "无接收事实", "received": False})
            return self._send_json(200, {"received": True, "fact": fact})
        if path.startswith("/api/receipts/"):
            delivery_id = path.rsplit("/", 1)[1]
            return self._send_json(
                200, {"receipts": self.store.list_receipts(delivery_id)}
            )
        if path == "/ui":
            return self._serve_ui()
        self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/chaos":
            data = self._read_json()
            try:
                self.store.set_chaos(data["name"], bool(data["enabled"]))
            except KeyError:
                return self._send_json(422, {"detail": f"未知故障: {data.get('name')}"})
            return self._send_json(200, {"ok": True, **data})
        if path == RECEIVE_PATH:
            return self._handle_receive()
        self._send_json(404, {"detail": "not found"})

    def _handle_receive(self):
        # 故障：模拟报文在网络上丢失/对端无响应，发送端会先超时再查询事实
        if self.store.chaos_on("drop_receive"):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            envelope = self._read_json()
        except json.JSONDecodeError:
            return self._send_json(400, {"detail": "非法 JSON"})

        required = ("delivery_id", "archive_id", "destination", "payload")
        missing = [k for k in required if k not in envelope]
        if missing:
            return self._send_json(422, {"detail": f"缺少字段: {missing}"})

        payload = envelope["payload"]
        # 故障：模拟接收端内部处理时内容被篡改，双方指纹必然不一致
        if self.store.chaos_on("mutate_payload"):
            payload = dict(payload) if isinstance(payload, dict) else {"value": payload}
            payload["_receiver_mutation"] = "内容在接收端被意外改写"

        fp = fingerprint(payload)
        status, fact = self.store.receive(
            envelope["delivery_id"],
            envelope["archive_id"],
            envelope["destination"],
            fp,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        if status == "conflict":
            return self._send_json(409, {
                "detail": "同一投递 ID 内容不符，已隔离并保留首次接收原件",
                "received": True,
                "existing_fingerprint": fact["fingerprint"],
                "incoming_fingerprint": fp,
            })
        http_status = 201 if status == "accepted" else 200
        # 故障：事实已落库、但响应在回程丢失（发送端超时/500），
        # 对账查询应发现事实并补齐，而不是重复接收
        if self.store.chaos_on("lose_receipt") and status == "accepted":
            return self._send_json(500, {
                "detail": "回执在回程丢失（事实已落库，故障注入）",
            })
        return self._send_json(http_status, {
            "received": True,
            "idempotent": status == "duplicate",
            "delivery_id": envelope["delivery_id"],
            "fingerprint": fp,
            "accepted_at": fact["accepted_at"],
            "duplicate_count": fact["duplicate_count"],
        })

    def _serve_ui(self):
        from .receiver_ui import RECEIVER_UI_HTML
        body = RECEIVER_UI_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(host, port, db_path):
    server = ThreadingHTTPServer((host, port), ReceiverHandler)
    server.store = ReceiverStore(db_path)
    return server
