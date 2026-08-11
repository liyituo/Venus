"""统一跨平台测试入口：自动发现并运行全部 tests/*_test.py。

用法：
    python tests/run_all_tests.py            # 使用当前解释器
    .venv\\Scripts\\python tests/run_all_tests.py
    PYTHONUTF8=1 python tests/run_all_tests.py

逐个以子进程方式运行（避免 import 副作用互相污染），每个脚本要求以
exit code 0 结束；汇总输出 PASS/FAIL 清单并返回非零退出码。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = sorted(ROOT.glob("tests/*_test.py"))
PYTHON = sys.executable
ENV = dict(os.environ)
ENV.setdefault("PYTHONUTF8", "1")


def main() -> int:
    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    start = time.perf_counter()
    for path in TESTS:
        print(f"\n===== {path.name} =====", flush=True)
        try:
            proc = subprocess.run(
                [PYTHON, str(path)],
                cwd=str(ROOT),
                env=ENV,
                timeout=1800,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            failed.append(path.name)
            print(f"  TIMEOUT {path.name}")
            continue
        if proc.returncode == 0:
            passed.append(path.name)
            print(f"  OK ({path.name})")
        else:
            failed.append(path.name)
            print(f"  FAILED ({path.name}) exit={proc.returncode}")
            tail = "\n".join((proc.stdout or "").splitlines()[-15:])
            if tail:
                print(tail)
            err = "\n".join((proc.stderr or "").splitlines()[-10:])
            if err:
                print(err)
    elapsed = time.perf_counter() - start
    print("\n" + "=" * 60)
    print(f"总耗时 {elapsed:.1f}s | 通过 {len(passed)} | 失败 {len(failed)} | 跳过 {len(skipped)}")
    if passed:
        print("通过:", " ".join(passed))
    if failed:
        print("失败:", " ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
