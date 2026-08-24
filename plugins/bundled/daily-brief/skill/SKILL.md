---
name: daily-brief
description: 每日简报：搜科技新闻 + 查系统状态，汇总成简报
---

# 每日简报

1. 用 `system_status` 查看 CPU/内存/磁盘
2. 若有 `mcp_tavily_tavily_search`，搜索「今日科技新闻」Top 5
3. 否则用 `run_shell` 只读命令获取日期，并提示用户配置 Tavily MCP
4. 输出结构化简报：系统概况 → 新闻要点 → 一句话总结
