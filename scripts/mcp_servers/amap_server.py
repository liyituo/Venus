"""高德地图 MCP server：POI 搜索 / 周边 / 地理编码 / 路线规划 / IP 定位。

纯 HTTP 调用高德 Web 服务 API（国内直连，无需代理），key 从环境变量
AMAP_MAPS_API_KEY 读取（配在 mcp_config.json 的 env 中，不入库）。
全部为只读查询——接入方（llm_server）按 mcp_amap_ 前缀免确认。
"""
import json
import os
import urllib.parse
import urllib.request

from fastmcp import FastMCP

mcp = FastMCP("amap")

KEY = os.environ.get("AMAP_MAPS_API_KEY", "")
BASE = "https://restapi.amap.com/v3"


def _get(path: str, params: dict) -> str:
    if not KEY:
        return "未配置 AMAP_MAPS_API_KEY（mcp_config.json 的 env 中设置）"
    query = urllib.parse.urlencode({**params, "key": KEY})
    with urllib.request.urlopen(f"{BASE}{path}?{query}", timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("status") != "1":
        return f"高德 API 错误：{data.get('info', '?')}"
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def poi_search(keyword: str, city: str = "", page: int = 1) -> str:
    """按关键字搜索地点（餐厅/加油站/地铁站/银行等）。city 可省略（如 "北京"）。"""
    return _get("/place/text", {"keywords": keyword, "city": city, "offset": 10, "page": page})


@mcp.tool()
def poi_around(longitude: float, latitude: float, keyword: str = "", radius: int = 3000) -> str:
    """在指定经纬度周边搜索地点（半径单位米，默认 3000）。"""
    return _get("/place/around",
                {"location": f"{longitude},{latitude}", "keywords": keyword,
                 "radius": radius, "offset": 10})


@mcp.tool()
def geocode(address: str, city: str = "") -> str:
    """地址转经纬度（如 "北京市朝阳区望京SOHO"）。"""
    return _get("/geocode/geo", {"address": address, "city": city})


@mcp.tool()
def regeocode(longitude: float, latitude: float) -> str:
    """经纬度转地址（逆地理编码）。"""
    return _get("/geocode/regeo", {"location": f"{longitude},{latitude}"})


@mcp.tool()
def direction(origin: str, destination: str, mode: str = "driving") -> str:
    """路线规划。mode: driving 驾车 / walking 步行 / transit 公交 / bicycling 骑行。
    origin/destination 为 '经度,纬度'（如 "116.397,39.908"）或地名（内部先地理编码）。"""
    if mode not in ("driving", "walking", "transit", "bicycling"):
        return f"mode 无效：{mode}（driving/walking/transit/bicycling）"
    return _get(f"/direction/{mode}", {"origin": origin, "destination": destination})


@mcp.tool()
def ip_location(ip: str = "") -> str:
    """IP 定位（ip 留空用本机出口 IP），返回城市级位置。"""
    return _get("/ip", {"ip": ip})


if __name__ == "__main__":
    mcp.run()
