"""投递调度与对账核心逻辑。

关键保证：
1. 超时不猜结果——先向接收端查询接收事实，再决定重试或记账；
2. 接收端按 delivery_id 幂等，重试绝不产生重复接收；
3. 同一投递双方指纹不符 → CONFLICT 隔离，不再自动重试；
4. 发送端在“接收成功后、本地记账前”崩溃，恢复对账可补齐 RECEIVED；
5. 状态单调，并发重试与晚到回执都不能回退状态。
"""
import json
import time

from .fingerprint import fingerprint
from .http_client import TransportError, request
from .sender_store import (
    CONFLICT,
    IN_FLIGHT,
    PENDING,
    RECEIVED,
    ConflictError,
    RollbackBlocked,
)

BACKOFF = [1, 2, 4]  # 重试退避（秒，演示用短周期）


class Dispatcher:
    def __init__(self, store, base_url, timeout=2.0, sleep=time.sleep):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.sleep = sleep

    # ---------- 接收端事实查询 ----------
    def query_fact(self, delivery):
        """查询接收事实。返回 (found, fact_dict, error)。"""
        try:
            status, body = request(
                "GET",
                f"{self.base_url}/api/facts/{delivery['id']}",
                timeout=self.timeout,
            )
        except TransportError as e:
            return False, None, str(e)
        if status == 200 and body.get("received"):
            return True, body["fact"], None
        if status == 404:
            return False, None, None
        return False, None, f"查询返回 HTTP {status}"

    def _compare_and_settle(self, delivery, fact, kind):
        """根据接收事实对账：指纹一致补齐 RECEIVED，不一致隔离 CONFLICT。"""
        local_fp = delivery["fingerprint"]
        remote_fp = fact["fingerprint"]
        if local_fp != remote_fp:
            detail = (
                f"指纹不符：本地 {local_fp} / 接收端 {remote_fp}；"
                f"接收端原件已保留，投递隔离待人工处理"
            )
            self.store.log_attempt(delivery["id"], kind, "conflict", detail)
            self.store.mark_conflict(delivery["id"], remote_fp, detail)
            self.store.audit("delivery.conflict", delivery["id"], detail)
            raise ConflictError(detail)
        self.store.log_attempt(
            delivery["id"], kind, "recovered",
            f"接收事实存在且指纹一致，补齐记账（accepted_at={fact['accepted_at']}）",
        )
        self.store.mark_received(delivery["id"], remote_fp, fact["accepted_at"])
        self.store.audit("delivery.recovered", delivery["id"], "依据接收事实补齐已接收")

    # ---------- 一次投递 ----------
    def send_once(self, delivery):
        dest = self.store.get_destination(delivery["dest_code"])
        if not dest:
            self.store.log_attempt(
                delivery["id"], "dispatch", "error", "目的地已停用，挂起待人工处理"
            )
            self.store.schedule_retry(delivery["id"], BACKOFF[-1])
            return
        envelope = {
            "delivery_id": delivery["id"],
            "archive_id": delivery["archive_id"],
            "destination": delivery["dest_code"],
            "payload": json.loads(delivery["payload"]),
        }
        try:
            http_status, body = request(
                "POST",
                f"{self.base_url}/api/receive",
                envelope,
                timeout=self.timeout,
            )
        except TransportError as e:
            # 网络层失败：分不清“未收到”还是“收到但回执丢失”——先查事实
            self.store.log_attempt(
                delivery["id"], "dispatch", "timeout", f"网络异常: {e}，转为查询接收事实"
            )
            self._reconcile_after_timeout(delivery)
            return

        if http_status in (200, 201):
            remote_fp = body.get("fingerprint")
            if remote_fp and remote_fp != delivery["fingerprint"]:
                detail = (
                    f"回执指纹不符：本地 {delivery['fingerprint']} / 回执 {remote_fp}"
                )
                self.store.log_attempt(delivery["id"], "dispatch", "conflict", detail)
                self.store.mark_conflict(delivery["id"], remote_fp, detail)
                self.store.audit("delivery.conflict", delivery["id"], detail)
                return
            note = "幂等重复接收（对端已有事实，未重复落库）" if body.get("idempotent") else "接收成功"
            self.store.log_attempt(
                delivery["id"], "dispatch",
                "duplicate" if body.get("idempotent") else "accepted",
                f"{note}，HTTP {http_status}",
                http_status,
            )
            self.store.mark_received(
                delivery["id"], remote_fp, body.get("accepted_at")
            )
            self.store.audit("delivery.received", delivery["id"], note)
            # 故障注入：接收端已确认、本地也已记账后模拟进程崩溃点之前的场景
            # （crash_after_accept 的“未记账”分支在 send_and_maybe_crash 中处理）
            return

        if http_status == 409:
            # 接收端发现同 ID 内容冲突
            detail = body.get("detail", "接收端返回内容冲突")
            self.store.log_attempt(
                delivery["id"], "dispatch", "conflict", detail, http_status
            )
            self.store.mark_conflict(
                delivery["id"],
                body.get("incoming_fingerprint"),
                f"{detail}；已存指纹 {body.get('existing_fingerprint')}",
            )
            self.store.audit("delivery.conflict", delivery["id"], detail)
            return

        # 5xx 等：同样不猜，查事实
        self.store.log_attempt(
            delivery["id"], "dispatch", "error",
            f"HTTP {http_status}：{body.get('detail', '')}，转为查询接收事实",
            http_status,
        )
        self._reconcile_after_timeout(delivery)

    def _reconcile_after_timeout(self, delivery):
        found, fact, error = self.query_fact(delivery)
        if error:
            # 连查询都失败：保持待确认，按退避重试；幂等键保证不会重复接收
            self.store.log_attempt(
                delivery["id"], "query", "error", f"事实查询失败: {error}"
            )
            backoff = BACKOFF[min(delivery["attempt_count"] - 1, len(BACKOFF) - 1)]
            self.store.schedule_retry(delivery["id"], backoff)
            return
        if found:
            # 回执确实丢了：依据事实补齐，绝不重发
            self._compare_and_settle(delivery, fact, kind="query")
            return
        # 对端确认没有收到 → 安全重试（同一 delivery_id，即便对端稍后出现事实也幂等）
        self.store.log_attempt(
            delivery["id"], "query", "error",
            "接收端无事实，判定未收到，安排重试",
        )
        backoff = BACKOFF[min(delivery["attempt_count"] - 1, len(BACKOFF) - 1)]
        self.store.schedule_retry(delivery["id"], backoff)

    # ---------- 崩溃注入：接收成功但本地未记账 ----------
    def send_and_maybe_crash(self, delivery):
        """模拟发送进程在对端接收成功后、写本地状态前被 kill。

        返回 dict 描述结果；crash_after_accept 开启时刻意不记账、不推进状态，
        调用方负责把该结果以 HTTP 503 表达成“进程中断”。
        """
        envelope = {
            "delivery_id": delivery["id"],
            "archive_id": delivery["archive_id"],
            "destination": delivery["dest_code"],
            "payload": json.loads(delivery["payload"]),
        }
        http_status, body = request(
            "POST", f"{self.base_url}/api/receive", envelope, timeout=self.timeout
        )
        if http_status not in (200, 201):
            raise RuntimeError(f"崩溃注入前置条件失败: HTTP {http_status}")
        if self.store.chaos_on("crash_after_accept"):
            # 刻意不记账、不更新状态，模拟进程此刻被杀
            self.store.log_attempt(
                delivery["id"], "dispatch", "error",
                "模拟进程崩溃：对端已接收，本地停留在待确认",
                http_status,
            )
            self.store.audit("delivery.crash", delivery["id"], "接收成功后本地未记账")
            return {"crashed": True, "remote_accepted_at": body.get("accepted_at")}
        self.store.mark_received(
            delivery["id"], body.get("fingerprint"), body.get("accepted_at")
        )
        return {"crashed": False}

    # ---------- 晚到回执 ----------
    def accept_late_receipt(self, delivery_id, remote_fingerprint, accepted_at):
        """处理晚到回执：已接收/冲突等终态不会被回退。

        - 指纹与本地一致：幂等登记，状态保持/推进为 RECEIVED；
        - 指纹不一致：拒绝并保持当前状态（绝不因一条晚到回执翻转终态）。
        """
        delivery = self.store.get_delivery(delivery_id)
        if not delivery:
            raise KeyError(delivery_id)
        if delivery["fingerprint"] != remote_fingerprint:
            self.store.log_attempt(
                delivery_id, "receipt", "rollback_blocked",
                f"晚到回执指纹不符，拒绝记账（保持 {delivery['status']}）："
                f"本地 {delivery['fingerprint']} / 回执 {remote_fingerprint}",
            )
            self.store.audit(
                "receipt.rejected", delivery_id,
                f"晚到回执指纹不符，状态 {delivery['status']} 保持不变",
            )
            raise RollbackBlocked("晚到回执指纹与本地投递不符")
        try:
            updated = self.store.mark_received(
                delivery_id, remote_fingerprint, accepted_at
            )
        except RollbackBlocked:
            self.store.audit(
                "receipt.ignored", delivery_id,
                f"晚到回执被拒绝回退，当前状态 "
                f"{self.store.get_delivery(delivery_id)['status']}",
            )
            raise
        self.store.log_attempt(
            delivery_id, "receipt", "accepted",
            f"晚到回执记账，状态 {updated['status']}",
        )
        return updated

    # ---------- 恢复扫描 ----------
    def recover_on_startup(self):
        """进程重启后：所有待确认记录先查接收事实，能补齐就补齐。"""
        recovered = []
        for delivery in self.store.list_deliveries(status=IN_FLIGHT):
            found, fact, error = self.query_fact(delivery)
            if error:
                self.store.log_attempt(
                    delivery["id"], "reconcile", "error",
                    f"恢复扫描查询失败: {error}，保留待确认",
                )
                continue
            if not found:
                self.store.log_attempt(
                    delivery["id"], "reconcile", "error",
                    "恢复扫描：接收端无事实，等待调度重试",
                )
                continue
            before = delivery["status"]
            try:
                self._compare_and_settle(delivery, fact, kind="reconcile")
            except ConflictError:
                pass
            recovered.append(delivery["id"])
        return recovered

    # ---------- 一个调度周期 ----------
    def tick(self):
        """认领并处理一条到期投递；无任务返回 None。"""
        # 暂停计划不产生新投递（生成在 enqueue 侧拦截），
        # 已在途记录仍可被认领做对账/重试。
        delivery = self.store.claim_for_dispatch()
        if delivery is None:
            return None
        self.send_once(delivery)
        return self.store.get_delivery(delivery["id"])
