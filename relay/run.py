"""本机一键启动两个服务（无第三方依赖）。

用法：
    python -m relay.run                # 发送端 :8088，接收端 :8089
    python -m relay.run --clean        # 清空演示数据库后重启
"""
import argparse
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from .receiver_service import build_server as build_receiver
from .sender_service import build_server as build_sender

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "var" / "relay"


def seed_demo(sender, receiver_base):
    """幂等写入演示目的地与首个归档。"""
    store = sender.store
    store.add_destination(
        "QA-HUB", "质量集团接收枢纽", f"{receiver_base}/api/receive"
    )
    store.add_destination(
        "DR-SITE", "异地灾备接收点", f"{receiver_base}/api/receive"
    )
    if not store.list_archives():
        store.add_archive("周度质量复核简报", {
            "week": "2026-W46",
            "rules": 12,
            "lift_max": 2.71,
            "batches": 240,
            "headline": "热稳定×黏度共败线索待制样组复核",
            "note": "常规归档原件",
        })
    # 建一个默认计划但不自动投递，交给演示脚本/页面操作
    if not store.list_plans():
        arc = store.list_archives()[0]
        store.create_plan(arc["id"], "QA-HUB")


def main(argv=None):
    parser = argparse.ArgumentParser(description="归档简报投递对账演示")
    parser.add_argument("--sender-host", default="127.0.0.1")
    parser.add_argument("--sender-port", type=int, default=8088)
    parser.add_argument("--receiver-host", default="127.0.0.1")
    parser.add_argument("--receiver-port", type=int, default=8089)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--clean", action="store_true", help="删除旧数据库后启动")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="关闭后台自动调度（演示脚本手动 tick 时使用）")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if args.clean and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    receiver_base = f"http://{args.receiver_host}:{args.receiver_port}"
    receiver = build_receiver(
        args.receiver_host, args.receiver_port, str(data_dir / "receiver.db")
    )
    sender = build_sender(
        args.sender_host, args.sender_port, str(data_dir / "sender.db"),
        receiver_base, scheduler_interval=1.0,
    )
    seed_demo(sender, receiver_base)

    import threading
    threading.Thread(target=receiver.serve_forever, daemon=True,
                     name="relay-receiver").start()
    if not args.no_scheduler:
        sender.scheduler.start()

    print("=" * 64)
    print("归档简报投递对账演示已启动")
    print(f"  发送对账台 : http://{args.sender_host}:{args.sender_port}/app")
    print(f"  接收端控制台: http://{args.receiver_host}:{args.receiver_port}/ui")
    print(f"  数据目录   : {data_dir}")
    print("=" * 64)

    stop = {"flag": False}

    def _shutdown(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        while not stop["flag"]:
            time.sleep(0.3)
    finally:
        sender.scheduler.stop()
        sender.shutdown()
        receiver.shutdown()
        print("\n已停止。")


if __name__ == "__main__":
    main(sys.argv[1:])
