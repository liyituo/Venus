"""把 Windows 侧 src 增量同步到 WSL（base64 分块管道，不依赖 /mnt）。

用法: python tools_sync_wsl.py
覆盖同名文件；保留 WSL 特有文件（amap_server.py 等）。
"""
import base64
import io
import subprocess
import sys
import tarfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
DIST = "Debian"
CHUNK = 14000          # 每块字节数（wsl.exe 命令行长度限制内）


def wsl(cmd: str, timeout: int = 120) -> str:
    r = subprocess.run(["wsl", "-d", DIST, "-e", "bash", "-c", cmd],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(f"  [warn] rc={r.returncode}: {cmd[:60]}... {r.stderr[:120]}")
    return r.stdout


def main() -> None:
    files = sorted(p.name for p in SRC.glob("*.py"))
    print(f"同步 {len(files)} 个文件: {', '.join(files)}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in files:
            tf.add(SRC / name, arcname=f"src/{name}")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    print(f"base64 总长 {len(data)}，分 {len(data) // CHUNK + 1} 块传输…")
    wsl("rm -f /tmp/src.b64")
    for i in range(0, len(data), CHUNK):
        chunk = data[i:i + CHUNK]
        wsl(f'printf %s "{chunk}" >> /tmp/src.b64', timeout=60)
        if i % (CHUNK * 10) == 0:
            print(f"  …{i + len(chunk)}/{len(data)}")
    out = wsl("cd /home/lyt_test && base64 -d /tmp/src.b64 | tar xzf - && rm -f /tmp/src.b64 && "
              "ls src/ | wc -l && ls src/*.py | wc -l && "
              "grep -c '0.7.5' src/llm_server.py")
    print(f"WSL src 文件数: {out.strip()}")
    print("同步完成")


if __name__ == "__main__":
    main()
