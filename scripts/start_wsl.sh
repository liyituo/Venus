#!/bin/bash
# PC Agent WSL 一键启动（隔离模式）——统一入口：
#  - llm_server 以 --isolated 运行（禁屏幕工具，只留文件类），带代理（run_shell 可访问外网）
#  - Telegram bot 若未运行则自动拉起（守护）
#  - 最后进入 cli 终端
export PYTHONUTF8=1
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export HTTP_PROXY=http://127.0.0.1:7897
export HTTPS_PROXY=http://127.0.0.1:7897
export NO_PROXY=localhost,127.0.0.1
cd ~
if ! ss -tln 2>/dev/null | grep -q ':8001'; then
  echo "启动 llm_server（隔离模式，带代理）..."
  nohup .venv/bin/python src/llm_server.py --port 8001 --isolated > llm_wsl.log 2>&1 &
  sleep 3
fi
if ! pgrep -f '[t]elegram_bot' > /dev/null 2>&1; then
  echo "启动 Telegram bot（@QuraxBot）..."
  nohup .venv/bin/python -u src/telegram_bot.py > telegram_bot.log 2>&1 &
  sleep 2
  tail -1 telegram_bot.log
fi
exec .venv/bin/python src/cli.py --host localhost --port 8001
