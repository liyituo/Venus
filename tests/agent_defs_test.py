"""子 agent 定义文件结构测试：JSON 解析 / name 唯一 / tools 非空且格式合法。

不依赖 MCP 连接（MCP 工具存在性在真实环境验证）；CI 可跑。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
files = sorted(AGENTS_DIR.glob("*.json"))

print("== 子 agent 定义结构 ==")
check("存在定义文件", len(files) >= 1, str([f.name for f in files]))

specs = []
names = set()
for f in files:
    try:
        spec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        check(f"{f.name} JSON 解析", False, str(exc))
        continue
    specs.append(spec)
    check(f"{f.name}: name", isinstance(spec.get("name"), str) and spec["name"], str(spec)[:80])
    check(f"{f.name}: description", isinstance(spec.get("description"), str) and spec["description"], "")
    tools = spec.get("tools")
    check(f"{f.name}: tools 非空列表", isinstance(tools, list) and len(tools) > 0, str(tools))
    if isinstance(tools, list):
        bad = [t for t in tools if not isinstance(t, str) or not t.strip()]
        check(f"{f.name}: tools 全为合法字符串", not bad, str(bad))
    check(f"{f.name}: system_prompt 存在", isinstance(spec.get("system_prompt"), str)
          and len(spec["system_prompt"]) > 20, "")
    if spec.get("name"):
        names.add(spec["name"])

check("name 唯一", len(names) == len(specs), str(names))

# 本地工具白名单存在性（MCP 工具在真实环境验证）
local_names = {t["function"]["name"] for t in L.AGENT_TOOLS}
for spec in specs:
    missing = [t for t in (spec.get("tools") or [])
               if not t.startswith("mcp_") and t not in local_names]
    check(f"{spec['name']}: 本地白名单工具均存在", not missing, str(missing))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
