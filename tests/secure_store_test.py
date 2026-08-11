"""日志/剪贴板/密钥安全测试：安全存储 roundtrip、占位符迁移、损坏恢复、日志脱敏。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as daemon_mod        # noqa: E402
import chat as chat_mod         # noqa: E402
import llm_server as L          # noqa: E402
import secure_store as SS       # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ============ 1. 安全存储 roundtrip（重定向存储文件到临时目录）============
print("== 1. secure_store roundtrip ==")
tmpdir = tempfile.mkdtemp(prefix="pcagent_sec_")
SS._secrets_path = lambda: Path(tmpdir) / "secrets.json"
SS._secrets = {}
SS._loaded = False

SS.store("api_key", "sk-secret-123")
back = SS.load("api_key")
check("store/load roundtrip", back == "sk-secret-123", back)
check("存储为密文（非明文）", "sk-secret-123" not in Path(tmpdir, "secrets.json").read_text(encoding="utf-8"),
      Path(tmpdir, "secrets.json").read_text(encoding="utf-8")[:80])

SS.store("api_key", "")
check("空值删除", SS.load("api_key") == "", "")

# ============ 2. 占位符与迁移 ============
print("== 2. 占位符 / 迁移 ==")
SS._secrets = {}
SS._loaded = False
SS.store("api_key", "plain-migrate-me")
cfg = SS.migrate_from_plaintext({"api_key": "plain-migrate-me", "model": "x"}, ("api_key",))
check("迁移后配置为占位符", cfg["api_key"] == "__secure__", str(cfg))
check("迁移后可读回", SS.load("api_key") == "plain-migrate-me", "")
check("空值不迁移", SS.migrate_from_plaintext({"api_key": ""}, ("api_key",))["api_key"] == "", "")
check("占位符不重复迁移", SS.migrate_from_plaintext({"api_key": "__secure__"}, ("api_key",))["api_key"] == "__secure__", "")

# llm_server load_config 占位符读取
SS._secrets = {}
SS._loaded = False
SS.store("api_key", "sk-via-llm")
cfg_file = Path(tmpdir) / "chat_config.json"
cfg_file.write_text(json.dumps({"api_key": "__secure__", "model": "m"}), encoding="utf-8")
orig_path = L.CONFIG_PATH
L.CONFIG_PATH = cfg_file
cfg = L.load_config()
check("llm_server 占位符读取真实 Key", cfg.get("api_key") == "sk-via-llm", cfg.get("api_key", ""))
L.CONFIG_PATH = orig_path

# ============ 3. 损坏文件恢复（重命名不静默清空）============
print("== 3. 损坏恢复 ==")
SS._secrets = {}
SS._loaded = False
bad = Path(tmpdir) / "secrets.json"
bad.write_text("{not-json!!!", encoding="utf-8")
SS._load()
check("损坏不崩溃", SS._load_warning != "", SS._load_warning)
corrupts = [p.name for p in Path(tmpdir).glob("secrets.json.corrupt-*")]
check("损坏文件已重命名备份", len(corrupts) == 1, str(corrupts))
SS._secrets = {}
SS._loaded = False

# ============ 4. app.py 动作日志脱敏 ============
print("== 4. 动作日志脱敏 ==")
req = daemon_mod.ActionRequest(action="type_text", text="这是要输入的敏感内容")
meta = daemon_mod._safe_action_log(req)
check("type_text 只记字符数", meta == {"action": "type_text", "chars": len("这是要输入的敏感内容")}
      and "敏感" not in json.dumps(meta), str(meta))
req2 = daemon_mod.ActionRequest(action="click", x=100, y=200, clicks=2)
meta2 = daemon_mod._safe_action_log(req2)
check("click 记坐标", meta2 == {"action": "click", "position": [100, 200], "clicks": 2, "button": "left"},
      str(meta2))
req3 = daemon_mod.ActionRequest(action="press_key", key="ctrl+c")
meta3 = daemon_mod._safe_action_log(req3)
check("press_key 记按键", meta3 == {"action": "press_key", "key": "ctrl+c"}, str(meta3))

# ============ 5. chat 参数日志脱敏 ============
print("== 5. 工具参数日志脱敏 ==")
red = chat_mod._redact_args(json.dumps({"text": "secret text", "x": 10}))
check("type_text 内容不出现", "secret" not in red and "<11字>" in red, red)
red2 = chat_mod._redact_args(json.dumps({"command": "rm -rf /etc", "cwd": "."}))
check("command 内容不出现", "rm" not in red2 and "<11字>" in red2, red2)
red3 = chat_mod._redact_args(json.dumps({"file": "a.py", "occurrence": 2}))
check("非敏感参数正常显示", "a.py" in red3 and "occurrence=2" in red3, red3)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
