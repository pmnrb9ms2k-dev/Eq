#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
地震预警 Bark 推送
数据源：中国地震台网（Wolfx API）
关注地：四川省宜宾市翠屏区
规则：估算本地烈度 >= 1 度即推送
"""

import json
import asyncio
import aiohttp
import datetime
import math
import websockets
from urllib.parse import quote

# ==================== 配置区域 ====================
BARK_KEYS = [
    "rcuMeBUdF5XA6852tt9ZcX",
]

# 最小震级，低于此值不推送
MIN_MAGNITUDE = 2.0

# 本地烈度阈值：估算翠屏区烈度 >= 此值就推送
MIN_LOCAL_INTENSITY = 1

# 数据源：中国地震台网预警
WS_URL = "wss://ws-api.wolfx.jp/cenc_eew"

# Bark 推送级别：critical 为重要警告，忽略静音和勿扰模式
PUSH_LEVEL = "critical"

# 翠屏区中心坐标（宜宾市翠屏区）
CUI_PING_LAT = 28.79
CUI_PING_LON = 104.61

# 去重缓存大小
RECENT_PUSH_LIMIT = 200
# =================================================


recent_push_cache = set()
recent_push_order = []


def get_time():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{get_time()}] {msg}")


def safe_get(data, key, default="未知"):
    value = data.get(key, default)
    return default if value in (None, "") else value


def add_recent_cache(item):
    """返回 True 表示新内容，False 表示重复"""
    if item in recent_push_cache:
        return False
    recent_push_cache.add(item)
    recent_push_order.append(item)
    if len(recent_push_order) > RECENT_PUSH_LIMIT:
        old = recent_push_order.pop(0)
        recent_push_cache.discard(old)
    return True


def haversine(lat1, lon1, lat2, lon2):
    """计算两点间距离（公里）"""
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def estimate_intensity(mag, epicentral_distance_km, depth_km):
    """
    简化烈度衰减公式，估算本地烈度（仅供参考）
    I = 1.5M - 3.5log10(震源距) + 3.0
    """
    hypocentral_distance = math.sqrt(epicentral_distance_km ** 2 + depth_km ** 2)
    if hypocentral_distance < 1:
        hypocentral_distance = 1
    intensity = 1.5 * mag - 3.5 * math.log10(hypocentral_distance) + 3.0
    return intensity


def calc_local_info(data):
    """计算震中距与本地烈度，返回 (distance, intensity, lat, lon, depth)"""
    lat = data.get("Latitude")
    lon = data.get("Longitude")
    depth = data.get("Depth", 10)
    mag_str = data.get("Magnitude", "0")

    if lat is None or lon is None:
        return None

    try:
        lat = float(lat)
        lon = float(lon)
        depth = float(depth) if depth else 10.0
        mag = float(mag_str)
    except (ValueError, TypeError):
        return None

    distance = haversine(lat, lon, CUI_PING_LAT, CUI_PING_LON)
    intensity = estimate_intensity(mag, distance, depth)
    return distance, intensity, lat, lon, depth


def should_push(data):
    """根据震级、距离、估算本地烈度判断是否推送"""
    mag_str = data.get("Magnitude", "0")
    try:
        mag = float(mag_str)
    except (ValueError, TypeError):
        mag = 0.0

    if mag < MIN_MAGNITUDE:
        return False

    # 如果没有经纬度，回退到名称匹配
    if data.get("Latitude") is None or data.get("Longitude") is None:
        hypo = str(data.get("HypoCenter", ""))
        return "翠屏" in hypo or "宜宾" in hypo

    info = calc_local_info(data)
    if info is None:
        return False

    _, intensity, _, _, _ = info
    return intensity >= MIN_LOCAL_INTENSITY


async def bark_fetch(session, key, title, subtitle, body, level):
    """调用 Bark API 三段式推送：标题 / 副标题 / 正文"""
    encoded_title = quote(title)
    encoded_subtitle = quote(subtitle)
    encoded_body = quote(body)
    encoded_group = quote("地震预警")

    url = (
        f"https://api.day.app/{key}/"
        f"{encoded_title}/{encoded_subtitle}/{encoded_body}"
        f"?level={level}&group={encoded_group}&sound=音频截取_爱给网_aigei_com"
    )

    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
        return await response.text()


async def push(session, title, subtitle, body, dedup_key=None, level=PUSH_LEVEL):
    """向所有 Bark 设备并发推送"""
    unique_key = dedup_key or f"{title}|{subtitle}|{body}"

    if not add_recent_cache(unique_key):
        log(f"[跳过重复] {title} | {subtitle}")
        return

    tasks = [
        asyncio.create_task(bark_fetch(session, key, title, subtitle, body, level))
        for key in BARK_KEYS
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            log(f"[Bark失败] key_index={i} error={result}")
        else:
            log(f"[Bark成功] key_index={i} {title} | {subtitle}")


def format_content(data, source_name):
    """统一构造推送内容（无图标）"""
    report_num = safe_get(data, "ReportNum")
    origin_time = safe_get(data, "OriginTime")
    update_time = safe_get(data, "UpdateTime", "")
    hypo = safe_get(data, "HypoCenter")
    mag = safe_get(data, "Magnitude")
    depth = safe_get(data, "Depth", "未知")
    max_intensity = safe_get(data, "MaxIntensity", "未知")

    # 标题
    title = f"地震预警 · M{mag}"

    # 副标题：震中位置 + 报告编号
    subtitle = f"{hypo} 第{report_num}报"

    # 正文：逐行拼接详细信息
    lines = []
    lines.append(f"数据来源：{source_name}")
    lines.append(f"发震时刻：{origin_time}")
    lines.append(f"震中位置：{hypo}")
    lines.append(f"震级：M{mag}")
    lines.append(f"震源深度：{depth} km")
    lines.append(f"最大烈度：{max_intensity} 度")

    # 本地影响
    info = calc_local_info(data)
    if info is not None:
        distance, intensity, lat, lon, _ = info
        lines.append(f"震中距翠屏区：{distance:.1f} km")
        lines.append(f"预估本地烈度：{intensity:.1f} 度")
        lines.append(f"震中坐标：{lat:.3f}, {lon:.3f}")
    else:
        lines.append("震中距翠屏区：未知（缺经纬度）")

    if update_time:
        lines.append(f"更新时间：{update_time}")

    lines.append(f"推送时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    body = "\n".join(lines)

    dedup_key = f"{source_name}|{report_num}|{origin_time}|{hypo}|{mag}|{max_intensity}"
    return title, subtitle, body, dedup_key


def build_message(data):
    """根据数据类型生成推送内容"""
    msg_type = data.get("type")

    if msg_type == "cenc_eew":
        return format_content(data, "中国地震台网预警")

    if msg_type == "sc_eew":
        return format_content(data, "四川地震局预警")

    if msg_type == "fj_eew":
        return format_content(data, "福建地震局预警")

    if msg_type == "cenc_eqlist":
        # 速报数据字段结构不同，单独处理
        no1 = data.get("No1", {})
        if not no1:
            return None
        eq_time = safe_get(no1, "time")
        location = safe_get(no1, "location")
        mag = safe_get(no1, "magnitude")
        depth = safe_get(no1, "depth")
        intensity = safe_get(no1, "intensity", "未知")

        title = f"地震速报 · M{mag}"
        subtitle = f"{location}"
        body = (
            f"数据来源：中国地震台网速报\n"
            f"发震时刻：{eq_time}\n"
            f"震中位置：{location}\n"
            f"震级：M{mag}\n"
            f"震源深度：{depth} km\n"
            f"最大烈度：{intensity} 度\n"
            f"推送时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        dedup_key = f"cenc_eqlist|{eq_time}|{location}|{mag}|{depth}|{intensity}"
        return title, subtitle, body, dedup_key

    return None


async def websocket_client(session):
    """WebSocket 客户端，断线自动重连"""
    reconnect_delay = 1
    max_delay = 30

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2 ** 20,
            ) as websocket:
                log("已连接到 Wolfx WebSocket API")
                reconnect_delay = 1

                async for raw in websocket:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as e:
                        log(f"JSON解析错误: {e}")
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
                    log(f"[{title}] {subtitle}")
                    log(f"正文:\n{body}\n{'-' * 40}")
                    await push(session, title, subtitle, body, dedup_key)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"WebSocket错误: {e}, {reconnect_delay}s后重连")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)


async def main():
    timeout = aiohttp.ClientTimeout(total=15)
    connector = aiohttp.TCPConnector(limit=20)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector
    ) as session:
        await websocket_client(session)


if __name__ == "__main__":
    asyncio.run(main())
