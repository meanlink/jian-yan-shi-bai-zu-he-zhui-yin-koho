"""发送端存储：归档、投递计划、投递状态机、尝试与审计。

状态单调推进，rank 只增不减，并发重试与晚到回执都无法回退状态：
    PENDING(待发) < IN_FLIGHT(待确认) < RECEIVED(已接收) < CONFLICT(冲突隔离)
"""
import sqlite3
import threading
import time
import uuid

from .fingerprint import canonical, fingerprint

PENDING = "PENDING"          # 待发
IN_FLIGHT = "IN_FLIGHT"      # 待确认
RECEIVED = "RECEIVED"        # 已接收
CONFLICT = "CONFLICT"        # 冲突隔离

STATUS_RANK = {PENDING: 1, IN_FLIGHT: 2, RECEIVED: 3, CONFLICT: 4}
# 允许的状态转移；任何不在表内的转移一律拒绝（包括“回退”）
ALLOWED = {
    PENDING: {IN_FLIGHT},
    IN_FLIGHT: {RECEIVED, CONFLICT, PENDING},  # 允许重投递回待发，但需经显式重投递
    RECEIVED: {CONFLICT},                       # 对账发现内容不符仍可升级为冲突
    CONFLICT: set(),                            # 终态，隔离后不自动处理
}

SENDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS archives (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    superseded_by TEXT,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS destinations (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL,
    dest_code TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    archive_id TEXT NOT NULL,
    archive_version INTEGER NOT NULL,
    dest_code TEXT NOT NULL,          -- 创建时快照，计划改目的地不挪动在途记录
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    claimed_at REAL,
    next_attempt_at REAL NOT NULL,
    remote_fingerprint TEXT,
    remote_accepted_at REAL,
    conflict_detail TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL,
    kind TEXT NOT NULL,               -- dispatch / query / receipt / reconcile
    outcome TEXT NOT NULL,            -- sent / timeout / error / accepted / duplicate / conflict / recovered / rollback_blocked
    detail TEXT,
    http_status INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chaos (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
"""

SENDER_CHAOS = ("crash_after_accept",)


class ConflictError(RuntimeError):
    """同一投递内容与接收端事实不符。"""


class RollbackBlocked(RuntimeError):
    """晚到回执/并发更新试图把状态向回改，被单调状态机拒绝。"""


class SenderStore:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.conn.executescript(SENDER_SCHEMA)
        for name in SENDER_CHAOS:
            self.conn.execute(
                "INSERT OR IGNORE INTO chaos(name, enabled) VALUES(?, 0)", (name,)
            )
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- 审计与尝试 ----------
    def audit(self, action, entity, detail=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO audits(action, entity, detail, created_at) VALUES(?,?,?,?)",
                (action, entity, detail, time.time()),
            )

    def _log_attempt(self, delivery_id, kind, outcome, detail=None, http_status=None):
        self.conn.execute(
            "INSERT INTO attempts(delivery_id, kind, outcome, detail, http_status, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (delivery_id, kind, outcome, detail, http_status, time.time()),
        )

    def log_attempt(self, delivery_id, kind, outcome, detail=None, http_status=None):
        with self.lock:
            self._log_attempt(delivery_id, kind, outcome, detail, http_status)

    # ---------- 归档（旧版本原件永久保留） ----------
    def add_archive(self, title, payload, archive_id=None):
        with self.lock:
            archive_id = archive_id or f"arc-{uuid.uuid4().hex[:10]}"
            older = self.conn.execute(
                "SELECT id FROM archives WHERE title=? ORDER BY version DESC LIMIT 1",
                (title,),
            ).fetchone()
            version = 1
            if older:
                version = self.conn.execute(
                    "SELECT version FROM archives WHERE id=?", (older[0],)
                ).fetchone()[0] + 1
                self.conn.execute(
                    "UPDATE archives SET superseded_by=? WHERE id=?",
                    (archive_id, older[0]),
                )
            body = canonical(payload)
            self.conn.execute(
                "INSERT INTO archives(id, title, version, superseded_by, fingerprint,"
                " payload, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    archive_id, title, version, None, fingerprint(payload),
                    body, time.time(),
                ),
            )
            self.audit("archive.created", archive_id, f"title={title} v{version}")
            return self.get_archive(archive_id)

    def get_archive(self, archive_id):
        row = self.conn.execute(
            "SELECT * FROM archives WHERE id=?", (archive_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_archives(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM archives ORDER BY title, version DESC"
        ).fetchall()]

    # ---------- 目的地与计划 ----------
    def add_destination(self, code, name, endpoint):
        with self.lock:
            self.conn.execute(
                "INSERT INTO destinations(code, name, endpoint, active) VALUES(?,?,?,1)"
                " ON CONFLICT(code) DO UPDATE SET name=excluded.name, endpoint=excluded.endpoint",
                (code, name, endpoint),
            )
            self.audit("destination.upsert", code, endpoint)

    def get_destination(self, code):
        row = self.conn.execute(
            "SELECT * FROM destinations WHERE code=? AND active=1", (code,)
        ).fetchone()
        return dict(row) if row else None

    def list_destinations(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM destinations ORDER BY code"
        ).fetchall()]

    def create_plan(self, archive_id, dest_code, plan_id=None):
        with self.lock:
            if not self.get_archive(archive_id):
                raise KeyError(f"归档不存在: {archive_id}")
            if not self.get_destination(dest_code):
                raise KeyError(f"目的地不存在或停用: {dest_code}")
            plan_id = plan_id or f"plan-{uuid.uuid4().hex[:10]}"
            self.conn.execute(
                "INSERT INTO plans(id, archive_id, dest_code, paused, created_at)"
                " VALUES(?,?,?,0,?)",
                (plan_id, archive_id, dest_code, time.time()),
            )
            self.audit("plan.created", plan_id, f"archive={archive_id} dest={dest_code}")
            return self.get_plan(plan_id)

    def get_plan(self, plan_id):
        row = self.conn.execute(
            "SELECT * FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_plans(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM plans ORDER BY created_at"
        ).fetchall()]

    def set_plan_paused(self, plan_id, paused):
        """暂停后停止新投递；在途（IN_FLIGHT）记录保留并继续可对账。"""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE plans SET paused=? WHERE id=?",
                (1 if paused else 0, plan_id),
            )
            if cur.rowcount == 0:
                raise KeyError(plan_id)
            self.audit(
                "plan.paused" if paused else "plan.resumed", plan_id, str(paused)
            )

    def change_plan_destination(self, plan_id, new_dest):
        """改目的地只影响之后新生成的投递；在途记录保留原 dest_code 快照。"""
        with self.lock:
            if not self.get_destination(new_dest):
                raise KeyError(f"目的地不存在或停用: {new_dest}")
            old = self.get_plan(plan_id)
            if not old:
                raise KeyError(plan_id)
            self.conn.execute(
                "UPDATE plans SET dest_code=? WHERE id=?", (new_dest, plan_id)
            )
            self.audit(
                "plan.destination_changed", plan_id,
                f"{old['dest_code']} -> {new_dest}（在途记录不迁移）",
            )
            return self.get_plan(plan_id)

    # ---------- 故障注入 ----------
    def set_chaos(self, name, enabled):
        if name not in SENDER_CHAOS:
            raise KeyError(name)
        with self.lock:
            self.conn.execute(
                "UPDATE chaos SET enabled=? WHERE name=?",
                (1 if enabled else 0, name),
            )

    def chaos_on(self, name):
        row = self.conn.execute(
            "SELECT enabled FROM chaos WHERE name=?", (name,)
        ).fetchone()
        return bool(row and row[0])

    # ---------- 投递生成 ----------
    def enqueue_for_plan(self, plan_id, now=None):
        """为计划生成一条待发投递；暂停中的计划不生成。"""
        now = now if now is not None else time.time()
        with self.lock:
            plan = self.get_plan(plan_id)
            if not plan:
                raise KeyError(plan_id)
            if plan["paused"]:
                return None
            arc = self.get_archive(plan["archive_id"])
            delivery_id = f"dlv-{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                "INSERT INTO deliveries(id, plan_id, archive_id, archive_version,"
                " dest_code, fingerprint, payload, status, attempt_count, next_attempt_at,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                (
                    delivery_id, plan_id, arc["id"], arc["version"],
                    plan["dest_code"], arc["fingerprint"], arc["payload"],
                    PENDING, now, now, now,
                ),
            )
            self.audit("delivery.enqueued", delivery_id, f"plan={plan_id}")
            return self.get_delivery(delivery_id)

    def get_delivery(self, delivery_id):
        row = self.conn.execute(
            "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_deliveries(self, status=None, plan_id=None):
        sql = "SELECT * FROM deliveries WHERE 1=1"
        args = []
        if status:
            sql += " AND status=?"; args.append(status)
        if plan_id:
            sql += " AND plan_id=?"; args.append(plan_id)
        sql += " ORDER BY created_at DESC, id"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def attempts(self, delivery_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM attempts WHERE delivery_id=? ORDER BY id", (delivery_id,)
        ).fetchall()]

    # ---------- 单调状态机 ----------
    def transition(self, delivery_id, target, expect=None, **fields):
        """条件更新：仅当当前状态允许且（可选）等于 expect 时才转移。

        依靠单条 UPDATE ... WHERE 原子完成，多线程并发下也只有一个赢家；
        任何向低 rank 的转移都会被拒绝并记 rollback_blocked。
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT status FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            current = row[0]
            if expect is not None and current != expect:
                self._log_attempt(
                    delivery_id, "reconcile", "rollback_blocked",
                    f"期望 {expect}，实际 {current}，拒绝转移到 {target}",
                )
                raise RollbackBlocked(delivery_id)
            if target == current:
                return self.get_delivery(delivery_id)
            if target not in ALLOWED.get(current, set()):
                self._log_attempt(
                    delivery_id, "reconcile", "rollback_blocked",
                    f"{current} 不允许转移到 {target}（状态不可回退）",
                )
                raise RollbackBlocked(delivery_id)
            sets = ["status=?", "updated_at=?"]
            args = [target, time.time()]
            for key, value in fields.items():
                sets.append(f"{key}=?"); args.append(value)
            args.append(delivery_id)
            self.conn.execute(
                f"UPDATE deliveries SET {', '.join(sets)} WHERE id=?", args
            )
            return self.get_delivery(delivery_id)

    def claim_for_dispatch(self, now=None):
        """原子认领一条可投递记录（PENDING，或退避到期的 IN_FLIGHT）。

        并发 worker 只有一个能认领成功，杜绝重复发送：
        - PENDING：到期即可认领；
        - IN_FLIGHT：必须退避到期（next_attempt_at 严格晚于 claimed_at），
          刚认领的记录对其他线程不可见；
        - UPDATE 带 status + attempt_count 双条件，输家 rowcount=0。
        """
        now = now if now is not None else time.time()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM deliveries WHERE"
                    " ((status=? AND next_attempt_at <= ?)"
                    "  OR (status=? AND next_attempt_at <= ?"
                    "      AND (claimed_at IS NULL OR next_attempt_at > claimed_at)))"
                    " ORDER BY created_at LIMIT 1",
                    (PENDING, now, IN_FLIGHT, now),
                ).fetchone()
                if row is None:
                    self.conn.execute("COMMIT")
                    return None
                delivery = dict(row)
                cur = self.conn.execute(
                    "UPDATE deliveries SET status=?, attempt_count=attempt_count+1,"
                    " last_attempt_at=?, claimed_at=?, updated_at=?"
                    " WHERE id=? AND status=? AND attempt_count=?",
                    (IN_FLIGHT, now, now, now, delivery["id"],
                     delivery["status"], delivery["attempt_count"]),
                )
                if cur.rowcount == 0:
                    # 被其他 worker 抢先认领/推进，放弃本轮
                    self.conn.execute("COMMIT")
                    return None
                self._log_attempt(
                    delivery["id"], "dispatch", "claimed",
                    f"第 {delivery['attempt_count'] + 1} 次认领",
                )
                self.conn.execute("COMMIT")
                return self.get_delivery(delivery["id"])
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def schedule_retry(self, delivery_id, backoff_seconds, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            # 保持 IN_FLIGHT（待确认），仅推迟下一次动作；状态不回退
            self.conn.execute(
                "UPDATE deliveries SET next_attempt_at=?, updated_at=? WHERE id=?",
                (now + backoff_seconds, now, delivery_id),
            )

    def mark_conflict(self, delivery_id, remote_fingerprint, detail):
        with self.lock:
            d = self.get_delivery(delivery_id)
            if d["status"] == CONFLICT:
                return d
            return self.transition(
                delivery_id, CONFLICT,
                remote_fingerprint=remote_fingerprint, conflict_detail=detail,
            )

    def mark_received(self, delivery_id, remote_fingerprint, accepted_at, expect=None):
        """登记接收成功。若本地指纹与回执事实不符则升级为冲突而非接收。"""
        with self.lock:
            d = self.get_delivery(delivery_id)
            if d is None:
                raise KeyError(delivery_id)
            if d["status"] == CONFLICT:
                self._log_attempt(
                    delivery_id, "receipt", "rollback_blocked",
                    "冲突隔离记录拒绝被晚到回执改回已接收",
                )
                raise RollbackBlocked(delivery_id)
            if d["fingerprint"] != remote_fingerprint:
                detail = (
                    f"指纹不符：本地 {d['fingerprint']} / 接收端 {remote_fingerprint}"
                )
                self._log_attempt(delivery_id, "reconcile", "conflict", detail)
                return self.mark_conflict(delivery_id, remote_fingerprint, detail)
            return self.transition(
                delivery_id, RECEIVED, expect=expect,
                remote_fingerprint=remote_fingerprint,
                remote_accepted_at=accepted_at,
            )

    def counts_by_status(self):
        result = {s: 0 for s in STATUS_RANK}
        for r in self.conn.execute(
            "SELECT status, COUNT(*) c FROM deliveries GROUP BY status"
        ):
            result[r["status"]] = r["c"]
        return result

    def list_audits(self, limit=100):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
