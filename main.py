#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import asyncio
import aiohttp
from aiohttp import web
import datetime
import math
import websockets
import os
from urllib.parse import quote

# ==================== 配置区域 ====================
# Bark 设备码
BARK_KEYS = [
    "rcuMeBUdF5XA6852tt9ZcX",
]

# 最小震级，低于此值不推送
MIN_MAGNITUDE = 2.0

# 本地烈度阈值：估算翠屏区烈度 >= 此值就推送
MIN_LOCAL_INTENSITY = 1

# 数据源：中国地震台网预警 (如果你想多报更频繁，可改为 wss://ws-api.wolfx.jp/all_eew)
WS_URL = "wss://ws-api.wolfx.jp/cenc_eew"

# Bark 推送级别：critical 为重要警告，忽略静音和勿扰模式
PUSH_LEVEL = "critical"

# 翠屏区中心坐标（宜宾市翠屏区）
CUI_PING_LAT = 28.77
CUI_PING_LON = 104.62

# 去重缓存大小
RECENT_PUSH_LIMIT = 200
# =================================================

recent_push_cache = set()
recent_push_order = []

def get_time():
    return datetime.datetime.now().strftime("%H:%M:%S")

def log(msg):
    print(f"[{get_time()}] {msg}", flush=True)

def safe_get(data, key, default="未知"):
    value = data.get(key, default)
    return default if value in (None, "") else value

def add_recent_cache(item):
    if item in recent_push_cache:
        return False
    recent_push_cache.add(item)
    recent_push_order.append(item)
    if len(recent_push_order) > RECENT_PUSH_LIMIT:
        old = recent_push_order.pop(0)
        recent_push_cache.discard(old)
    return True

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def estimate_intensity(mag, epicentral_distance_km, depth_km):
    hypocentral_distance = math.sqrt(epicentral_distance_km ** 2 + depth_km ** 2)
    if hypocentral_distance < 1:
        hypocentral_distance = 1
    return 1.5 * mag - 3.5 * math.log10(hypocentral_distance) + 3.0

def calc_local_info(data):
    lat = data.get("Latitude")
    lon = data.get("Longitude")
    depth = data.get("Depth", 10)
    mag_str = data.get("Magnitude", "0")
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
        depth = float(depth) if depth else 10.0
        mag = float(mag_str)
    except (ValueError, TypeError):
        return None
    distance = haversine(lat, lon, CUI_PING_LAT, CUI_PING_LON)
    intensity = estimate_intensity(mag, distance, depth)
    return distance, intensity, lat, lon, depth

def estimate_arrival_time(distance_km, origin_time_str):
    """估算S波到达时间，并减去发震后流逝的时间"""
    s_wave_speed = 3.5
    travel_time = distance_km / s_wave_speed
    
    try:
        # 解析发震时刻（兼容有无毫秒的格式）
        if "." in origin_time_str:
            origin_time = datetime.datetime.strptime(origin_time_str, "%Y-%m-%d %H:%M:%S.%f")
        else:
            origin_time = datetime.datetime.strptime(origin_time_str, "%Y-%m-%d %H:%M:%S")
            
        now = datetime.datetime.now()
        elapsed_seconds = (now - origin_time).total_seconds()
        
        # 剩余时间 = 传播时间 - 已流逝时间
        remaining_time = travel_time - elapsed_seconds
        return max(0, int(remaining_time))
    except Exception as e:
        log(f"倒计时时间计算错误: {e}")
        return max(0, int(travel_time))

def should_push(data):
    mag_str = data.get("Magnitude", "0")
    try:
        mag = float(mag_str)
    except (ValueError, TypeError):
        mag = 0.0
    if mag < MIN_MAGNITUDE:
        return False
    if data.get("Latitude") is None or data.get("Longitude") is None:
        hypo = str(data.get("HypoCenter", ""))
        return "翠屏" in hypo or "宜宾" in hypo
    info = calc_local_info(data)
    if info is None:
        return False
    _, intensity, _, _, _ = info
    return intensity >= MIN_LOCAL_INTENSITY

async def bark_fetch(session, key, title, subtitle, body, level):
    encoded_title = quote(title)
    encoded_subtitle = quote(subtitle)
    encoded_body = quote(body)
    encoded_group = quote("地震预警")
    
    # 自定义铃声编码
    encoded_sound = quote("音频截取_爱给网_aigei_com")

    url = (
        f"https://api.day.app/{key}/"
        f"{encoded_title}/{encoded_subtitle}/{encoded_body}"
        f"?level={level}&group={encoded_group}&sound={encoded_sound}&call=1"
    )
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
        return await response.text()

async def push(session, title, subtitle, body, dedup_key=None, level=PUSH_LEVEL):
    unique_key = dedup_key or f"{title}|{subtitle}|{body}"
    if not add_recent_cache(unique_key):
        log(f"[跳过重复] {title} | {subtitle}")
        return
    tasks = [asyncio.create_task(bark_fetch(session, key, title, subtitle, body, level)) for key in BARK_KEYS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            log(f"[Bark失败] key_index={i} error={result}")
        else:
            log(f"[Bark成功] key_index={i} {title} | {subtitle}")

def format_content(data, source_name):
    report_num = safe_get(data, "ReportNum")
    origin_time = safe_get(data, "OriginTime")
    update_time = safe_get(data, "UpdateTime", "")
    hypo = safe_get(data, "HypoCenter")
    mag = safe_get(data, "Magnitude")
    depth = safe_get(data, "Depth", "未知")
    max_intensity = safe_get(data, "MaxIntensity", "未知")

    title = f"地震预警 · M{mag} (第{report_num}报)"
    subtitle = f"{hypo}"
    
    lines = [
        f"数据来源：{source_name}",
        f"发震时刻：{origin_time}",
        f"震中位置：{hypo}",
        f"震级：M{mag}",
        f"震源深度：{depth} km",
        f"最大烈度：{max_intensity} 度"
    ]
    
    info = calc_local_info(data)
    if info is not None:
        distance, intensity, lat, lon, _ = info
        countdown = estimate_arrival_time(distance, origin_time)
        
        if countdown > 0:
            lines.append(f"预警倒计时：预计 {countdown} 秒后到达")
        else:
            lines.append(f"预警状态：地震波已到达或已过境")
            
        lines.append(f"震中距翠屏区：{distance:.1f} km")
        lines.append(f"预估本地烈度：{intensity:.1f} 度")
        lines.append(f"震中坐标：{lat:.3f}, {lon:.3f}")
    else:
        lines.append("震中距翠屏区：未知（缺经纬度）")
        
    if update_time:
        lines.append(f"更新时间：{update_time}")
    lines.append(f"推送时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    body = "\n".join(lines)
    # 去重逻辑包含 report_num，保证第2报、第3报不会被吞掉
    dedup_key = f"{source_name}|{report_num}|{origin_time}|{hypo}|{mag}|{max_intensity}"
    return title, subtitle, body, dedup_key

def build_message(data):
    msg_type = data.get("type")
    if msg_type in ["cenc_eew", "sc_eew", "fj_eew"]:
        source_map = {"cenc_eew": "中国地震台网预警", "sc_eew": "四川地震局预警", "fj_eew": "福建地震局预警"}
        return format_content(data, source_map.get(msg_type, "地震预警"))
    return None

async def websocket_client(session):
    reconnect_delay = 1
    max_delay = 30
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10, close_timeout=5, max_size=2 ** 20) as websocket:
                log("已连接到 Wolfx WebSocket API")
                reconnect_delay = 1
                async for raw in websocket:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msg_type = data.get("type")
                    if msg_type == "heartbeat":
                        log("收到心跳包")
                        continue
                    if not should_push(data):
                        continue
                    message = build_message(data)
                    if not message:
                        continue
                    title, subtitle, body, dedup_key = message
                    log(f"准备推送: {title} | {subtitle}")
                    await push(session, title, subtitle, body, dedup_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"WebSocket错误: {e}, {reconnect_delay}s后重连")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

# ================= Cloady/Koyeb 等平台必需的健康检查服务 =================
async def health_check(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/healthz', health_check)
    app.router.add_get('/', health_check) # 根路径也返回OK，防止部分平台检测失败
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log(f"健康检查服务已启动，监听端口 {port}")

async def main():
    timeout = aiohttp.ClientTimeout(total=15)
    connector = aiohttp.TCPConnector(limit=20)
    
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # 同时运行 Web 服务器和 WebSocket 客户端
        await asyncio.gather(
            start_web_server(),
            websocket_client(session)
        )

if __name__ == "__main__":
    asyncio.run(main())
