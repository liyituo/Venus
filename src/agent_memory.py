"""Agent 记忆系统：L0-L3 分层记忆 + 动态 Skill + CodeGraph（借鉴 TencentDB-Agent-Memory）。

架构等价移植（零 Docker/数据库）：
- L0 原文归档：l0_events.jsonl 只追加（会话有裁剪/清空，不承担永久层职责）
- L1 原子记忆：带版本信封、可追溯 source_refs、可纠正/遗忘（superseded/retracted）
- L2 场景归纳：从现有压缩摘要旁路派生（不改变压缩流程），注入只给路径，正文按需加载
- L3 长期画像：明确偏好一次即入；推断 pattern 需 ≥2 个独立会话；derived_from 反查
- 动态 Skill：candidate → active → retired 状态机，不覆盖同名静态 Skill
- CodeGraph：Python 用 ast，其他语言正则回退；按工作区隔离；增量更新

原则：
- 只保存能追溯到用户明确陈述或外部工具结果的内容，不存隐藏推理/人格猜测
- 提取全部保守：规则候选优先，无法可靠结构化才调 LLM（off 模式）
- 并发：有界 MemoryJobQueue + 单 MemoryWorker；幂等（extraction_cursors）
- 落盘：临时文件 + flush + fsync + os.replace + .bak；损坏改名 .corrupt 保留现场
- 所有路径经 _memory_file() 动态解析（测试可 lambda 重定向，禁止 import 时缓存）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

log = logging.getLogger("llm-backend")

# 模块级写锁：L1/L2/L3 的读-改-写必须串行化（Windows 上并发 replace 会
# 因文件锁冲突抛 WinError 5，且丢更新）。召回访问计数同样走锁。
_mem_lock = threading.Lock()

# ---- 常量 ----
MEMORY_SCHEMA_VERSION = 1
L0_MAX_BYTES = 10_000_000        # l0 归档单文件上限（超出轮转 .1）
L1_MAX_ITEMS = 2000              # L1 记忆 LRU 上限
L1_LOOKBACK_MESSAGES = 40        # 提取时回看最近消息数
PROFILE_INJECT_CHARS = 300       # L3 注入版字符上限
RECALL_DEFAULT_K = 5
SKILL_CANDIDATE_MIN_STEPS = 2    # 生成 Skill 候选的最小工具步数
CG_MAX_FILES = 2000              # CodeGraph 单工作区文件上限


def _memory_file(name: str) -> Path:
    """记忆数据文件路径（每次 I/O 动态解析：测试可 monkeypatch 重定向）。

    禁止在模块 import 时缓存路径——否则测试里的 lambda 重定向不会生效。
    """
    from data_paths import data_file
    return data_file("memory") / name


def _sha(text) -> str:
    if isinstance(text, bytes):
        return hashlib.sha256(text).hexdigest()[:16]
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _now() -> float:
    return time.time()


# ======================================================================
# 原子写（复用会话文件模式：临时文件 + flush + fsync + replace + .bak）
# ======================================================================
def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        # 主文件落盘后更新 .bak（用于损坏恢复）
        import shutil
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _atomic_append_jsonl(path: Path, entry: dict) -> None:
    """只追加一行 JSON（L0 归档用）。轮转：超限改名 .1。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > L0_MAX_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _load_json_robust(path: Path, default: dict) -> dict:
    """加载 JSON；损坏时改名 .corrupt 保留现场，返回默认（不静默清空数据）。"""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        try:
            path.replace(path.with_name(path.name + f".corrupt-{int(time.time())}"))
        except OSError:
            pass
        return default


# ======================================================================
# L0 原文归档（只追加）
# ======================================================================
def l0_append(*, session_id, request_id, role, content, workspace="") -> None:
    try:
        _atomic_append_jsonl(_memory_file("l0_events.jsonl"), {
            "event_id": uuid.uuid4().hex[:12],
            "session_id": session_id, "request_id": request_id,
            "role": role, "content": content,
            "ts": _now(), "workspace": workspace,
        })
    except OSError:
        pass    # 归档失败不影响主流程


# ======================================================================
# L1 原子记忆（版本信封）
# ======================================================================
def _empty_l1_envelope() -> dict:
    return {"schema_version": MEMORY_SCHEMA_VERSION, "revision": 0,
            "extraction_cursors": {}, "items": []}


def _migrate_l1(data: dict) -> dict:
    """旧 schema 迁移：裸数组 → 信封。"""
    if "schema_version" in data and "items" in data:
        return data
    if isinstance(data, list):
        return {"schema_version": MEMORY_SCHEMA_VERSION, "revision": 1,
                "extraction_cursors": {},
                "items": [{"id": str(i + 1), "type": "fact", "content": str(x),
                           "status": "active", "created_at": _now()}
                          for i, x in enumerate(data)]}
    return _empty_l1_envelope()


def load_l1() -> dict:
    """加载 L1 信封。兼容裸数组旧格式（自动迁移）；损坏文件改名 .corrupt。"""
    path = _memory_file("l1_memories.json")
    raw: dict | list = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            try:
                path.replace(path.with_name(path.name + f".corrupt-{int(time.time())}"))
            except OSError:
                pass
            raw = {}
    if not isinstance(raw, (dict, list)):
        raw = {}
    return _migrate_l1(raw)


def save_l1(data: dict) -> None:
    data["revision"] = int(data.get("revision") or 0) + 1
    _atomic_write_json(_memory_file("l1_memories.json"), data)


# ---- 冲突检测与去重 ----
def _memories_conflict(a: dict, b: dict) -> bool:
    """两条记忆是否语义冲突（同 scope 同主题、内容实质不同）。"""
    if a.get("scope") != b.get("scope"):
        return False
    ka, kb = set(a.get("retrieval_keys") or []), set(b.get("retrieval_keys") or [])
    if not (ka & kb):
        return False
    return (a.get("content") or "").strip() != (b.get("content") or "").strip()


def _is_duplicate(a: dict, b: dict) -> bool:
    return (a.get("content") or "").strip() == (b.get("content") or "").strip()


def add_memories(new_items: list[dict], *, session_id=None, request_id=None,
                 cursor: dict | None = None) -> int:
    """入库新记忆：去重 + 冲突（新记忆 supersede 旧记忆）+ LRU（pinned 保护）。

    返回实际新增条数。cursor 记录提取进度（幂等）。
    读-改-写全程持 _mem_lock（并发安全 + 防丢更新）。
    """
    with _mem_lock:
        return _add_memories_locked(new_items, session_id=session_id,
                                    request_id=request_id, cursor=cursor)


def _add_memories_locked(new_items: list[dict], *, session_id=None,
                         request_id=None, cursor: dict | None = None) -> int:
    data = load_l1()
    if not new_items:
        # 空入库也要记录 cursor（幂等）：处理过但无候选的请求不重复处理
        if cursor is not None:
            cursors = data.setdefault("extraction_cursors", {})
            key = f"session-{session_id}" if session_id else "default"
            cursors[key] = {**cursors.get(key, {}), **cursor}
            save_l1(data)
        return 0
    items = data.get("items") or []
    added = 0
    all_supersedes: set[str] = set()
    for new in new_items:
        # 去重：内容相同 → 合并证据（source_refs 累积会话证据，供 L3 pattern
        # 判断「≥2 个独立会话」），并刷新访问时间；不产生重复条目
        dup = next((e for e in items if e.get("status") == "active"
                    and _is_duplicate(e, new)), None)
        if dup is not None:
            old_refs = [json.loads(json.dumps(r)) for r in (dup.get("source_refs") or [])]
            new_refs = [json.loads(json.dumps(r)) for r in (new.get("source_refs") or [])]
            seen = {json.dumps(r, sort_keys=True) for r in old_refs}
            for r in new_refs:
                if json.dumps(r, sort_keys=True) not in seen:
                    old_refs.append(r)
            dup["source_refs"] = old_refs[:20]
            dup["updated_at"] = _now()
            if new.get("explicit") and not dup.get("explicit"):
                dup["explicit"] = True        # 后续显式表达升级为显式记忆
            continue
        # 冲突：新记忆标记 supersedes 旧记忆
        supersedes = [e["id"] for e in items if e.get("status") == "active"
                      and _memories_conflict(e, new)]
        if supersedes:
            new = dict(new)
            new["supersedes"] = supersedes
            new.setdefault("updated_at", _now())
            all_supersedes |= set(supersedes)
        items.append(new)
        added += 1
    for old_id in all_supersedes:
        for e in items:
            if e.get("id") == old_id and e.get("status") == "active":
                e["status"] = "superseded"
                e["updated_at"] = _now()
    # LRU：pinned 永不淘汰
    pinned = [e for e in items if e.get("pinned")]
    unpinned = [e for e in items if not e.get("pinned")]
    if len(unpinned) > L1_MAX_ITEMS:
        # 只淘汰非 active 的旧记忆；若仍超限按最旧 active 淘汰
        removable = [e for e in unpinned if e.get("status") != "active"]
        overflow = len(unpinned) - L1_MAX_ITEMS
        dropped = removable[:overflow]
        if len(dropped) < overflow:
            actives = [e for e in unpinned if e.get("status") == "active"]
            actives.sort(key=lambda e: e.get("last_accessed_at") or e.get("created_at") or 0)
            dropped += actives[: overflow - len(dropped)]
        for e in dropped:
            unpinned.remove(e)
        items = pinned + unpinned
    data["items"] = items
    if cursor is not None:
        cursors = data.setdefault("extraction_cursors", {})
        key = f"session-{session_id}" if session_id else "default"
        cursors[key] = {**cursors.get(key, {}), **cursor}
    save_l1(data)
    return added


# ---- 内部接口：遗忘/纠正/撤销 ----
def forget_memory(memory_id: str) -> bool:
    with _mem_lock:
        data = load_l1()
        for e in data.get("items", []):
            if e.get("id") == memory_id:
                e["status"] = "retracted"
                e["updated_at"] = _now()
                save_l1(data)
                return True
    return False


def forget_session_memories(session_id) -> int:
    """删除会话后调用：清除该会话所有 source_refs 对应的记忆（保留 pinned 显式记忆）。"""
    with _mem_lock:
        data = load_l1()
        n = 0
        for e in data.get("items", []):
            if e.get("pinned"):
                continue
            refs = e.get("source_refs") or []
            if any(str(r.get("session_id")) == str(session_id) for r in refs):
                e["status"] = "retracted"
                e["updated_at"] = _now()
                n += 1
        if n:
            save_l1(data)
    return n


def correct_memory(memory_id: str, replacement: str) -> bool:
    """纠正记忆：旧条目 retracted + 新条目（显式、confidence 1.0、supersedes 旧）。"""
    with _mem_lock:
        data = load_l1()
        old = next((e for e in data.get("items", []) if e.get("id") == memory_id), None)
        if old is None:
            return False
        old["status"] = "retracted"
        old["updated_at"] = _now()
        new = {"id": uuid.uuid4().hex[:12], "type": old.get("type", "fact"),
               "content": replacement, "scope": old.get("scope", "global"),
               "workspace_id": old.get("workspace_id", ""),
               "confidence": 1.0, "status": "active", "explicit": True,
               "pinned": old.get("pinned", False),
               "retrieval_keys": old.get("retrieval_keys", []),
               "source_refs": old.get("source_refs", []),
               "supersedes": [old["id"]], "created_at": _now(), "updated_at": _now(),
               "last_accessed_at": _now(), "access_count": 0}
        data.setdefault("items", []).append(new)
        save_l1(data)
    return True


def retract_memory(memory_id: str) -> bool:
    return forget_memory(memory_id)


def list_memories(*, status: str = "active", limit: int = 100,
                  workspace_id: str = "") -> list[dict]:
    """列出 L1 记忆（API 用；默认仅 active）。"""
    limit = max(1, min(int(limit or 100), 500))
    with _mem_lock:
        data = load_l1()
        items = data.get("items") or []
        out = []
        for e in reversed(items):
            if status and e.get("status") != status:
                continue
            if workspace_id and e.get("scope") == "workspace":
                if (e.get("workspace_id") or "") != workspace_id:
                    continue
            out.append({
                "id": e.get("id"),
                "type": e.get("type"),
                "content": e.get("content"),
                "scope": e.get("scope"),
                "confidence": e.get("confidence"),
                "explicit": e.get("explicit"),
                "pinned": e.get("pinned"),
                "status": e.get("status"),
                "created_at": e.get("created_at"),
                "updated_at": e.get("updated_at"),
            })
            if len(out) >= limit:
                break
        return out


def save_profile_preferences(preferences: list[dict]) -> dict:
    """手动更新 L3 画像偏好列表（API 用）。"""
    with _mem_lock:
        profile = load_profile()
        cleaned = []
        for p in preferences:
            if not isinstance(p, dict):
                continue
            content = str(p.get("content") or "").strip()
            if not content:
                continue
            cleaned.append({
                "content": content[:500],
                "confidence": float(p.get("confidence") or 1.0),
                "explicit": bool(p.get("explicit", True)),
            })
        profile["preferences"] = cleaned[:50]
        profile["updated"] = _now()
        _atomic_write_json(_memory_file("profile.json"), profile)
    return profile


def inject_preview(query: str, workspace: str = "") -> dict:
    """派活/对话前预览将注入的记忆上下文。"""
    profile_text = profile_inject_text()
    hits = recall_memories(query, top_k=RECALL_DEFAULT_K, workspace_id=workspace)
    return {
        "profile": profile_text,
        "profile_preferences": len((load_profile().get("preferences") or [])),
        "recalled": hits,
        "recalled_count": len(hits),
    }


# ======================================================================
# L2 场景派生（从现有压缩摘要旁路写入，不改变压缩流程）
# ======================================================================
def add_scenario_from_summary(*, summary: dict, session_id) -> bool:
    """压缩成功后旁路调用：按 objective 聚合写场景。正文按需加载（召回时再取）。"""
    with _mem_lock:
        objective = (summary.get("objective") or "").strip()
        if not objective:
            return False
        path = _memory_file("l2_scenarios.json")
        data = _load_json_robust(path, {"schema_version": 1, "scenarios": []})
        scenarios = data.setdefault("scenarios", [])
        keys = [str(k) for k in (summary.get("retrieval_keys") or [])][:20]
        # 同 objective 聚合
        for s in scenarios:
            if (s.get("topic") or "").strip() == objective:
                s["source_sessions"] = sorted(set(s.get("source_sessions") or [])
                                              | {session_id})
                s["retrieval_keys"] = sorted(set(s.get("retrieval_keys") or []) | set(keys))
                s["updated_at"] = _now()
                s["summary"] = _summary_to_text_stable(summary)
                _atomic_write_json(path, data)
                return True
        scenarios.append({"id": uuid.uuid4().hex[:12], "topic": objective,
                          "summary": _summary_to_text_stable(summary),
                          "members": list(summary.get("open_tasks") or []),
                          "retrieval_keys": keys,
                          "source_sessions": [session_id],
                          "created_at": _now(), "updated_at": _now()})
        if len(scenarios) > 200:
            scenarios.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
            data["scenarios"] = scenarios[:200]
        _atomic_write_json(path, data)
    return True


def _summary_to_text_stable(summary: dict) -> str:
    parts = []
    if summary.get("objective"):
        parts.append(f"目标：{summary['objective']}")
    for label, key in (("标准", "success_criteria"), ("约束", "user_constraints"),
                       ("决定", "decisions"), ("未完成", "open_tasks")):
        vals = summary.get(key) or []
        if vals:
            parts.append(f"{label}：" + "；".join(sorted(str(v) for v in vals)))
    if summary.get("files_and_artifacts"):
        parts.append("文件：" + "；".join(str(v) for v in summary["files_and_artifacts"]))
    if summary.get("active_errors"):
        parts.append("未解决错误：" + "；".join(str(v) for v in summary["active_errors"]))
    return "\n".join(parts)[:800]


def list_scenario_paths() -> list[dict]:
    """场景路径清单（注入只给路径+标题，正文按需加载）。"""
    data = _load_json_robust(_memory_file("l2_scenarios.json"), {})
    return [{"id": s.get("id"), "topic": s.get("topic"),
             "updated_at": s.get("updated_at")} for s in data.get("scenarios", [])]


def get_scenario_body(scenario_id: str) -> str | None:
    data = _load_json_robust(_memory_file("l2_scenarios.json"), {})
    for s in data.get("scenarios", []):
        if s.get("id") == scenario_id:
            return s.get("summary")
    return None


# ======================================================================
# L3 长期画像（确定性聚合；derived_from 反查）
# ======================================================================
def rebuild_profile() -> dict:
    """从 L1 聚合画像：
    - preference/constraint 显式条目：一次即入
    - pattern（推断）：需 ≥2 个独立会话支持
    - 矛盾取最新明确表达；每条保留 derived_from
    """
    with _mem_lock:
        data = load_l1()
        actives = [e for e in data.get("items", []) if e.get("status") == "active"]
        prefs: dict[str, list] = {}       # content -> [items]
        for e in actives:
            if e.get("type") in ("preference", "constraint") and e.get("explicit"):
                prefs.setdefault((e.get("content") or "").strip(), []).append(e)
        profile = {"preferences": [], "work_patterns": [], "derived_from": {},
                   "workspace": "", "updated": _now()}
        for content, es in prefs.items():
            latest = max(es, key=lambda e: e.get("created_at") or 0)
            profile["preferences"].append({
                "content": content, "confidence": latest.get("confidence", 0.9),
                "scope": latest.get("scope", "global"),
                "workspace_id": latest.get("workspace_id", ""),
                "explicit": True, "sessions": len({str(r.get("session_id"))
                                                   for e in es for r in (e.get("source_refs") or [])}),
            })
            profile["derived_from"][content] = [e["id"] for e in es]
        # 推断 pattern：非显式条目按内容聚合，≥2 独立会话
        inferred: dict[str, list] = {}
        for e in actives:
            if e.get("type") in ("fact", "decision") and not e.get("explicit"):
                inferred.setdefault((e.get("content") or "").strip(), []).append(e)
        for content, es in inferred.items():
            sessions = {str(r.get("session_id")) for e in es
                        for r in (e.get("source_refs") or [])}
            if len(sessions) >= 2:
                latest = max(es, key=lambda e: e.get("created_at") or 0)
                profile["work_patterns"].append({"content": content,
                                                 "confidence": 0.6,
                                                 "sessions": len(sessions)})
                profile["derived_from"][content] = [e["id"] for e in es]
        profile["preferences"].sort(key=lambda p: p.get("confidence", 0), reverse=True)
        _atomic_write_json(_memory_file("profile.json"), profile)
    return profile


def load_profile() -> dict:
    return _load_json_robust(_memory_file("profile.json"),
                             {"preferences": [], "work_patterns": [],
                              "derived_from": {}, "workspace": "", "updated": 0})


def profile_inject_text(profile: dict | None = None, limit: int = PROFILE_INJECT_CHARS) -> str:
    """画像注入版（≤limit 字符）。磁盘完整版不受此限制。"""
    p = profile if profile is not None else load_profile()
    parts = []
    for pref in p.get("preferences") or []:
        parts.append("· " + str(pref.get("content", ""))[:80])
    for pat in p.get("work_patterns") or []:
        parts.append("· " + str(pat.get("content", ""))[:80])
    text = "；".join(parts)
    return text[:limit]


# ======================================================================
# 召回（复用 history_index.tokenize；scope 过滤 + 访问计数）
# ======================================================================
def recall_memories(query: str, top_k: int = RECALL_DEFAULT_K,
                    workspace_id: str = "") -> list[dict]:
    with _mem_lock:
        from history_index import tokenize
        data = load_l1()
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        actives = [e for e in data.get("items", []) if e.get("status") == "active"]
        # scope 过滤：workspace 记忆只在同工作区可见；global 始终可见
        visible = [e for e in actives
                   if e.get("scope") != "workspace"
                   or (e.get("workspace_id") or "") == (workspace_id or "")]
        scored = []
        for e in visible:
            keys = [k.lower() for k in (e.get("retrieval_keys") or [])]
            content = (e.get("content") or "").lower()
            score = 0
            for t in q_tokens:
                if t in content:
                    score += 2
                if any(t in k or k in t for k in keys):
                    score += 3
            if score <= 0:
                continue        # 无词面匹配不召回（显式加分不能替代相关性）
            if e.get("explicit"):
                score += 1
            scored.append((score, e))
        scored.sort(key=lambda kv: (-kv[0], -(kv[1].get("created_at") or 0)))
        out = []
        for score, e in scored[:top_k]:
            e["access_count"] = int(e.get("access_count") or 0) + 1
            e["last_accessed_at"] = _now()
            out.append({"id": e["id"], "type": e["type"], "content": e["content"],
                        "confidence": e.get("confidence"), "explicit": e.get("explicit"),
                        "score": score, "source_sessions": sorted({
                            str(r.get("session_id")) for r in (e.get("source_refs") or [])})})
        if out:
            # 访问计数落盘（不破坏失败安全：load_l1 已持有最新数据）
            save_l1(data)
    return out


# ======================================================================
# 动态 Skill（candidate → active → retired 状态机）
# ======================================================================
def load_skills_dynamic() -> list[dict]:
    data = _load_json_robust(_memory_file("skills_dynamic.json"),
                             {"schema_version": 1, "skills": []})
    return data.get("skills") or []


def save_skills_dynamic(skills: list[dict]) -> None:
    _atomic_write_json(_memory_file("skills_dynamic.json"),
                       {"schema_version": 1, "skills": skills})


def skill_names_used() -> set[str]:
    """所有已占用 skill 名（动态+静态），防同名覆盖。"""
    used = {s.get("name", "") for s in load_skills_dynamic()}
    try:
        from llm_server import _scan_skills
        used |= {s.get("name", "") for s in _scan_skills()}
    except Exception:
        pass
    return used


def add_skill_candidate(*, name, trigger, steps, verify, artifacts,
                        source_run, scope="workspace", private=True) -> dict | None:
    """新增候选 skill；重名（动态或静态）拒绝。"""
    if not name or name in skill_names_used():
        return None
    skills = load_skills_dynamic()
    skill = {"id": uuid.uuid4().hex[:12], "name": name, "version": 1,
             "status": "candidate", "trigger": trigger, "non_triggers": [],
             "steps": steps, "verify": verify, "artifacts": artifacts,
             "source_runs": [source_run],
             "success_evidence": [], "success_count": 1, "failure_count": 0,
             "scope": scope, "private": private,
             "content_hash": _sha(json.dumps(steps, sort_keys=True, ensure_ascii=False)),
             "created_at": _now(), "updated_at": _now()}
    skills.append(skill)
    save_skills_dynamic(skills)
    return skill


def record_skill_success(name: str, run: dict) -> bool:
    """候选 skill 被成功复用：成功次数 +1；≥2 次独立成功 → 自动激活。"""
    skills = load_skills_dynamic()
    for s in skills:
        if s.get("name") == name:
            s["success_count"] = int(s.get("success_count") or 0) + 1
            s["success_evidence"].append(run)
            s["source_runs"].append(run)
            s["updated_at"] = _now()
            if (s.get("status") == "candidate" and s["success_count"] >= 2
                    and len({r.get("session_id") for r in s.get("source_runs") if r.get("session_id")}) >= 2):
                s["status"] = "active"
            save_skills_dynamic(skills)
            return True
    return False


def activate_skill(name: str, by_user: bool = True) -> bool:
    """激活候选 skill（用户批准或系统判定）。"""
    skills = load_skills_dynamic()
    for s in skills:
        if s.get("name") == name and s.get("status") == "candidate":
            s["status"] = "active"
            s["updated_at"] = _now()
            save_skills_dynamic(skills)
            return True
    return False


def recall_skills(query: str, top_k: int = 3) -> list[dict]:
    """召回 active 技能（触发条件词法匹配）。"""
    from history_index import tokenize
    q_tokens = tokenize(query)
    out = []
    for s in load_skills_dynamic():
        if s.get("status") != "active":
            continue
        trig = (s.get("trigger") or "").lower()
        non_triggers = s.get("non_triggers") or []
        if any(nt and nt.lower() in query.lower() for nt in non_triggers):
            continue
        score = sum(1 for t in q_tokens if t in trig)
        if score:
            out.append({"name": s["name"], "description": trig[:120],
                        "steps": s.get("steps", []), "score": score})
    out.sort(key=lambda x: -x["score"])
    return out[:top_k]


# ======================================================================
# 有界任务队列 + 单 MemoryWorker（并发安全）
# ======================================================================
class MemoryJobQueue:
    def __init__(self, max_size: int = 32):
        self._q: deque = deque()
        self._lock = threading.Lock()
        self._max = max_size

    def put(self, job: dict) -> bool:
        """满则丢弃最旧（记忆提取不能阻塞任务主流程）。"""
        with self._lock:
            if len(self._q) >= self._max:
                self._q.popleft()
            self._q.append(job)
            return True

    def get(self, timeout: float = 1.0) -> dict | None:
        with self._lock:
            return self._q.popleft() if self._q else None


class MemoryWorker(threading.Thread):
    """单消费者线程：幂等检查 → 提取 → 落盘。失败静默不影响主流程。"""

    def __init__(self, queue: MemoryJobQueue, handler):
        super().__init__(daemon=True, name="memory-worker")
        self.queue = queue
        self.handler = handler      # handler(job) -> None
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            job = self.queue.get()
            if job is None:
                time.sleep(0.5)
                continue
            try:
                self.handler(job)
            except Exception:
                log.exception("memory worker 处理失败（不影响主流程）")

    def stop(self) -> None:
        self._stop.set()


# ======================================================================
# CodeGraph：符号索引（Python ast 优先，正则回退；按工作区隔离；增量）
# ======================================================================
_CG_SYMBOL_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)(.*)$")
_CG_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
_CG_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")

_PY_SUFFIXES = (".py",)


def _workspace_hash(root: str) -> str:
    return _sha(str(Path(root).resolve()))


def _cg_parse_python(text: str) -> dict:
    """用 ast 提取符号/import/调用（confidence=high）。语法错误抛 SyntaxError。"""
    import ast
    tree = ast.parse(text)
    out = {"symbols": [], "imports": [], "calls": {}, "confidence": "high"}
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported_names.add(name)
                out["imports"].append({"module": getattr(node, "module", None)
                                       or alias.name, "alias": name})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = {"FunctionDef": "function", "AsyncFunctionDef": "function",
                    "ClassDef": "class"}.get(type(node).__name__, "symbol")
            out["symbols"].append({"name": node.name, "line": node.lineno,
                                   "kind": kind})
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out["calls"][f.id] = out["calls"].get(f.id, 0) + 1
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                out["calls"][f"{f.value.id}.{f.attr}"] = out["calls"].get(f"{f.value.id}.{f.attr}", 0) + 1
    return out


def _cg_parse_regex(text: str) -> dict:
    """非 Python 或语法错误回退：正则提取（confidence=heuristic）。"""
    out = {"symbols": [], "imports": [], "calls": {}, "confidence": "heuristic"}
    for i, line in enumerate(text.splitlines(), 1):
        m = _CG_SYMBOL_RE.match(line)
        if m:
            out["symbols"].append({"name": m.group(1), "line": i, "kind": "symbol"})
        mm = _CG_CALL_RE.search(line)
        if mm:
            name = mm.group(1)
            if name not in ("if", "for", "while", "return", "print"):
                out["calls"][name] = out["calls"].get(name, 0) + 1
    for mm in _CG_IMPORT_RE.finditer(text):
        mod = mm.group(1) or mm.group(2)
        if mod:
            out["imports"].append({"module": mod, "alias": mod.split(".")[0]})
    return out


def _cg_parse_file(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > 1_000_000:
        return None
    if "\x00" in text[:1024]:
        return None
    if path.suffix.lower() in _PY_SUFFIXES:
        try:
            return _cg_parse_python(text)
        except SyntaxError:
            return _cg_parse_regex(text)
    return _cg_parse_regex(text)


def build_codegraph(workspace_root, files_iter, max_files: int = CG_MAX_FILES) -> dict:
    """构建/增量更新单工作区符号索引。

    files_iter: (abs_path) 迭代器（llm_server 传 _iter_workspace_files）。
    按 mtime_ns+size+sha256 增量：文件未变跳过解析。
    返回 {files, built_at, truncated, per_file_confidence}。
    """
    root = Path(workspace_root)
    whash = _workspace_hash(str(root))
    path = _memory_file("codegraph.json")
    data = _load_json_robust(path, {"schema_version": 1, "workspaces": {}})
    ws = data.setdefault("workspaces", {}).setdefault(
        whash, {"root": str(root), "built_at": 0, "truncated": False, "files": {}})
    files = ws.setdefault("files", {})
    count = 0
    truncated = False
    # 物化迭代器：生成器只能迭代一次（二次迭代为空会导致下方
    # 「移除已不存在文件」把刚构建的文件全部删掉）
    file_list = list(files_iter)
    for item in file_list:
        # 兼容 yield (abs_p, rel) 元组迭代器（llm_server 的 _iter_workspace_files）
        if isinstance(item, (tuple, list)):
            abs_p = Path(item[0])
        else:
            abs_p = Path(item)
        try:
            st = abs_p.stat()
        except OSError:
            continue
        rel = str(abs_p)
        content_hash = _sha(abs_p.read_bytes()[:4096]) if st.st_size <= 2_000_000 else f"big-{st.st_size}"
        prev = files.get(rel)
        if prev and prev.get("mtime_ns") == st.st_mtime_ns and prev.get("size") == st.st_size \
                and prev.get("sha256") == content_hash:
            continue        # 未变化：增量跳过
        parsed = _cg_parse_file(abs_p)
        if parsed is None:
            continue
        files[rel] = {"symbols": parsed["symbols"], "imports": parsed["imports"],
                      "calls": parsed["calls"], "confidence": parsed["confidence"],
                      "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                      "sha256": content_hash}
        count += 1
        if count >= max_files:
            truncated = True
            break
    # 移除已不存在的文件
    existing = {str(item[0] if isinstance(item, (tuple, list)) else item)
                for item in file_list}
    for rel in [r for r in files if r not in existing]:
        files.pop(rel, None)
    ws["built_at"] = _now()
    ws["truncated"] = truncated
    if len(data["workspaces"]) > 4:      # 工作区上限（LRU：保留最近构建的）
        keep = sorted(data["workspaces"].items(),
                      key=lambda kv: kv[1].get("built_at") or 0, reverse=True)[:4]
        data["workspaces"] = dict(keep)
    _atomic_write_json(path, data)
    return ws


def codegraph_query(workspace_root, symbol: str, top_k: int = 10) -> dict:
    """符号查找：定义位置 + 调用方（谁调用它）。"""
    whash = _workspace_hash(str(workspace_root))
    data = _load_json_robust(_memory_file("codegraph.json"), {})
    ws = (data.get("workspaces") or {}).get(whash)
    if not ws:
        return {"ok": False, "detail": "CodeGraph 未构建（可先用 repo_map 触发）"}
    definitions, callers = [], []
    for rel, f in ws.get("files", {}).items():
        for s in f.get("symbols", []):
            if s.get("name") == symbol:
                definitions.append({"file": rel, "line": s.get("line"),
                                    "confidence": f.get("confidence")})
        calls = f.get("calls") or {}
        if symbol in calls:
            callers.append({"file": rel, "calls": calls[symbol],
                            "confidence": f.get("confidence")})
    return {"ok": True, "symbol": symbol,
            "definitions": definitions[:top_k], "callers": callers[:top_k],
            "truncated": ws.get("truncated", False)}


def codegraph_impact(workspace_root, target: str, depth: int = 1) -> dict:
    """影响分析：目标文件/符号的直接与间接依赖方（启发式置信度）。

    target 支持绝对路径、工作区相对路径或裸文件名（归一化匹配）。
    """
    whash = _workspace_hash(str(workspace_root))
    data = _load_json_robust(_memory_file("codegraph.json"), {})
    ws = (data.get("workspaces") or {}).get(whash)
    if not ws:
        return {"ok": False, "detail": "CodeGraph 未构建"}
    files = ws.get("files", {})
    target_norm = str(target).replace("\\", "/")
    # 目标文件 key（归一化：反斜杠→斜杠；相对/裸名 → 按结尾匹配）
    target_key = next((k for k in files
                       if k.replace("\\", "/") == target_norm
                       or k.replace("\\", "/").endswith("/" + target_norm)
                       or Path(k).name == Path(target).name), target_norm)
    direct = []
    # 目标自身符号集
    own_symbols = {s["name"] for s in files.get(target_key, {}).get("symbols", [])}
    for rel, f in files.items():
        if rel.replace("\\", "/") == target_norm:
            continue
        calls = f.get("calls") or {}
        imported_aliases = {i.get("alias", "") for i in f.get("imports", [])}
        hit = (any(s in calls for s in own_symbols)
               or Path(rel).name in calls
               or any(a in own_symbols for a in imported_aliases))
        if hit:
            direct.append({"file": rel, "confidence": f.get("confidence")})
    # 间接依赖：依赖了 direct dependents 的模块（一层启发式）
    indirect = []
    direct_names = {Path(d["file"]).stem for d in direct}
    for rel, f in files.items():
        if rel == target_norm or any(d["file"] == rel for d in direct):
            continue
        imported = {i.get("alias", "") for i in f.get("imports", [])}
        if imported & direct_names:
            indirect.append({"file": rel, "confidence": f.get("confidence")})
    return {"ok": True, "target": target_norm,
            "direct_dependents": direct[:20],
            "indirect": indirect[:10],
            "confidence": "high" if direct and all(
                d["confidence"] == "high" for d in direct) else "heuristic"}


# ======================================================================
# L1 保守提取（确定性规则优先；无法可靠结构化才 LLM 兜底）
# ======================================================================
# 只处理用户消息；排除代码块/引用/疑问/假设/密钥/注入
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_BLOCKQUOTE_RE = re.compile(r"^\s*>", re.M)
_QUESTION_HINTS = ("吗", "？", "?", "怎么", "如何", "为什么", "什么是", "帮我看看", "能不能")
_HYPOTHETICAL_HINTS = ("如果", "假如", "假设", "要是", "比如")
_INJECT_HINTS = ("无视", "安全规则", "系统提示", "扮演", "jailbreak",
                 "开发者模式")
# 提示注入组合（"忽略"单独太宽：会误杀「忽略那个文件」等正常指令，
# 仅当与安全/系统/规则组合出现才判定注入）
_INJECT_PATTERNS = (
    re.compile(r"忽略[^\n]{0,12}(安全规则|系统提示|之前的|所有指令|所有规则|设定|限制)", re.I),
    re.compile(r"(不要|别)[^\n]{0,12}(遵守|遵循)[^\n]{0,12}(安全|规则)", re.I),
)
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
                        r"Bearer\s+[A-Za-z0-9._\-]{12,}|password\s*[:=]|passwd\s*[:=]|"
                        r"token\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}|cookie\s*[:=])", re.I)

_PREF_RE = re.compile(r"(?:我)?(?:喜欢|偏好|习惯|想要|希望|更愿意|倾向于)")
# "别" 单独匹配会误杀「区别/级别/特别/别人」等常见词：只匹配「别」后接动作动词
_CONSTRAINT_RE = re.compile(
    r"(?:请|你要|你)?(?:不要|别(?=[用做动改碰管提再说去来写删加放开搞尝试])|不能|禁止|必须|务必|记住|以后)"
)
_DECISION_RE = re.compile(r"(?:我们|我)?(?:决定|确定|定了|就用|选)")

_RULE_TYPES = (
    ("preference", _PREF_RE),
    ("constraint", _CONSTRAINT_RE),
    ("decision", _DECISION_RE),
)


def _clean_user_text(text: str) -> str:
    """预处理：去代码块/引用行；截断超长。"""
    text = _CODE_FENCE_RE.sub(" ", text or "")
    text = _BLOCKQUOTE_RE.sub(" ", text)
    return text[:4000]


def _is_excluded(text: str) -> bool:
    if _SECRET_RE.search(text):
        return True
    if any(h in text.lower() for h in _INJECT_HINTS):
        return True
    if any(p.search(text) for p in _INJECT_PATTERNS):
        return True
    return False


def detect_l1_candidates(user_text: str) -> list[dict]:
    """确定性候选检测：只对用户消息；返回可直接结构化的候选。

    疑问/假设句不提取（无法确定是用户真实偏好）；
    密钥/注入内容永远排除。
    """
    text = _clean_user_text(user_text)
    if not text.strip() or _is_excluded(text):
        return []
    if any(h in text for h in _QUESTION_HINTS) and not _CONSTRAINT_RE.search(text):
        return []
    if any(h in text for h in _HYPOTHETICAL_HINTS):
        return []
    out = []
    for mtype, pat in _RULE_TYPES:
        if pat.search(text):
            content = text.strip()
            out.append({"type": mtype, "content": content[:200],
                        "confidence": 0.9, "explicit": True,
                        "retrieval_keys": _default_keys(content)})
    return out[:3]


def _default_keys(text: str) -> list[str]:
    """候选的默认检索键：文件路径/数字/技术词。"""
    keys = []
    for m in re.finditer(r"[A-Za-z0-9_./\\-]+\.[a-z]{1,5}\b", text):
        keys.append(m.group(0))
    for m in re.finditer(r"\b\d{2,6}\b", text):
        keys.append(m.group(0))
    return sorted(set(keys))[:12]


def _lesson_triplet(messages: list[dict]) -> list[dict]:
    """error_lesson 提取：必须「原因 + 修复 + 验证」三者齐全（否则不写）。"""
    text = "\n".join(str(m.get("content") or "") for m in messages)[-8000:]
    cause = re.search(r"(?:错误|error|失败|failed|异常)[^\n]{0,200}", text, re.I)
    fix = re.search(r"(?:修复|修复方式|解决|fixed|fix)[^\n]{0,200}", text, re.I)
    verify = re.search(r"(?:验证|测试通过|passed|PASS|✓)[^\n]{0,200}", text, re.I)
    if not (cause and fix and verify):
        return []
    return [{"type": "error_lesson",
             "content": f"问题：{cause.group(0).strip()[:120]}；修复：{fix.group(0).strip()[:120]}；验证：{verify.group(0).strip()[:120]}",
             "confidence": 0.85, "explicit": False,
             "retrieval_keys": _default_keys(text)}]


def extract_l1_from_run(record: dict, llm_extract_fn=None) -> list[dict]:
    """从 AgentRunRecord 提取 L1 候选（保守）。

    llm_extract_fn(messages) -> list[dict] | None：LLM 兜底（未提供或
    不可用时只走确定性规则）。规则有候选 → 直接结构化为显式记忆。
    """
    msgs = record.get("input_messages") or []
    # 只处理用户消息（含 l0 归档中的 user role）
    candidates: list[dict] = []
    user_texts = [str(m.get("content") or "") for m in msgs
                  if m.get("role") == "user"]
    for t in user_texts[-6:]:          # 只看最近 6 条用户消息
        candidates.extend(detect_l1_candidates(t))
    if not candidates:
        return []                       # 无候选：直接结束（不调 LLM）
    if record.get("status") == "error":
        # error：只提取明确偏好/约束，不生成 skill；不写 lesson（除非三要素齐全）
        candidates = [c for c in candidates if c["type"] in ("preference", "constraint")]
        return candidates
    if record.get("status") == "completed":
        candidates.extend(_lesson_triplet(msgs))
        return candidates
    return []                            # cancelled/partial：默认不提取
