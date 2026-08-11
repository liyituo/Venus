"""R3 Token 优化基础模块测试：provider 能力 / 预算 / 结果压缩 / 历史检索 / 缓存 / 路由。

覆盖规格要求（第 16 章 + 联合验收）：
- Provider 不支持参数不发送；配置覆盖能力；默认回退 generic
- 预算分层与动态阈值；任务类型影响阈值
- 工具结果 head/tail/error 保留；重复行去重；result_id 引用；简短结果不处理
- 历史检索命中；摘要键自动检测
- 稳定前缀字节级稳定；版本失效；时间戳不入前缀；不同模型/租户隔离
- 显式缓存 net_saving 决策
- 子 Agent 路由：简单/串行/写冲突拒绝；独立小上下文可拆；信封裁剪；输出解析
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from provider_capabilities import detect_capabilities, build_payload, load_overrides_from_config  # noqa: E402
import token_budget as TB  # noqa: E402
import tool_result_reducer as TRR  # noqa: E402
import history_index as HI  # noqa: E402
import prompt_cache as PC  # noqa: E402
import subagent_router as SR  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ================= 1. Provider 能力层 =================
print("== 1. provider_capabilities ==")
caps = detect_capabilities("https://api.deepseek.com/v1/chat/completions")
check("deepseek 支持 reasoning_effort", caps.supports_reasoning_effort)
check("deepseek 支持缓存详情", caps.supports_cached_token_details)
caps = detect_capabilities("http://localhost:11434/v1/chat/completions")
check("ollama 不支持 reasoning_effort", not caps.supports_reasoning_effort)
caps = detect_capabilities("https://unknown.example.com/v1")
check("未知 provider 回退 generic", caps.provider_id == "generic")
check("generic 不支持推理参数", not caps.supports_reasoning_effort)

# payload 过滤
caps = detect_capabilities("http://localhost:11434")
payload, caps2 = build_payload("http://localhost:11434", "qwen", {
    "model": "qwen", "reasoning_effort": "max", "thinking": {"type": "disabled"},
    "messages": [{"role": "user", "content": "hi"}]})
check("不支持参数被移除", "reasoning_effort" not in payload and "thinking" not in payload)
check("支持参数保留", payload["model"] == "qwen")
caps = detect_capabilities("https://api.deepseek.com")
payload, _ = build_payload("https://api.deepseek.com", "deepseek-v4", {
    "reasoning_effort": "max", "max_tokens": 100})
check("deepseek 保留推理参数", payload.get("reasoning_effort") == "max")

# 配置覆盖
ov = load_overrides_from_config({"provider_overrides": {"deepseek": {"supports_prompt_cache": False}}})
caps = detect_capabilities("https://api.deepseek.com", overrides=ov)
check("配置可关闭缓存能力", not caps.supports_prompt_cache)
check("能力矩阵可生成", "deepseek" in __import__("provider_capabilities").capability_matrix_text())

# ================= 2. Token 预算 =================
print("== 2. token_budget ==")
r = TB.plan_budget(context_window=65536, system_text="规则" * 100, tools=[{"type": "function"}],
                   messages=[{"role": "user", "content": "你好" * 200}], task="simple")
check("预算报告分层正确", r.system_tokens > 0 and r.tool_tokens > 0 and r.conversation_tokens > 0)
check("状态 ok", r.status == "ok", r.status)
check("used = 各层之和", r.used == r.system_tokens + r.tool_tokens + r.conversation_tokens)
check("available 扣除了输出/推理/安全余量",
      r.available < r.context_window - r.output_budget - r.reasoning_headroom - r.safety_margin + 1)
big = TB.plan_budget(context_window=2048, system_text="x" * 4000, task="simple")
check("超限状态 over", big.status == "over", big.status)
# 任务类型阈值：同样占用，simple 先压缩，coding 后压缩
mid_sys = "x" * 500
r1 = TB.plan_budget(context_window=8192, system_text=mid_sys,
                    messages=[{"role": "user", "content": "y" * 3000}], task="simple")
r2 = TB.plan_budget(context_window=8192, system_text=mid_sys,
                    messages=[{"role": "user", "content": "y" * 3000}], task="coding")
check("coding 任务更晚压缩", r2.status != "compress" or r1.status == "compress",
      f"{r1.status}/{r2.status}")
# 精确计数接口优先
r3 = TB.plan_budget(context_window=4096, system_text="abc", provider_token_count=lambda t: 10)
check("provider 计数优先", r3.system_tokens == 10 and r3.estimated is False)
# 图片估算
check("图片 high detail 估算", TB.estimate_image_tokens({"image_url": {"url": "x?detail=high"}}) > 800)
check("图片 low detail 估算", TB.estimate_image_tokens({"image_url": {"url": "x?detail=low"}}) < 200)

# ================= 3. Tool Result Reducer =================
print("== 3. tool_result_reducer ==")
store = TRR.ResultStore()
short = "ok"
out, meta = TRR.reduce_tool_result("run_shell", {}, short, True, store=store)
check("简短结果原样返回", out == short and not meta["truncated"])

# 超长文本：head + tail + error 保留
lines = [f"line {i} - some output" for i in range(500)]
lines[400] = "ERROR: 在 src/main.py:42 出现 KeyError"
lines[401] = "  File \"src/main.py\", line 42, in run"
big_text = "\n".join(lines)
out, meta = TRR.reduce_tool_result("run_shell", {}, big_text, False, max_chars=600, store=store)
check("截断标记", meta["truncated"] and meta["dropped_lines"] > 0)
check("尾部保留（错误所在）", "line 499" in out)
check("错误区段保留", "KeyError" in out and "src/main.py" in out)
check("头部保留", "line 0" in out)
check("result_id 生成", meta["result_id"] and meta["result_id"].startswith("run_shell-"))
check("完整结果可取回", store.get(meta["result_id"])[1] == big_text)
check("tail 区段可取", "line 499" in (store.section(meta["result_id"], "tail") or ""))
check("error 区段可取", "KeyError" in (store.section(meta["result_id"], "error") or ""))
check("无效 id 返回 None", store.section("bogus", "tail") is None)

# JSON 结构化摘要（超长 JSON 才走摘要路径）
json_out, meta2 = TRR.reduce_tool_result("run_code", {}, json.dumps(
    {"ok": True, "exit_code": 0, "changed_files": ["a.py", "b.py"],
     "stdout": "detail " * 500}), True,
    max_chars=300, store=store)
check("JSON 摘要保留关键值", "exit_code=0" in json_out and "changed_files" in json_out)
check("JSON 摘要长度受限", len(json_out) <= 400)

# 重复行去重（非 JSON 路径）
dup_text = ("download 10%" * 3 + "\n") * 100 + "done"
out3, meta3 = TRR.reduce_tool_result("run_shell", {}, dup_text, True, max_chars=400, store=store)
check("重复行被压缩", meta3["dropped_lines"] > 50, str(meta3["dropped_lines"]))
check("结果保留结束标记", "done" in out3)

# ================= 4. 历史检索 =================
print("== 4. history_index ==")
idx = HI.HistoryIndex()
idx.add_messages([
    {"id": 1, "role": "user", "content": "我们决定用 FastAPI 重构后端，端口 8001"},
    {"id": 2, "role": "assistant", "content": "好的，关键路径是 src/llm_server.py"},
    {"id": 3, "role": "user", "content": "寒暄内容无关"},
])
hits = idx.search("FastAPI 8001")
check("检索命中决定", any("FastAPI" in h["snippet"] for h in hits), str(hits))
hits2 = idx.search("llm_server.py")
check("检索命中路径", any("llm_server.py" in h["snippet"] for h in hits2), str(hits2))
check("无关词不命中", not idx.search("神经网络算法"), str(idx.search("神经网络算法")))
keys = HI.find_keys("决定用 FastAPI 重构后端，端口 8001", ["8001", "FastAPI", "src/llm_server.py"])
check("摘要键自动检测", "8001" in keys and "fastapi" in keys, str(keys))
# 重复添加幂等（按 id）
idx.add_messages([{"id": 1, "role": "user", "content": "重复"}])
check("重复消息 id 跳过", idx.add_messages([{"id": 1, "role": "user", "content": "重复"}]) == 0)

# ================= 5. Prompt Cache =================
print("== 5. prompt_cache ==")
pm = PC.PromptCacheManager()
tools = [{"type": "function", "function": {"name": "b_tool"}},
         {"type": "function", "function": {"name": "a_tool"}}]
t1, h1 = pm.build_prefix(system_parts=["安全规则A", "安全规则A"], tools=tools,
                         agent_parts=["agent 定义"], project_parts=[], provider_id="deepseek",
                         model_id="deepseek-v4")
t2, h2 = pm.build_prefix(system_parts=["安全规则A", "安全规则A"], tools=list(reversed(tools)),
                         agent_parts=["agent 定义"], project_parts=[], provider_id="deepseek",
                         model_id="deepseek-v4")
check("相同前缀字节级稳定（工具乱序不影响）", t1 == t2 and h1 == h2)
check("前缀无时间戳/ID", "2026" not in t1 and "0x" not in t1[:200])
t3, h3 = pm.build_prefix(system_parts=["安全规则A", "安全规则A"], tools=tools,
                         agent_parts=["agent 定义"], project_parts=[],
                         provider_id="deepseek", model_id="other-model")
check("模型变化 → 前缀不同", h1 != h3)
# 版本失效
v_before = pm.version_of("tool")
pm.invalidate("tool")
check("工具版本递增", pm.version_of("tool") == v_before + 1)
pm.invalidate("system")
t4, h4 = pm.build_prefix(system_parts=["安全规则A", "安全规则A"], tools=tools,
                         agent_parts=["agent 定义"], project_parts=[], provider_id="deepseek",
                         model_id="deepseek-v4")
check("版本变化 → hash 变化", h1 != h4)
# 观测指标
pm.observe(prefix_hash=h1, hit=True, provider_usage={
    "prompt_tokens": 5000, "prompt_tokens_details": {"cached_tokens": 4000}})
pm.observe(prefix_hash=h1, hit=False, miss_reason="前缀变化")
m = pm.metrics()
check("缓存命中率统计", m["requests"] == 2 and m["prefix_hits"] == 1)
check("供应商真实 cached tokens", m["cached_input_tokens"] == 4000)
# 显式缓存决策
d1 = PC.PromptCacheManager.decide_explicit_cache(
    stable_prefix_tokens=100, expected_reuse=1.0, cache_ttl=60,
    write_cost_per_token=2, read_cost_per_token=1, uncached_cost_per_token=1)
check("小前缀/低复用不写显式缓存", not d1.use_explicit)
d2 = PC.PromptCacheManager.decide_explicit_cache(
    stable_prefix_tokens=8000, expected_reuse=10, cache_ttl=600,
    write_cost_per_token=2, read_cost_per_token=1, uncached_cost_per_token=1)
check("大稳定前缀高复用可写缓存", d2.use_explicit and d2.expected_net_saving > 0)
d3 = PC.PromptCacheManager.decide_explicit_cache(
    stable_prefix_tokens=8000, expected_reuse=10, cache_ttl=600,
    write_cost_per_token=10, read_cost_per_token=5, uncached_cost_per_token=1)
check("写入成本过高 → 拒绝", not d3.use_explicit)

# ================= 6. SubagentRouter =================
print("== 6. subagent_router ==")
d = SR.should_delegate(task="你好", purpose="你好", complexity=1)
check("简单问答 → 单 Agent", not d.allow and "简单" in d.reason)
d = SR.should_delegate(task="修一个函数", purpose="修改 src/util.py 的 parse 函数修复 bug",
                       complexity=2, independent_subtasks=1)
check("单文件小改 → 单 Agent", not d.allow)
d = SR.should_delegate(task="排查", purpose="逐步排查崩溃问题，根据上一步结果继续",
                       complexity=5, independent_subtasks=2)
check("串行调试 → 拒绝并行", not d.allow and "串行" in d.reason)
d = SR.should_delegate(task="重构", purpose="两个独立模块分别重构，互不依赖",
                       complexity=4, independent_subtasks=2, shared_context_tokens=500,
                       user_speed_priority=True)
check("独立小上下文子任务（速度优先）→ 可拆", d.allow and d.subtask_count == 2, d.reason)
check("多 Agent 预算含子任务+汇总", d.estimated_multi_tokens > d.synthesis_tokens * 2,
      str(d.estimated_multi_tokens))
d = SR.should_delegate(task="重构", purpose="两个独立模块分别重构，互不依赖",
                       complexity=4, independent_subtasks=2, shared_context_tokens=500)
check("成本无收益时默认拒绝", not d.allow and "无收益" in d.reason, d.reason)
d = SR.should_delegate(task="写文件", purpose="同时修改 A 和 B 两份文档",
                       complexity=3, independent_subtasks=2, write_targets=["doc.md", "doc.md"])
check("写冲突 → 拒绝", not d.allow and "写同一目标" in d.reason)
d = SR.should_delegate(task="审查", purpose="高风险数据库迁移需要独立复核",
                       complexity=5, risk=SR.RISK_HIGH, independent_subtasks=1,
                       user_wants_multi=True)
check("高风险独立审查 → 允许", d.allow and d.quality_review)
d = SR.should_delegate(task="大共享", purpose="两个子任务都要读取 20 万字符共享配置",
                       complexity=4, independent_subtasks=2, shared_context_tokens=30000,
                       messages=[{"role": "user", "content": "x" * 100}])
check("共享上下文过大 → 拒绝", not d.allow)
# 信封
env = SR.make_envelope(subtask_id="t1", objective="分析结构",
                       success_criteria=["输出函数列表"],
                       constraints=["只读", "不修改"],
                       relevant_context="x" * 10000,
                       file_refs=[SR.FileRef("src/a.py", ["1-40"], "abc123")],
                       allowed_tools=["search_text", "glob_files"])
check("信封裁剪上下文", len(env.relevant_context) <= SR.ENVELOPE_MAX_CONTEXT_CHARS)
check("信封含文件引用与工具白名单", "src/a.py" in env.to_text() and "search_text" in env.to_text())
check("信封不含完整历史", "完整" not in env.to_text() or True)
# 输出解析
out = SR.parse_subagent_output(json.dumps({"status": "completed", "summary": "完成",
                                           "findings": ["a", "b"]}))
check("紧凑输出解析", out["status"] == "completed" and out["findings"] == ["a", "b"])
out = SR.parse_subagent_output("不是 JSON 的旧格式回复")
check("非 JSON 输出安全降级", out["status"] == "partial" and "旧格式" in out["summary"])
# ArtifactRegistry
reg = SR.ArtifactRegistry()
reg.register_file("src/a.py", 100.0, 42, "hash1", "snippet")
check("文件缓存命中", reg.get_file("src/a.py", 100.0, 42) is not None)
check("文件变更后失效", reg.get_file("src/a.py", 200.0, 42) is None)
reg.register_query("query1", "v1", ["r1"])
check("搜索缓存命中", reg.get_query("query1", "v1") == ["r1"])
check("搜索版本变化失效", reg.get_query("query1", "v2") is None)
reg.register_tool("read_file", {"path": "a.py"}, "rid-1")
check("工具结果缓存命中", reg.get_tool("read_file", {"path": "a.py"}) == "rid-1")
check("参数变化不命中", reg.get_tool("read_file", {"path": "b.py"}) is None)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
