"""本地 mock OpenAI 兼容接口，用于端到端测试 llm_server（端口 8999）。

模拟场景：
- 正常回复（Key 需含 good-key）
- Agent 工具循环：第 1 轮要求调 get_screen_size，第 2 轮要求调 click，之后正常回复
- reasoner 模型：content 为空 + reasoning_content 有内容（DeepSeek-R1 行为）
- 未知模型：400 + error.message（如 DeepSeek 的 "Model Not Exist"）
- 无效 Key：401
"""
import uvicorn
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

app = FastAPI()


class ChatReq(BaseModel):
    model: str
    messages: list[dict]
    temperature: float = 0.7
    tools: list | None = None


@app.post("/v1/chat/completions")
async def completions(req: ChatReq, request: Request, response: Response):
    # 调试：记录收到的请求（model + tool 消息数）
    with open("mock_requests.log", "a", encoding="utf-8") as f:
        n_tool = sum(1 for m in req.messages if m.get("role") == "tool")
        f.write(f"model={req.model!r} tools={bool(req.tools)} tool_msgs={n_tool}\n")
    auth = request.headers.get("authorization", "")
    if "good-key" not in auth:
        return Response(status_code=401,
                        content='{"error": {"message": "Authentication Fails", "type": "auth"}}',
                        media_type="application/json")

    # Agent 工具循环模拟：按已有 tool 结果消息数决定行为
    if req.tools:
        tool_msgs = [m for m in req.messages if m.get("role") == "tool"]
        if req.model == "loop-model":
            # 死循环模拟：永远要求调用 get_screen_size（测试熔断器/轮数上限）
            return {
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{len(tool_msgs) + 1}", "type": "function",
                     "function": {"name": "get_screen_size", "arguments": "{}"}},
                ]}}],
                "usage": {"total_tokens": 10},
            }
        if len(tool_msgs) == 0:
            return {
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "get_screen_size", "arguments": "{}"}},
                ]}}],
                "usage": {"total_tokens": 10},
            }
        if len(tool_msgs) == 1:
            return {
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_2", "type": "function",
                     "function": {"name": "click", "arguments": "{\"x\": 100, \"y\": 200}"}},
                ]}}],
                "usage": {"total_tokens": 10},
            }
        if len(tool_msgs) == 2:
            # 第三轮：观察到了工具结果，给出最终回复
            screen_ok = "成功" if "width" in tool_msgs[0].get("content", "") else "失败"
            click_ok = "成功" if "ok" in tool_msgs[1].get("content", "") else "失败"
            return {
                "choices": [{"message": {"role": "assistant",
                                         "content": f"[mock] 工具循环完成：屏幕尺寸获取{screen_ok}，"
                                                    f"点击执行{click_ok}。"}}],
                "usage": {"total_tokens": 20},
            }

    if "reasoner" in req.model.lower():
        return {
            "choices": [{"message": {"role": "assistant", "content": "",
                                     "reasoning_content": "我在推理……"}}],
            "usage": {"total_tokens": 99},
        }
    if req.model == "not-exist-model":
        return Response(status_code=400,
                        content='{"error": {"message": "Model Not Exist", "type": "invalid_request_error"}}',
                        media_type="application/json")
    last = req.messages[-1]["content"]
    return {
        "choices": [{"message": {"role": "assistant",
                                 "content": f"[mock] 收到 {len(req.messages)} 条消息，模型 {req.model}，你说：{last[:50]}"}}],
        "usage": {"total_tokens": 42},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8999, log_level="warning")
