"""工作区与文件工具测试：delete/move/copy/rename、undo 增强、read_file 分页、工作区切换隔离。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


_TMP = tempfile.mkdtemp(prefix="pcagent_fs_")
WS = Path(_TMP)
_real_get_ws = L._get_workspace
L._get_workspace = lambda: WS
L._todos = []
L._todos_loaded = True
L._agent_modified_files = set()
L._pending_git_snapshot = ""
import shutil as _sh
_sh.rmtree(L._backup_dir(), ignore_errors=True)
L._backup_index()


def run(name, arguments: str):
    return L._execute_tool(name, arguments)


def ok_json(result: str) -> dict:
    return json.loads(result)


# ============ 1. read_file 分页 ============
print("== 1. read_file 分页 ==")
(WS / "big.txt").write_text("\n".join(f"line{i}" for i in range(1, 101)), encoding="utf-8")
ok, res = run("read_file", json.dumps({"path": "big.txt", "start_line": 10, "end_line": 12}))
check("分页读取成功", ok and "共 100 行" in res and "返回第 10-12 行" in res, res[:120])
check("分页内容正确", ok and "line10" in res and "line11" in res and "line12" in res, res[:200])
check("分页不含范围外", ok and "line9" not in res and "line13" not in res, "")
ok, res = run("read_file", json.dumps({"path": "big.txt"}))
check("无参数返回全文", ok and "line1" in res and "line100" in res, "")
ok, res = run("read_file", json.dumps({"path": "big.txt", "start_line": 95}))
check("仅起始行", ok and "line95" in res and "line100" in res, res[:100])
ok, res = run("read_file", json.dumps({"path": "big.txt", "start_line": 200}))
check("起始行超界钳制", ok and "返回第 100-100 行" in res, res[:100])

# ============ 2. 新文件工具 ============
print("== 2. delete/move/copy/rename ==")
(WS / "src1.txt").write_text("内容A\n", encoding="utf-8")
(WS / "dst1.txt").write_text("旧目标\n", encoding="utf-8")

ok, res = run("copy_file", json.dumps({"src": "src1.txt", "dst": "dst1.txt"}))
check("copy_file 覆盖（备份目标）", ok and (WS / "dst1.txt").read_text(encoding="utf-8") == "内容A\n", res)
ok, res = run("undo", json.dumps({"file": "dst1.txt"}))
check("copy 覆盖可 undo 恢复目标", ok and (WS / "dst1.txt").read_text(encoding="utf-8") == "旧目标\n", res)

ok, res = run("move_file", json.dumps({"src": "src1.txt", "dst": "sub/moved.txt"}))
check("move_file 成功", ok and not (WS / "src1.txt").exists()
      and (WS / "sub" / "moved.txt").read_text(encoding="utf-8") == "内容A\n", res)
ok, res = run("undo", "{}")
check("move 可 undo 移回", ok and (WS / "src1.txt").read_text(encoding="utf-8") == "内容A\n"
      and not (WS / "sub" / "moved.txt").exists(), res[:120])

ok, res = run("rename_file", json.dumps({"src": "src1.txt", "dst": "renamed.txt"}))
check("rename_file 成功", ok and (WS / "renamed.txt").exists(), res)
ok, res = run("undo", "{}")
check("rename 可 undo", ok and (WS / "src1.txt").exists(), res)

ok, res = run("delete_file", json.dumps({"path": "src1.txt"}))
check("delete_file 成功", ok and not (WS / "src1.txt").exists(), res)
ok, res = run("undo", "{}")
check("delete 可 undo 恢复", ok and (WS / "src1.txt").read_text(encoding="utf-8") == "内容A\n", res)

# 删除不存在的文件
ok, res = run("delete_file", json.dumps({"path": "nope.txt"}))
check("删除不存在拒绝", not ok, res)

# ============ 3. 目录操作 ============
print("== 3. 目录 ==")
ok, res = run("create_folder", json.dumps({"path": "newdir"}))
check("create_folder 成功", ok and (WS / "newdir").is_dir(), res)
ok, res = run("undo", "{}")
check("新建目录可 undo", ok and not (WS / "newdir").exists(), res)

(WS / "full").mkdir()
(WS / "full" / "f.txt").write_text("x", encoding="utf-8")
ok, res = run("delete_folder", json.dumps({"path": "full"}))
check("非空目录拒绝删除", not ok and "非空" in res, res)
(WS / "full" / "f.txt").unlink()
ok, res = run("delete_folder", json.dumps({"path": "full"}))
check("空目录删除成功", ok and not (WS / "full").exists(), res)
ok, res = run("undo", "{}")
check("删除目录可 undo 恢复", ok and (WS / "full").is_dir(), res)

# ============ 4. 新建文件 undo（删除新建）============
print("== 4. 新建 undo ==")
ok, res = run("create_file", json.dumps({"path": "fresh.py", "content": "print(1)"}))
check("create_file 新建", ok and (WS / "fresh.py").exists(), res)
ok, res = run("undo", "{}")
check("新建文件可 undo 删除", ok and not (WS / "fresh.py").exists(), res)

# ============ 5. 备份失败阻止高风险修改 ============
print("== 5. 备份失败中止 ==")
(WS / "prot.txt").write_text("v1\n", encoding="utf-8")
_orig_tb = L._take_backup
L._take_backup = lambda *a, **k: False      # 模拟备份系统不可用
ok, res = run("replace_text", json.dumps({"file": "prot.txt", "old": "v1", "new": "v2"}))
L._take_backup = _orig_tb
check("备份失败中止修改", not ok and "备份失败" in res, res[:120])
check("文件未被修改", (WS / "prot.txt").read_text(encoding="utf-8") == "v1\n", "")
L._take_backup = lambda *a, **k: False
ok, res = run("delete_file", json.dumps({"path": "prot.txt"}))
L._take_backup = _orig_tb
check("备份失败中止删除", not ok and (WS / "prot.txt").exists(), res[:120])

# ============ 6. 工作区切换 ============
print("== 6. 工作区切换隔离 ==")
ws2 = Path(tempfile.mkdtemp(prefix="pcagent_ws2_"))
L._get_workspace = _real_get_ws            # 恢复真实函数验证切换逻辑
L._workspace_lock = L._workspace_lock
epoch0 = L._workspace_epoch
with L._workspace_lock:
    L._workspace_path = ws2
    L._workspace_epoch += 1
check("切换后 epoch 变化", L._workspace_epoch > epoch0, "")
check("旧 epoch 检测到变化", L._workspace_changed(epoch0) is True, "")
check("新 epoch 一致", L._workspace_changed(L._workspace_epoch) is False, "")
check("_get_workspace 返回新工作区", L._get_workspace() == ws2, str(L._get_workspace()))
check("备份目录跟随工作区", str(L._backup_dir()).startswith(str(ws2)), str(L._backup_dir()))
# 切回原工作区（后续文件操作测试继续在 WS 内进行）
with L._workspace_lock:
    L._workspace_path = WS
check("工作区已切回", L._get_workspace() == WS, "")

# ============ 7. 二进制 round-trip ============
print("== 7. 二进制备份 / 撤销 ==")
import struct
bin_data = bytes(range(256)) * 4 + struct.pack(">II", 0xDEAD, 0xBEEF)   # 全字节值 + 魔数
(WS / "blob.bin").write_bytes(bin_data)
ok, res = run("replace_text", json.dumps({"file": "blob.bin", "old": "x", "new": "y"}))
check("二进制 replace 正常执行", ok, res[:80])
ok, res = run("undo", json.dumps({"file": "blob.bin"}))
check("二进制 undo 成功", ok, res[:120])
check("二进制无损恢复", (WS / "blob.bin").read_bytes() == bin_data, "")

# 二进制删除/恢复
(WS / "img.png").write_bytes(bin_data)
ok, res = run("delete_file", json.dumps({"path": "img.png"}))
check("二进制删除", ok and not (WS / "img.png").exists(), res[:80])
ok, res = run("undo", "{}")
check("二进制删除可恢复", ok and (WS / "img.png").read_bytes() == bin_data, "")

# ============ 8. 覆盖移动撤销（双边状态）============
print("== 8. 覆盖移动撤销 ==")
(WS / "srcA.txt").write_text("源A内容\n", encoding="utf-8")
(WS / "dstB.txt").write_text("目标B原内容\n", encoding="utf-8")
ok, res = run("move_file", json.dumps({"src": "srcA.txt", "dst": "dstB.txt"}))
check("覆盖移动成功", ok and (WS / "dstB.txt").read_text(encoding="utf-8") == "源A内容\n", res[:100])
check("源已移动", not (WS / "srcA.txt").exists(), "")
ok, res = run("undo", "{}")
check("覆盖移动 undo 成功", ok, res[:150])
check("源恢复", (WS / "srcA.txt").read_text(encoding="utf-8") == "源A内容\n", "")
check("目标原内容恢复（双边）",
      (WS / "dstB.txt").read_text(encoding="utf-8") == "目标B原内容\n",
      str((WS / "dstB.txt").read_text(encoding="utf-8")))

# ============ 9. 覆盖复制撤销 ============
print("== 9. 覆盖复制撤销 ==")
(WS / "srcC.txt").write_text("源C\n", encoding="utf-8")
(WS / "dstD.txt").write_text("目标D原内容\n", encoding="utf-8")
ok, res = run("copy_file", json.dumps({"src": "srcC.txt", "dst": "dstD.txt"}))
d = ok_json(res)
check("覆盖复制成功", ok and (WS / "dstD.txt").read_text(encoding="utf-8") == "源C\n", res[:100])
check("backup 字段反映复制前目标存在", d.get("backup") is True, str(d))
ok, res = run("undo", "{}")
check("覆盖复制 undo 恢复目标",
      ok and (WS / "dstD.txt").read_text(encoding="utf-8") == "目标D原内容\n", res[:120])
check("复制不删源", (WS / "srcC.txt").read_text(encoding="utf-8") == "源C\n", "")

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
