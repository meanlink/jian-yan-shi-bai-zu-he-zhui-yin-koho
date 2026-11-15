"""端到端可重放演示：覆盖需求中的全部可靠性场景。

    python -m relay.demo            # 跑完场景后保留服务，可打开页面继续操作
    python -m relay.demo --once     # 跑完即退出（CI/验收用）

每个场景都带断言；场景之间通过故障开关与真实 HTTP 交互驱动，
不直接改数据库状态。
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

from .http_client import TransportError, request
from .receiver_service import build_server as build_receiver
from .sender_service import build_server as build_sender

PASS, FAIL = "✅", "❌"
failures = []


def check(name, cond, detail=""):
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def section(title):
    print("\n" + "─" * 70)
    print(f"▌ {title}")
    print("─" * 70)


class Api:
    def __init__(self, base):
        self.base = base

    def get(self, path):
        status, body = request("GET", self.base + path, timeout=5)
        return status, body

    def post(self, path, body=None):
        status, resp = request("POST", self.base + path, body or {}, timeout=5)
        if status >= 500:
            raise RuntimeError(f"{path} -> {status} {resp}")
        return status, resp


def wait_port(api, name):
    for _ in range(50):
        try:
            api.get("/health")
            return
        except TransportError:
            time.sleep(0.1)
    raise RuntimeError(f"{name} 未就绪")


def reset_chaos(sender: Api):
    for side, flags in (
        ("receiver", ["drop_receive", "lose_receipt", "drop_facts", "mutate_payload"]),
        ("sender", ["crash_after_accept"]),
    ):
        for flag in flags:
            sender.post("/api/chaos", {"side": side, "name": flag, "enabled": False})


def get_plan(sender: Api):
    _, ov = sender.get("/api/overview")
    return ov["plans"][0]["id"]


def new_delivery(sender: Api, plan_id=None):
    plan_id = plan_id or get_plan(sender)
    _, body = sender.post(f"/api/plans/{plan_id}/enqueue")
    return body["delivery"]["id"]


def delivery_detail(sender: Api, delivery_id):
    _, body = sender.get(f"/api/deliveries/{delivery_id}")
    return body


def run_scenarios(sender: Api, receiver: Api):
    # 场景 1：正常投递 + 幂等重试不重复接收 -------------------------------
    section("场景1 正常投递：待发 → 待确认 → 已接收，重试不重复接收")
    d1 = new_delivery(sender)
    check("生成后为待发 PENDING", delivery_detail(sender, d1)["delivery"]["status"] == "PENDING")
    _, tick1 = sender.post("/api/tick")
    check("调度后已接收 RECEIVED", tick1["delivery"]["status"] == "RECEIVED")
    _, fact = receiver.get(f"/api/facts/{d1}")
    check("接收端存在事实", fact["received"] is True)
    # 再手工触发对账/重复投递：对端应返回幂等而非二次落库
    _, again = sender.post("/api/tick")  # 无到期任务
    check("无到期任务时空转", again["ran"] is False)
    # 直接向接收端重发同一信封，验证接收端自身幂等
    detail = delivery_detail(sender, d1)
    envelope = {
        "delivery_id": d1,
        "archive_id": detail["delivery"]["archive_id"],
        "destination": detail["delivery"]["dest_code"],
        "payload": json.loads(detail["delivery"]["payload"]),
    }
    status, dup = request("POST", receiver.base + "/api/receive", envelope, timeout=5)
    check("重复提交返回幂等标记", status == 200 and dup["idempotent"] is True)
    _, fact2 = receiver.get(f"/api/facts/{d1}")
    check("接收事实仍只有一条、重复计数+1", fact2["fact"]["duplicate_count"] == 1)
    check("双方指纹一致",
          fact2["fact"]["fingerprint"] == detail["delivery"]["fingerprint"])

    # 场景 2：回执丢失（事实已在接收端）——先查事实再补齐，不重发 -------------
    section("场景2 回执回程丢失：超时不猜结果，查接收事实后补齐，不重复接收")
    sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": True})
    d2 = new_delivery(sender)
    _, tick2 = sender.post("/api/tick")
    state2 = tick2["delivery"]["status"]
    # tick 内部已按“先查事实”自动对账；lose_receipt 下事实存在 → 直接补齐
    check("回执丢失后经事实查询自动判定（RECEIVED 或短暂 IN_FLIGHT）",
          state2 in ("IN_FLIGHT", "RECEIVED"), f"实际 {state2}")
    _, rf = receiver.get(f"/api/facts/{d2}")
    check("接收端事实其实已存在", rf["received"] is True)
    if state2 == "IN_FLIGHT":
        # 关闭故障后立即对账（模拟超时后的事实查询）
        sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": False})
        status, rec = sender.post(f"/api/deliveries/{d2}/reconcile")
        check("依据事实补齐为 RECEIVED", status == 200 and rec["settled"] is True)
    else:
        sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": False})
        check("tick 已依据事实补齐为 RECEIVED",
              delivery_detail(sender, d2)["delivery"]["status"] == "RECEIVED")
    _, rf2 = receiver.get(f"/api/facts/{d2}")
    check("未产生重复接收（计数仍 0）", rf2["fact"]["duplicate_count"] == 0)

    # 场景 3：对端确实没收到——查询无事实后安全重试 --------------------------
    section("场景3 报文丢失且对端无事实：查询确认后重试，最终成功且只接收一次")
    sender.post("/api/chaos", {"side": "receiver", "name": "drop_receive", "enabled": True})
    d3 = new_delivery(sender)
    _, tick3 = sender.post("/api/tick")
    check("投递被丢弃后保持待确认", tick3["delivery"]["status"] == "IN_FLIGHT")
    _, nofact = receiver.get(f"/api/facts/{d3}")
    check("接收端确认无事实", nofact["received"] is False)
    sender.post("/api/chaos", {"side": "receiver", "name": "drop_receive", "enabled": False})
    # 到重试时间后再次 tick（手动把退避窗口走完）
    time.sleep(1.2)
    _, tick3b = sender.post("/api/tick")
    check("重试后成功 RECEIVED", tick3b["delivery"]["status"] == "RECEIVED")
    _, rf3 = receiver.get(f"/api/facts/{d3}")
    check("接收端仅一条事实", rf3["fact"]["duplicate_count"] == 0)
    check("尝试记录包含 query 环节",
          any(a["kind"] == "query" for a in delivery_detail(sender, d3)["attempts"]))

    # 场景 4：事实查询也失败——退避保持待确认，恢复后仍可对账 -----------------
    section("场景4 事实接口故障：保持待确认并退避，故障消除后对账补齐")
    sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": True})
    sender.post("/api/chaos", {"side": "receiver", "name": "drop_facts", "enabled": True})
    d4 = new_delivery(sender)
    _, tick4 = sender.post("/api/tick")
    check("投递与查询双故障下保持 IN_FLIGHT", tick4["delivery"]["status"] == "IN_FLIGHT")
    sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": False})
    sender.post("/api/chaos", {"side": "receiver", "name": "drop_facts", "enabled": False})
    status, rec4 = sender.post(f"/api/deliveries/{d4}/reconcile")
    check("故障消除后对账补齐 RECEIVED", status == 200 and rec4["settled"] is True)

    # 场景 5：内容不符——同一投递指纹冲突，隔离且保留双方原件 ------------------
    section("场景5 同一投递内容不符：指纹冲突 → CONFLICT 隔离，不再自动重试")
    sender.post("/api/chaos", {"side": "receiver", "name": "mutate_payload", "enabled": True})
    d5 = new_delivery(sender)
    _, tick5 = sender.post("/api/tick")
    check("投递被标记 CONFLICT", tick5["delivery"]["status"] == "CONFLICT")
    detail5 = delivery_detail(sender, d5)
    check("记录双方指纹与冲突说明",
          detail5["delivery"]["remote_fingerprint"] is not None
          and detail5["delivery"]["fingerprint"] != detail5["delivery"]["remote_fingerprint"])
    check("内容差异可在详情查看",
          detail5["remote"]["found"] and detail5["remote"]["fact"]["fingerprint"]
          == detail5["delivery"]["remote_fingerprint"])
    # 关闭故障后再 tick：冲突是终态，不会被重新认领
    sender.post("/api/chaos", {"side": "receiver", "name": "mutate_payload", "enabled": False})
    time.sleep(1.2)
    _, tick5b = sender.post("/api/tick")
    check("冲突记录不参与自动重试", tick5b["ran"] is False
          or delivery_detail(sender, d5)["delivery"]["status"] == "CONFLICT")
    # 正确指纹的晚到回执也不能把冲突改回已接收
    status, _ = request("POST", sender.base + f"/api/deliveries/{d5}/late-receipt",
                        {"fingerprint": detail5["delivery"]["fingerprint"],
                         "accepted_at": time.time()}, timeout=5)
    check("晚到回执无法回退冲突态（409）", status == 409)
    check("冲突态保持不变", delivery_detail(sender, d5)["delivery"]["status"] == "CONFLICT")

    # 场景 6：发送中断恢复——接收成功但本地未记账，重启后补齐 ------------------
    section("场景6 发送进程中断：对端已接收、本地待确认，恢复扫描补齐")
    sender.post("/api/chaos", {"side": "sender", "name": "crash_after_accept", "enabled": True})
    d6 = new_delivery(sender)
    # 崩溃端点：对端真实落库，但本地状态不推进，HTTP 503 表达进程中断
    status6, body6 = request(
        "POST", sender.base + f"/api/deliveries/{d6}/crash-send", timeout=5,
    )
    check("发送中断以 503 表达且标记 crashed",
          status6 == 503 and body6.get("crashed") is True)
    check("本地记录停留 IN_FLIGHT（未记账）",
          delivery_detail(sender, d6)["delivery"]["status"] == "IN_FLIGHT")
    _, rf6 = receiver.get(f"/api/facts/{d6}")
    check("接收端事实已存在", rf6["received"] is True)
    sender.post("/api/chaos", {"side": "sender", "name": "crash_after_accept", "enabled": False})
    # 模拟进程重启：调用恢复扫描
    _, recovered = sender.post("/api/recover")
    check("恢复扫描补齐该投递", d6 in recovered["recovered"])
    check("补齐后为 RECEIVED",
          delivery_detail(sender, d6)["delivery"]["status"] == "RECEIVED")

    # 场景 7：并发重试不重复发送、晚到回执不回退 ------------------------------
    section("场景7 并发与晚到回执：原子认领只有一个赢家，状态单调不回退")
    d7 = new_delivery(sender)
    results = []

    def concurrent_tick():
        try:
            _, body = sender.post("/api/tick")
            results.append(body)
        except RuntimeError as e:
            results.append({"error": str(e)})

    threads = [threading.Thread(target=concurrent_tick) for _ in range(4)]
    for th in threads: th.start()
    for th in threads: th.join(timeout=8)
    ran = [r for r in results if r.get("ran")]
    check("4 个并发调度只有 1 个认领成功", len(ran) == 1, f"ran={len(ran)}")
    check("投递最终 RECEIVED",
          delivery_detail(sender, d7)["delivery"]["status"] == "RECEIVED")
    _, rf7 = receiver.get(f"/api/facts/{d7}")
    check("接收端仅一条事实（无重复接收）", rf7["fact"]["duplicate_count"] == 0)
    # 晚到的错误回执：拒绝且 RECEIVED 状态保持不变
    status, _ = request("POST", sender.base + f"/api/deliveries/{d7}/late-receipt",
                        {"fingerprint": "0" * 64, "accepted_at": time.time()}, timeout=5)
    check("错误指纹晚到回执被拒绝（409）且状态不变",
          status == 409
          and delivery_detail(sender, d7)["delivery"]["status"] == "RECEIVED")
    # 正确指纹晚到回执：状态已 RECEIVED，幂等保持不回退
    status, late = request("POST", sender.base + f"/api/deliveries/{d7}/late-receipt",
                           {"fingerprint": delivery_detail(sender, d7)["delivery"]["fingerprint"],
                            "accepted_at": time.time()}, timeout=5)
    check("正确晚到回执幂等保持 RECEIVED",
          status == 200 and late["delivery"]["status"] == "RECEIVED")

    # 场景 8：暂停停止新投递、在途仍可对账；改目的地不挪在途 -------------------
    section("场景8 计划暂停与改目的地：停止新投递，在途记录保留原目的地")
    _, ov = sender.get("/api/overview")
    plan_id = ov["plans"][0]["id"]
    # 先在运行态造出一条投递，再暂停，验证“在途仍可对账、新投递被拦下”
    sender.post(f"/api/plans/{plan_id}/resume")
    d8 = new_delivery(sender, plan_id)
    sender.post(f"/api/plans/{plan_id}/pause")
    status, blocked = sender.post(f"/api/plans/{plan_id}/enqueue")
    check("暂停后新投递被拒绝（409）", status == 409 and "暂停" in blocked["detail"])
    # 在途记录：制造回执丢失，暂停期间仍可调度对账
    sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": True})
    _, tick8 = sender.post("/api/tick")
    check("暂停期间在途记录仍可处理（IN_FLIGHT 或已对账 RECEIVED）",
          tick8["delivery"]["status"] in ("IN_FLIGHT", "RECEIVED"))
    sender.post("/api/chaos", {"side": "receiver", "name": "lose_receipt", "enabled": False})
    status, rec8 = sender.post(f"/api/deliveries/{d8}/reconcile")
    check("在途记录对账补齐 RECEIVED", status == 200 and rec8["settled"] is True)
    # 改目的地：在途/历史记录保持原快照
    sender.post(f"/api/plans/{plan_id}/destination", {"dest_code": "DR-SITE"})
    plan_now = next(p for p in sender.get("/api/overview")[1]["plans"] if p["id"] == plan_id)
    check("计划当前目的地已改 DR-SITE", plan_now["dest_code"] == "DR-SITE")
    check("历史投递仍保留原目的地快照 QA-HUB",
          delivery_detail(sender, d8)["delivery"]["dest_code"] == "QA-HUB")
    sender.post(f"/api/plans/{plan_id}/resume")
    d9 = new_delivery(sender, plan_id)
    check("改目的地后新投递走新目的地",
          delivery_detail(sender, d9)["delivery"]["dest_code"] == "DR-SITE")
    _, tick9 = sender.post("/api/tick")
    check("新目的地投递成功", tick9["delivery"]["status"] == "RECEIVED")

    # 场景 9：旧归档保留原件，新版本另存，指纹各自独立 -------------------------
    section("场景9 归档版本化：旧归档原件保留，新简报产生新指纹")
    _, before = sender.get("/api/archives")
    old = next(a for a in before["archives"] if a["title"] == "周度质量复核简报"
               and a["version"] == 1)
    sender.post("/api/archives", {
        "title": "周度质量复核简报",
        "payload": {"week": "2026-W47", "rules": 15, "lift_max": 3.08,
                    "batches": 240, "headline": "新增色差×灰分线索", "note": "第二版"},
    })
    _, after = sender.get("/api/archives")
    versions = [a for a in after["archives"] if a["title"] == "周度质量复核简报"]
    check("存在两个版本且旧版原件保留", len(versions) == 2)
    v1 = next(a for a in versions if a["version"] == 1)
    v2 = next(a for a in versions if a["version"] == 2)
    check("旧版指纹未被覆盖", v1["fingerprint"] == old["fingerprint"])
    check("新旧版本指纹不同", v1["fingerprint"] != v2["fingerprint"])
    check("旧版标记被新版替代", v1["superseded_by"] == v2["id"])
    check("基于旧归档的历史投递仍引用旧指纹",
          delivery_detail(sender, d1)["delivery"]["fingerprint"] == old["fingerprint"])

    # 总览计数核对
    _, ov2 = sender.get("/api/overview")
    section("对账总览（按归档与目的地跟踪四种状态）")
    print("  状态计数:", json.dumps(ov2["counts"], ensure_ascii=False))
    counts = ov2["counts"]
    check("存在已接收记录", counts["RECEIVED"] >= 6)
    check("冲突隔离恰好 1 条", counts["CONFLICT"] == 1)
    check("无遗漏的待发记录", counts["PENDING"] == 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description="归档投递对账端到端演示")
    parser.add_argument("--once", action="store_true", help="跑完场景即退出")
    parser.add_argument("--clean", action="store_true", help="使用独立临时数据目录")
    parser.add_argument("--sender-port", type=int, default=8088)
    parser.add_argument("--receiver-port", type=int, default=8089)
    args = parser.parse_args(argv)

    data_dir = Path(__file__).resolve().parent.parent / "var" / "relay-demo"
    if args.clean and data_dir.exists():
        import shutil
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    receiver = build_receiver("127.0.0.1", args.receiver_port,
                              str(data_dir / "receiver.db"))
    receiver_base = f"http://127.0.0.1:{args.receiver_port}"
    sender = build_sender("127.0.0.1", args.sender_port,
                          str(data_dir / "sender.db"), receiver_base)
    # 演示以手动 tick 驱动，保证时序确定
    sender.scheduler.stop()

    from .run import seed_demo
    seed_demo(sender, receiver_base)

    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    threading.Thread(target=sender.serve_forever, daemon=True).start()

    sender_api = Api(f"http://127.0.0.1:{args.sender_port}")
    receiver_api = Api(receiver_base)
    wait_port(sender_api, "发送端")
    wait_port(receiver_api, "接收端")
    reset_chaos(sender_api)

    print("╔" + "═" * 68 + "╗")
    print("║ 归档简报投递对账 · 端到端可靠性演示（标准库 + SQLite，本机可跑）".ljust(78) + "║")
    print("╚" + "═" * 68 + "╝")
    run_scenarios(sender_api, receiver_api)

    print("\n" + "=" * 70)
    if failures:
        print(f"{FAIL} 演示未通过，失败断言 {len(failures)} 条：")
        for f in failures:
            print("   -", f)
        rc = 1
    else:
        print(f"{PASS} 全部断言通过：超时查事实、重试不重收、冲突隔离、崩溃补齐、")
        print("   并发不重发、晚到回执不回退、暂停/改目的地、旧归档保留均已验证。")
        rc = 0
    print("=" * 70)

    if args.once:
        sender.shutdown(); receiver.shutdown()
        return rc

    print(f"\n服务保持运行，浏览器打开：")
    print(f"  发送对账台 : http://127.0.0.1:{args.sender_port}/app")
    print(f"  接收端控制台: http://127.0.0.1:{args.receiver_port}/ui")
    print("按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sender.shutdown(); receiver.shutdown()
        print("\n已退出。")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
