#!/bin/bash
# Telegram Bot 前端启动（WSL）—— 代理已配置，llm_server 与 bot 共用
# 注意：每个 nohup 后台进程启动后都要 sleep 保持 wsl 会话，
# 否则未完成初始化的进程会在命令返回时被 WSL 清理。
export PYTHONUTF8=1 LANG=C.UTF-8 LC_ALL=C.UTF-8
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
  sleep 3
  tail -1 telegram_bot.log
else
  echo "Telegram bot 已在运行"
fi
