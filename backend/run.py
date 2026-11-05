import os
import signal
import socket
import subprocess
import sys
import time

import uvicorn

from app.config import settings


def _port_in_use(host: str, port: int) -> bool:
    """端口上是否已有监听者。

    先查 netstat：对方不 accept 时连接探测会被 backlog 挤掉而误判成空闲。
    """
    if _listener_pid(port) is not None:
        return True
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def _listener_pid(port: int) -> int | None:
    """从 netstat 里找监听该端口的进程 PID；非 Windows 环境返回 None。"""
    if os.name != "nt":
        return None
    try:
        # netstat 是控制台程序，中文 Windows 下输出是 GBK，显式指定编码避免 UTF-8 模式下解码失败
        output = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in output.splitlines():
        columns = line.split()
        if len(columns) >= 5 and columns[0].upper() == "TCP" and columns[3].upper() == "LISTENING":
            if columns[1].rsplit(":", 1)[-1] == str(port):
                return int(columns[4])
    return None


def _ensure_port_available(host: str, port: int) -> None:
    """端口被占时给一句能照做的提示；带 --kill 则先结束旧实例再继续启动。"""
    if not _port_in_use(host, port):
        return
    pid = _listener_pid(port)
    if "--kill" in sys.argv and pid is not None:
        print(f"端口 {port} 被 PID {pid} 占用，正在结束旧实例（--kill）...")
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _port_in_use(host, port):
                return
            time.sleep(0.25)
        print(f"PID {pid} 结束后端口 {port} 仍被占用，请手动检查。")
        sys.exit(1)
    owner = f"PID {pid}" if pid is not None else "其他进程"
    print(
        f"端口 {port} 已被{owner}占用，本次启动取消。\n"
        f"  结束旧实例并重启：python run.py --kill\n"
        f"  或换端口：改项目根目录 .env 里的 APP_PORT，前后端都读这个值"
    )
    sys.exit(1)


if __name__ == "__main__":
    _ensure_port_available(settings.app_host, settings.app_port)
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )
