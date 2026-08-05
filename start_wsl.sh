#!/bin/bash
# PC Agent WSL 一键启动（隔离模式）：
#  - llm_server 以 --isolated 运行：禁用屏幕操作工具，只保留文件类工具
#  - 强制 UTF-8（解决 PowerShell GBK 中文输入导致的崩溃）
export PYTHONUTF8=1
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
cd ~
if ! ss -tln 2>/dev/null | grep -q ':8001'; then
  echo "启动 llm_server（隔离模式）..."
  nohup .venv/bin/python llm_server.py --port 8001 --isolated > llm_wsl.log 2>&1 &
  sleep 3
fi
exec .venv/bin/python cli.py --host localhost --port 8001
