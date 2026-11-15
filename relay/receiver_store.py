"""接收端存储：以投递幂等键为唯一事实，重复提交不重复接收。"""
import sqlite3
import time


RECEIVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    delivery_id TEXT PRIMARY KEY,
    archive_id    TEXT NOT NULL,
    destination   TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    content       TEXT NOT NULL,
    accepted_at   REAL NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL,
    issued_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chaos (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);
"""

# 接收端可注入的故障，仅用于本机演示
CHAOS_FLAGS = ("drop_receive", "lose_receipt", "drop_facts", "mutate_payload")


class ReceiverStore:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(RECEIVER_SCHEMA)
        self.conn.commit()
        for name in CHAOS_FLAGS:
            self.conn.execute(
                "INSERT OR IGNORE INTO chaos(name, enabled) VALUES(?, 0)", (name,)
            )
        self.conn.commit()

    # ---- 故障注入 ----
    def set_chaos(self, name: str, enabled: bool):
        if name not in CHAOS_FLAGS:
            raise KeyError(name)
        self.conn.execute(
            "UPDATE chaos SET enabled=? WHERE name=?", (1 if enabled else 0, name)
        )
        self.conn.commit()

    def chaos_on(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT enabled FROM chaos WHERE name=?", (name,)
        ).fetchone()
        return bool(row and row[0])

    # ---- 接收事实 ----
    def receive(self, delivery_id, archive_id, destination, fingerprint, content):
        """返回 (状态, 事实行)。

        幂等：相同 delivery_id 已存在时绝不重复落库；
        指纹不同属于同一投递内容冲突，隔离报错且不覆盖原事实。
        """
        existing = self.conn.execute(
            "SELECT * FROM facts WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        now = time.time()
        if existing is not None:
            if existing["fingerprint"] == fingerprint:
                self.conn.execute(
                    "UPDATE facts SET duplicate_count=duplicate_count+1 WHERE delivery_id=?",
                    (delivery_id,),
                )
                self.conn.execute(
                    "INSERT INTO receipts(delivery_id, issued_at) VALUES(?, ?)",
                    (delivery_id, now),
                )
                self.conn.commit()
                refreshed = self.conn.execute(
                    "SELECT * FROM facts WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
                return "duplicate", dict(refreshed)
            return "conflict", dict(existing)

        self.conn.execute(
            "INSERT INTO facts(delivery_id, archive_id, destination, fingerprint,"
            " content, accepted_at, duplicate_count) VALUES(?,?,?,?,?,?,0)",
            (delivery_id, archive_id, destination, fingerprint, content, now),
        )
        self.conn.execute(
            "INSERT INTO receipts(delivery_id, issued_at) VALUES(?, ?)",
            (delivery_id, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM facts WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        return "accepted", dict(row)

    def fact(self, delivery_id):
        row = self.conn.execute(
            "SELECT * FROM facts WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_facts(self):
        rows = self.conn.execute(
            "SELECT * FROM facts ORDER BY accepted_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_receipts(self, delivery_id):
        rows = self.conn.execute(
            "SELECT id, issued_at FROM receipts WHERE delivery_id=? ORDER BY id",
            (delivery_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
