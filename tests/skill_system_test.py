"""Skill 包与系统监控测试：扫描/frontmatter/load_skill 工具/system_status/免确认。
import os
os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")

SKILLS_DIR 重定向到临时目录，不触碰真实 skills/。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="pcagent_skill_"))
L.SKILLS_DIR = _TMP / "skills"
L.SKILLS_DIR.mkdir()

# 造两个技能包：一个带 frontmatter，一个不带
(L.SKILLS_DIR / "code_review").mkdir()
(L.SKILLS_DIR / "code_review" / "SKILL.md").write_text(
    "---\nname: code-review\ndescription: 代码审查流程：先 repo_map 再看 diff，最后给评分\n---\n"
    "# 代码审查\n\n1. 跑 git_diff 看改动\n2. 检查边界条件\n3. 输出评分\n", encoding="utf-8")
(L.SKILLS_DIR / "plain").mkdir()
(L.SKILLS_DIR / "plain" / "SKILL.md").write_text("# 无 frontmatter 的技能\n直接执行即可\n",
                                                 encoding="utf-8")

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


# ============ 1. 扫描与 frontmatter ============
print("== 1. 扫描与 frontmatter ==")
skills = L._scan_skills()
check("扫描到 2 个技能", len(skills) == 2, str([s["name"] for s in skills]))
cr = next((s for s in skills if s["name"] == "code-review"), None)
check("frontmatter 解析 name/description",
      cr is not None and cr["description"].startswith("代码审查流程"), str(cr)[:120])
plain = next((s for s in skills if s["name"] == "plain"), None)
check("无 frontmatter 回退目录名", plain is not None and not plain["description"], str(plain))
cat = L._skill_catalog_text()
check("清单含技能名与描述", "code-review" in cat and "代码审查流程" in cat, cat)
check("清单不含全文（惰性）", "边界条件" not in cat, cat[:120])

# ============ 2. load_skill 工具 ============
print("== 2. load_skill 工具 ==")
ok, res = L._execute_tool("load_skill", json.dumps({"name": "code-review"}))
check("加载技能全文", ok and "边界条件" in res, res[:80])
ok, res = L._execute_tool("load_skill", json.dumps({"name": "不存在的"}))
check("未知技能报错并列出可用", not ok and "code-review" in res, res[:80])
ok, res = L._execute_tool("load_skill", json.dumps({"name": "plain"}))
check("无 frontmatter 技能可加载", ok and "直接执行" in res, res[:80])

# ============ 3. system_status ============
print("== 3. system_status ==")
ok, res = L._execute_tool("system_status", "{}")
check("返回磁盘信息", ok and "磁盘" in res, res[:100])
if sys.platform.startswith("linux"):
    check("返回 CPU 负载（Linux）", "CPU" in res, res[:100])
else:
    check("非 Linux 优雅降级（无 CPU 行不报错）", ok and "磁盘" in res, res[:100])

# ============ 4. 确认策略与隔离 ============
print("== 4. 确认策略与隔离 ==")
check("load_skill 免确认", L._confirm_policy("load_skill", {}) == "allow", "")
check("system_status 免确认", L._confirm_policy("system_status", {}) == "allow", "")
L.ISOLATED = True
names = [t["function"]["name"] for t in L._agent_tools()]
L.ISOLATED = False
check("隔离模式保留新工具", "load_skill" in names and "system_status" in names, "")
check("工具集已注册", "load_skill" in [t["function"]["name"] for t in L.AGENT_TOOLS]
      and "system_status" in [t["function"]["name"] for t in L.AGENT_TOOLS], "")

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
