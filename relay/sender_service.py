"""发送端 HTTP 服务：投递管理、对账动作、页面与后台调度。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .dispatcher import Dispatcher
from .fingerprint import fingerprint
from .sender_store import (
    CONFLICT,
    IN_FLIGHT,
    PENDING,
    RECEIVED,
    RollbackBlocked,
    SenderStore,
)


class SenderHandler(BaseHTTPRequestHandler):
    server_version = "RelaySender/1.0"

    @property
    def store(self) -> SenderStore:
        return self.server.store

    @property
    def dispatcher(self) -> Dispatcher:
        return self.server.dispatcher

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
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

    # ---------------- GET ----------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send_json(200, {"status": "ok", "role": "sender"})
        if path == "/app":
            return self._serve_app()
        if path == "/api/overview":
            return self._send_json(200, {
                "counts": self.store.counts_by_status(),
                "destinations": self.store.list_destinations(),
                "plans": self.store.list_plans(),
                "chaos": self._chaos_view(),
            })
        if path == "/api/archives":
            return self._send_json(200, {"archives": self.store.list_archives()})
        if path == "/api/deliveries":
            return self._send_json(200, {"deliveries": self.store.list_deliveries()})
        if path.startswith("/api/deliveries/"):
            return self._delivery_detail(path)
        if path == "/api/audits":
            return self._send_json(200, {"audits": self.store.list_audits()})
        self._send_json(404, {"detail": "not found"})

    def _delivery_detail(self, path):
        parts = path.strip("/").split("/")
        delivery_id = parts[2]
        delivery = self.store.get_delivery(delivery_id)
        if not delivery:
            return self._send_json(404, {"detail": "投递不存在"})
        # 同时拉取接收端事实，页面可并排展示双方指纹与差异
        found, fact, error = self.dispatcher.query_fact(delivery)
        return self._send_json(200, {
            "delivery": delivery,
            "attempts": self.store.attempts(delivery_id),
            "remote": {"found": found, "fact": fact, "error": error},
        })

    def _chaos_view(self):
        from .sender_store import SENDER_CHAOS
        from .receiver_store import CHAOS_FLAGS
        return {
            "sender": {n: self.store.chaos_on(n) for n in SENDER_CHAOS},
            "receiver_flags": list(CHAOS_FLAGS),
        }

    # ---------------- POST ----------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/archives":
                return self._create_archive()
            if path == "/api/plans":
                return self._create_plan()
            if path.startswith("/api/plans/") and path.endswith("/pause"):
                return self._set_pause(path, True)
            if path.startswith("/api/plans/") and path.endswith("/resume"):
                return self._set_pause(path, False)
            if path.startswith("/api/plans/") and path.endswith("/destination"):
                return self._change_destination(path)
            if path.startswith("/api/plans/") and path.endswith("/enqueue"):
                return self._enqueue(path)
            if path == "/api/tick":
                delivery = self.dispatcher.tick()
                return self._send_json(200, {
                    "ran": delivery is not None,
                    "delivery": delivery,
                })
            if path.startswith("/api/deliveries/") and path.endswith("/crash-send"):
                return self._crash_send(path)
            if path == "/api/recover":
                ids = self.dispatcher.recover_on_startup()
                return self._send_json(200, {"recovered": ids})
            if path.startswith("/api/deliveries/") and path.endswith("/reconcile"):
                return self._reconcile_one(path)
            if path.startswith("/api/deliveries/") and path.endswith("/late-receipt"):
                return self._late_receipt(path)
            if path == "/api/chaos":
                return self._set_chaos()
            self._send_json(404, {"detail": "not found"})
        except RollbackBlocked as e:
            self._send_json(409, {"detail": f"状态机拒绝回退: {e}"})
        except KeyError as e:
            self._send_json(404, {"detail": str(e)})
        except (ValueError, TypeError) as e:
            self._send_json(422, {"detail": str(e)})

    def _create_archive(self):
        data = self._read_json()
        if "title" not in data or "payload" not in data:
            return self._send_json(422, {"detail": "需要 title 与 payload"})
        arc = self.store.add_archive(data["title"], data["payload"])
        return self._send_json(201, {"archive": arc})

    def _create_plan(self):
        data = self._read_json()
        plan = self.store.create_plan(data["archive_id"], data["dest_code"])
        return self._send_json(201, {"plan": plan})

    def _plan_id(self, path):
        return path.strip("/").split("/")[2]

    def _set_pause(self, path, paused):
        plan_id = self._plan_id(path)
        self.store.set_plan_paused(plan_id, paused)
        return self._send_json(200, {"plan": self.store.get_plan(plan_id)})

    def _change_destination(self, path):
        plan_id = self._plan_id(path)
        data = self._read_json()
        plan = self.store.change_plan_destination(plan_id, data["dest_code"])
        return self._send_json(200, {"plan": plan})

    def _enqueue(self, path):
        plan_id = self._plan_id(path)
        delivery = self.store.enqueue_for_plan(plan_id)
        if delivery is None:
            return self._send_json(409, {"detail": "计划已暂停，停止新投递"})
        return self._send_json(201, {"delivery": delivery})

    def _reconcile_one(self, path):
        delivery_id = path.strip("/").split("/")[2]
        delivery = self.store.get_delivery(delivery_id)
        if not delivery:
            return self._send_json(404, {"detail": "投递不存在"})
        found, fact, error = self.dispatcher.query_fact(delivery)
        if error:
            return self._send_json(502, {"detail": f"接收端查询失败: {error}"})
        if not found:
            return self._send_json(200, {
                "settled": False, "detail": "接收端无事实，保持待确认等待重试",
            })
        from .dispatcher import ConflictError
        try:
            self.dispatcher._compare_and_settle(delivery, fact, kind="reconcile")
        except ConflictError as e:
            return self._send_json(409, {"settled": True, "conflict": True, "detail": str(e)})
        return self._send_json(200, {
            "settled": True, "delivery": self.store.get_delivery(delivery_id),
        })

    def _late_receipt(self, path):
        delivery_id = path.strip("/").split("/")[2]
        data = self._read_json()
        updated = self.dispatcher.accept_late_receipt(
            delivery_id, data["fingerprint"], data.get("accepted_at", time.time())
        )
        return self._send_json(200, {"delivery": updated})

    def _crash_send(self, path):
        """真实地走一遍接收端调用，然后在本地记账前中断（不更新状态）。"""
        delivery_id = path.strip("/").split("/")[2]
        delivery = self.store.get_delivery(delivery_id)
        if not delivery:
            return self._send_json(404, {"detail": "投递不存在"})
        if delivery["status"] not in (PENDING, IN_FLIGHT):
            return self._send_json(409, {
                "detail": f"状态 {delivery['status']} 不允许模拟崩溃发送",
            })
        self.store.claim_for_dispatch()
        result = self.dispatcher.send_and_maybe_crash(delivery)
        if result.get("crashed"):
            return self._send_json(503, {
                "detail": "模拟发送进程中断：接收端已确认，本地未记账（待恢复对账）",
                "crashed": True,
                "delivery_id": delivery_id,
            })
        return self._send_json(200, {"delivery": self.store.get_delivery(delivery_id)})

    def _set_chaos(self):
        data = self._read_json()
        side, name, enabled = data["side"], data["name"], bool(data["enabled"])
        if side == "sender":
            self.store.set_chaos(name, enabled)
        elif side == "receiver":
            # 转发给接收端
            from .http_client import request
            status, body = request(
                "POST", f"{self.server.receiver_base}/api/chaos",
                {"name": name, "enabled": enabled},
            )
            if status != 200:
                return self._send_json(502, body)
        else:
            return self._send_json(422, {"detail": "side 须为 sender/receiver"})
        self.store.audit(f"chaos.{side}", name, f"enabled={enabled}")
        return self._send_json(200, {"ok": True, **data})

    def _serve_app(self):
        from .sender_ui import SENDER_UI_HTML
        body = SENDER_UI_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Scheduler(threading.Thread):
    """后台调度：周期性 tick，恢复在途记录。"""

    def __init__(self, dispatcher, interval=1.0):
        super().__init__(daemon=True, name="relay-scheduler")
        self.dispatcher = dispatcher
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.dispatcher.tick()
            except Exception:
                import logging
                logging.exception("scheduler tick failed")

    def stop(self):
        self._stop.set()


def build_server(host, port, db_path, receiver_base, scheduler_interval=1.0):
    server = ThreadingHTTPServer((host, port), SenderHandler)
    server.store = SenderStore(db_path)
    server.dispatcher = Dispatcher(server.store, receiver_base)
    server.receiver_base = receiver_base.rstrip("/")
    server.scheduler = Scheduler(server.dispatcher, scheduler_interval)
    return server
