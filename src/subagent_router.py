"""子 Agent 智能路由层（SubagentRouter）。

原则（质量优先）：
- 默认由主 Agent 单独完成任务；只有满足明确的拆分收益条件才允许创建子 Agent。
- 禁止为展示能力而滥用子 Agent；禁止把完整历史/全部文件发给子 Agent。
- 所有子 Agent 创建请求必须经过路由器（业务代码禁止直接创建）。

本模块提供：
1. should_delegate：路由决策（成本模型 + 明确条件 + 禁止场景）
2. SubtaskEnvelope：裁剪后的子任务上下文信封
3. parse_subagent_output：紧凑输出协议解析
4. ArtifactRegistry：跨子 Agent 防重复工作的工件注册表
5. 决策记录（观测），不把完整决策过程发送给用户
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

# ---- 默认限制（规格 §5）----
MAX_PARALLEL_SUBAGENTS = 2      # 默认最大并行子 Agent
MAX_COMPLEX_SUBAGENTS = 3       # 普通复杂任务上限
MAX_NESTING_DEPTH = 1           # 嵌套深度：禁止子 Agent 再创建子 Agent
ENVELOPE_MAX_CONTEXT_CHARS = 4000    # 信封 relevant_context 字符上限
ENVELOPE_MAX_FILE_REFS = 6           # 文件引用上限（按片段传递，不传全文）
SUBAGENT_OUTPUT_MAX_CHARS = 3000     # 子 Agent 输出协议上限
SUBAGENT_BUDGET_TOKENS = 12_000      # 子 Agent 默认预算（输入+输出+推理）
SUBAGENT_RETRY_ONCE = True           # 同一失败原因最多重试一次

# 风险等级
RISK_LOW, RISK_MEDIUM, RISK_HIGH = "low", "medium", "high"

# 禁止场景关键词（任务描述命中 → 拒绝委派）
# "依赖" 用负向匹配：互不依赖 / 不依赖 / 无依赖 不算串行
_SERIAL_PATTERNS = (
    re.compile(r"调试|排查|trace|debug|接着|继续|然后|一步步|sequential|follow-up", re.IGNORECASE),
    re.compile(r"(?<!不)(?<!无)(?<!互)依赖"),
)


@dataclass
class RouterDecision:
    """路由决策结果（记录用，不发送完整决策给用户）。"""

    allow: bool
    reason: str                       # 决策原因（一句话）
    subtask_count: int = 0
    parallelism_gain: bool = False
    quality_review: bool = False      # 高风险独立审查模式
    estimated_single_tokens: int = 0
    estimated_multi_tokens: int = 0
    shared_context_tokens: int = 0
    synthesis_tokens: int = 0
    model: str = ""

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in (
            "allow", "reason", "subtask_count", "parallelism_gain", "quality_review",
            "estimated_single_tokens", "estimated_multi_tokens", "shared_context_tokens",
            "synthesis_tokens", "model")}


def _estimate_context_tokens(messages: list[dict] | None) -> int:
    """粗略估算对话上下文 token（路由成本模型用，不精确）。"""
    if not messages:
        return 0
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return int(chars * 0.5) + 20 * len(messages)


def _estimate_single_agent_tokens(context_tokens: int, tool_schema_tokens: int,
                                  system_tokens: int) -> int:
    return system_tokens + tool_schema_tokens + context_tokens + 1500


def _estimate_multi_agent_tokens(context_tokens: int, subtasks: int,
                                 shared_context_tokens: int,
                                 system_tokens: int, tool_schema_tokens: int,
                                 synthesis_tokens: int) -> int:
    """多 Agent 总 Token：每个子 Agent 的系统提示 + 裁剪后的工具子集 +
    局部上下文 + 输入输出 + 推理 + 主 Agent 汇总 + 重试成本。不能只比较输出。

    子 Agent 只携带任务必要工具（allowed_tools 过滤后 schema 约为全量 30%），
    不复制完整工具定义（规格条件 B）。
    """
    sub_tool_schema = max(300, int(tool_schema_tokens * 0.3))
    per_sub = (system_tokens + sub_tool_schema
               + shared_context_tokens // max(1, subtasks)   # 局部上下文（分摊）
               + 800)                                        # 输出+推理
    retry = int(per_sub * 0.15) * subtasks                    # 重试/失败恢复
    return subtasks * (per_sub + retry) + synthesis_tokens + context_tokens


def should_delegate(*, task: str, purpose: str = "", model: str = "",
                    messages: list[dict] | None = None,
                    system_tokens: int = 800, tool_schema_tokens: int = 4000,
                    complexity: int = 3, risk: str = RISK_LOW,
                    independent_subtasks: int = 0,
                    shared_context_tokens: int = 0,
                    write_targets: list | None = None,
                    user_wants_multi: bool = False,
                    user_speed_priority: bool = False,
                    agent_def: dict | None = None,
                    readonly_subagent: bool = False) -> RouterDecision:
    """路由决策：默认单 Agent；满足拆分收益条件才允许委派。"""
    write_targets = write_targets or []
    context_tokens = _estimate_context_tokens(messages)
    purpose = (purpose or task or "").strip()

    # ---- 禁止场景（硬性拒绝）----
    if not purpose:
        return RouterDecision(False, "无任务描述，不委派")
    if complexity <= 1:
        return RouterDecision(False, "简单任务由主 Agent 直接完成")
    if independent_subtasks <= 0:
        return RouterDecision(False, "无独立子任务，不拆")
    if any(p.search(purpose.lower()) for p in _SERIAL_PATTERNS):
        return RouterDecision(False, "任务必须严格串行，委派无收益")
    # 只读专业子 agent（视觉分析/专项审查等，工具全只读）：独立上下文执行，
    # 主上下文不膨胀（规格条件 B：子任务只需要很小的局部上下文）
    if readonly_subagent and independent_subtasks >= 1:
        single = _estimate_single_agent_tokens(context_tokens, tool_schema_tokens, system_tokens)
        return RouterDecision(
            True, "只读专业子 agent：独立上下文，主上下文不膨胀", subtask_count=1,
            quality_review=True, estimated_single_tokens=single,
            estimated_multi_tokens=single + 600, shared_context_tokens=0,
            synthesis_tokens=600, model=model)
    if independent_subtasks == 1 and not user_wants_multi:
        return RouterDecision(False, "只有一个子任务，委派成本高于收益")
    # 共享上下文过大：子 Agent 只能重复接收同一批内容
    if shared_context_tokens > context_tokens * 0.7 and shared_context_tokens > 6000:
        return RouterDecision(False, "子任务依赖大量共享上下文，委派需重复复制")
    # 写冲突：多个 Agent 可能同时修改相同目标
    if len(write_targets) > 1 and len(set(write_targets)) < len(write_targets):
        return RouterDecision(False, "多个子任务可能写同一目标，禁止并行")
    if write_targets and len(write_targets) >= 3:
        return RouterDecision(False, "写目标过多，冲突风险高，主 Agent 串行处理")

    # ---- 允许条件 ----
    quality_review = (risk == RISK_HIGH and user_wants_multi) or (risk == RISK_HIGH and agent_def is not None)
    if user_wants_multi and (independent_subtasks >= 2 or quality_review):
        n = min(independent_subtasks, MAX_PARALLEL_SUBAGENTS)
        single = _estimate_single_agent_tokens(context_tokens, tool_schema_tokens, system_tokens)
        multi = _estimate_multi_agent_tokens(
            context_tokens, n, shared_context_tokens, system_tokens,
            tool_schema_tokens, synthesis_tokens=600)
        return RouterDecision(
            True, "用户明确要求多 Agent/独立复核", subtask_count=n,
            parallelism_gain=True, quality_review=quality_review,
            estimated_single_tokens=single, estimated_multi_tokens=multi,
            shared_context_tokens=shared_context_tokens, synthesis_tokens=600, model=model)
    if independent_subtasks >= 2 and shared_context_tokens <= 2500:
        n = min(independent_subtasks, MAX_PARALLEL_SUBAGENTS)
        single = _estimate_single_agent_tokens(context_tokens, tool_schema_tokens, system_tokens)
        multi = _estimate_multi_agent_tokens(
            context_tokens, n, shared_context_tokens, system_tokens,
            tool_schema_tokens, synthesis_tokens=500)
        gain = multi <= single * 1.15 or (user_speed_priority and multi <= single * 1.5)
        if gain:
            return RouterDecision(
                True, "多个独立子任务且局部上下文小", subtask_count=n,
                parallelism_gain=True,
                estimated_single_tokens=single, estimated_multi_tokens=multi,
                shared_context_tokens=shared_context_tokens, synthesis_tokens=500,
                model=model)
        return RouterDecision(
            False, f"多 Agent 预计 {multi} tokens > 单 Agent {single}，无收益",
            estimated_single_tokens=single, estimated_multi_tokens=multi,
            shared_context_tokens=shared_context_tokens, synthesis_tokens=500, model=model)
    return RouterDecision(False, "未满足拆分收益条件，主 Agent 执行")


# ==================== SubtaskEnvelope ====================

@dataclass
class FileRef:
    path: str
    line_ranges: list = field(default_factory=list)
    content_hash: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "line_ranges": self.line_ranges,
                "content_hash": self.content_hash}


@dataclass
class SubtaskEnvelope:
    """子任务上下文信封：裁剪后的最小必要信息。"""

    subtask_id: str
    objective: str
    success_criteria: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    relevant_context: str = ""
    file_refs: list = field(default_factory=list)      # [FileRef]
    artifact_refs: list = field(default_factory=list)  # 已有结果引用，不复制正文
    allowed_tools: list = field(default_factory=list)
    write_scope: list = field(default_factory=list)
    token_budget: dict = field(default_factory=dict)
    output_schema: str = "compact"
    stop_conditions: list = field(default_factory=list)
    failure_policy: str = "返回证据，不自行扩大任务范围"

    def to_text(self) -> str:
        """序列化为发给子 Agent 的指令文本（信封不携带完整历史）。"""
        lines = [
            f"子任务 {self.subtask_id}",
            f"目标：{self.objective}",
        ]
        if self.success_criteria:
            lines.append("完成标准：" + "；".join(self.success_criteria))
        if self.constraints:
            lines.append("约束：" + "；".join(self.constraints))
        if self.relevant_context:
            lines.append("相关上下文：\n" + self.relevant_context[:ENVELOPE_MAX_CONTEXT_CHARS])
        if self.file_refs:
            refs = "；".join(f"{r.path}（hash={r.content_hash[:8]}）" for r in self.file_refs)
            lines.append("文件引用：" + refs)
        if self.artifact_refs:
            lines.append("已有结果引用：" + "；".join(str(a) for a in self.artifact_refs))
        if self.allowed_tools:
            lines.append("可用工具：" + "、".join(self.allowed_tools))
        if self.write_scope:
            lines.append("允许修改：" + "；".join(self.write_scope))
        if self.stop_conditions:
            lines.append("停止条件：" + "；".join(self.stop_conditions))
        lines.append(f"失败策略：{self.failure_policy}")
        lines.append("输出协议：只返回紧凑 JSON（status/summary/findings/evidence/"
                     "changes/tests/risks/artifact_ids/remaining_work/token_usage），"
                     "不返回思维过程与完整日志。")
        return "\n".join(lines)


def make_envelope(*, subtask_id: str, objective: str, success_criteria: list,
                  constraints: list, relevant_context: str = "",
                  file_refs: list[FileRef] | None = None,
                  artifact_refs: list | None = None,
                  allowed_tools: list | None = None,
                  write_scope: list | None = None,
                  token_budget: int = SUBAGENT_BUDGET_TOKENS,
                  stop_conditions: list | None = None) -> SubtaskEnvelope:
    """构造信封：相关上下文强制裁剪，文件按 hash 引用。"""
    return SubtaskEnvelope(
        subtask_id=subtask_id, objective=objective,
        success_criteria=list(success_criteria or []),
        constraints=list(constraints or []),
        relevant_context=(relevant_context or "")[:ENVELOPE_MAX_CONTEXT_CHARS],
        file_refs=list(file_refs or []),
        artifact_refs=list(artifact_refs or []),
        allowed_tools=list(allowed_tools or []),
        write_scope=list(write_scope or []),
        token_budget={"input": int(token_budget * 0.5), "output": int(token_budget * 0.3),
                      "reasoning": int(token_budget * 0.2)},
        stop_conditions=list(stop_conditions or []),
    )


def parse_subagent_output(text: str) -> dict:
    """解析子 Agent 紧凑输出；非 JSON 时退化为安全包装（不丢证据）。"""
    text = (text or "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return {
                "status": parsed.get("status", "completed"),
                "summary": str(parsed.get("summary") or "")[:800],
                "findings": list(parsed.get("findings") or [])[:20],
                "evidence": list(parsed.get("evidence") or [])[:20],
                "changes": list(parsed.get("changes") or [])[:20],
                "tests": list(parsed.get("tests") or [])[:20],
                "risks": list(parsed.get("risks") or [])[:10],
                "artifact_ids": list(parsed.get("artifact_ids") or [])[:20],
                "remaining_work": list(parsed.get("remaining_work") or [])[:10],
                "token_usage": parsed.get("token_usage") if isinstance(
                    parsed.get("token_usage"), dict) else {},
                "raw": parsed,
            }
    except (json.JSONDecodeError, ValueError):
        pass
    # 非 JSON（旧格式/截断）：保留原文作为 summary，标记状态
    return {"status": "partial", "summary": text[:SUBAGENT_OUTPUT_MAX_CHARS],
            "findings": [], "evidence": [], "changes": [], "tests": [],
            "risks": [], "artifact_ids": [], "remaining_work": [],
            "token_usage": {}, "raw": text}


# ==================== ArtifactRegistry ====================

def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


class ArtifactRegistry:
    """跨子 Agent 防重复工作：文件/搜索/工具结果按指纹共享。"""

    def __init__(self, ttl: float = 300.0):
        self._lock = threading.Lock()
        self._files: dict[str, dict] = {}       # key: path -> entry
        self._queries: dict[str, dict] = {}     # key: query_hash -> entry
        self._tools: dict[str, tuple[float, str]] = {}  # key: name|args_hash -> (ts, result_id)
        self._ttl = ttl

    def clear(self) -> None:
        with self._lock:
            self._files.clear()
            self._queries.clear()
            self._tools.clear()

    # 文件读取结果
    def register_file(self, path: str, mtime: float, size: int,
                      content_hash: str, snippet: str = "") -> None:
        with self._lock:
            self._files[path] = {"mtime": mtime, "size": size,
                                 "content_hash": content_hash,
                                 "snippet": snippet[:500], "ts": time.time()}

    def get_file(self, path: str, mtime: float, size: int) -> dict | None:
        with self._lock:
            e = self._files.get(path)
            if e and e["mtime"] == mtime and e["size"] == size:
                return e
            return None

    def file_hash(self, path: str) -> str | None:
        with self._lock:
            e = self._files.get(path)
            return e["content_hash"] if e else None

    # 搜索结果（query_hash + 数据版本）
    def register_query(self, query: str, data_version: str, results: list) -> None:
        with self._lock:
            self._queries[_hash_text(query)] = {"version": data_version,
                                                "results": list(results),
                                                "ts": time.time()}

    def get_query(self, query: str, data_version: str) -> list | None:
        with self._lock:
            e = self._queries.get(_hash_text(query))
            if e and e["version"] == data_version:
                return e["results"]
            return None

    # 工具结果（name + canonical args hash + TTL 有效期）
    def register_tool(self, name: str, args: dict, result_id: str) -> None:
        key = f"{name}|{_hash_text(json.dumps(args, sort_keys=True, ensure_ascii=False))}"
        with self._lock:
            self._tools[key] = (time.monotonic(), result_id)

    def get_tool(self, name: str, args: dict) -> str | None:
        key = f"{name}|{_hash_text(json.dumps(args, sort_keys=True, ensure_ascii=False))}"
        with self._lock:
            hit = self._tools.get(key)
            if hit and time.monotonic() - hit[0] <= self._ttl:
                return hit[1]
            return None

    def snapshot(self) -> dict:
        with self._lock:
            return {"files": len(self._files), "queries": len(self._queries),
                    "tools": len(self._tools)}
