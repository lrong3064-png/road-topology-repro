import os
import time
import json
import re
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

# =========================

ENV_KEY_NAME = "AMAP_API_KEY"
PRIVATE_CONFIG_PATH = "config.json"


def load_api_key() -> str:
    key = os.getenv(ENV_KEY_NAME)
    if key:
        return key.strip()

    if os.path.exists(PRIVATE_CONFIG_PATH):
        try:
            with open(PRIVATE_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            key2 = (cfg.get("AMAP_API_KEY") or "").strip()
            if key2:
                return key2
        except Exception as e:
            raise RuntimeError(f"读取 {PRIVATE_CONFIG_PATH} 失败：{e}") from e

    raise RuntimeError(
        f"未找到高德 Key。请设置环境变量 {ENV_KEY_NAME}，或创建本地 {PRIVATE_CONFIG_PATH}"
    )


# =========================
# 1) 参数区
# =========================
INPUT_FILE = "cities_with_coords.xlsx"  # 必须包含列: 地级市, X(经度), Y(纬度)
OUTPUT_DIR = Path("od_results")
JSON_DIR = OUTPUT_DIR / "jsons"
SUMMARY_FILE = OUTPUT_DIR / "route_summary_index.json"

REQUEST_DELAY = 0.35 # 每次请求后的 sleep
MAX_RETRIES = 3
TIMEOUT_SEC = 15

# 高德 v3 驾车策略
STRATEGY_HIGHWAY = 2
STRATEGY_SHORTEST = 2

NEIGHBOR_THRESHOLD = 0.6  # 高速占比阈值（可在论文里做敏感性分析）
FALLBACK_LOW_RATIO = 0.01  # 如果高速优先方案几乎没高速，就 fallback 最短

# 正则：用于根据 step['road'] 名称识别高速/快速路
_highway_name_re = re.compile(
    r"(?:G\d+|S\d+|高速|快速|高架|绕城|环路|环线|出口|匝道|主路|立交|隧道|枢纽)",
    flags=re.IGNORECASE,
)


# =========================
# 2) 工具函数
# =========================
def safe_filename(s: str) -> str:
    """避免城市名里出现 / \ : * ? 等导致文件系统报错"""
    return re.sub(r'[\\/:*?"<>| \t\r\n]+', "_", s.strip())


def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)


def request_with_retry(url: str, params: Dict[str, Any], max_retries: int = 3, timeout: int = 15) -> Dict[str, Any]:
    """带重试与指数退避的请求；避免偶发超时/限流直接失败"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            # 非 200 也当错误处理
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            # 指数退避：0.5, 1, 2 ... 秒（可按需调）
            sleep_s = 0.5 * (2 ** (attempt - 1))
            print(f"[WARN] 请求失败（第 {attempt}/{max_retries} 次）：{e}，{sleep_s:.1f}s 后重试")
            time.sleep(sleep_s)

    raise RuntimeError(f"请求多次失败：{last_err}")


def parse_route_v3(data: Dict[str, Any]) -> Optional[Tuple[float, float, float, Any]]:
    """
    解析高德 v3 direction/driving 返回：
    - distance (m)
    - duration (s)
    - highway_ratio
    - steps（原始分段，可选保存）
    """
    if str(data.get("status")) != "1":
        return None
    route = data.get("route", {})
    paths = route.get("paths") or []
    if not paths:
        return None

    path0 = paths[0]
    try:
        distance = float(path0.get("distance", 0.0))
        duration = float(path0.get("duration", 0.0))
        steps = path0.get("steps", []) or []
    except Exception:
        return None

    if distance <= 0:
        highway_ratio = 0.0
    else:
        # 通过 road 名称匹配高速/快速路（你目前的实现）
        highway_distance = 0.0
        for step in steps:
            road = step.get("road") or ""
            if road and _highway_name_re.search(road):
                try:
                    highway_distance += float(step.get("distance", 0.0))
                except Exception:
                    pass
        highway_ratio = highway_distance / distance

    return distance, duration, highway_ratio, steps


def get_route_amap_v3(
    api_key: str,
    origin: Dict[str, float],
    dest: Dict[str, float],
    strategy: int,
) -> Optional[Dict[str, Any]]:
    """
    调用高德 v3 驾车路径规划 API（direction/driving）
    返回：distance, duration, highway_ratio, steps
    """
    url = "https://restapi.amap.com/v3/direction/driving"
    params = {
        "key": api_key,
        "origin": f"{origin['X']},{origin['Y']}",
        "destination": f"{dest['X']},{dest['Y']}",
        "strategy": strategy,
        "extensions": "all",
        "output": "JSON",
    }

    try:
        data = request_with_retry(url, params, max_retries=MAX_RETRIES, timeout=TIMEOUT_SEC)
        parsed = parse_route_v3(data)
        if not parsed:
            return None
        distance, duration, highway_ratio, steps = parsed
        return {
            "distance": distance,
            "duration": duration,
            "highway_ratio": highway_ratio,
            "steps": steps
        }
    except Exception as e:
        print(f"[ERROR] Amap route failed: {e}")
        return None


# =========================
# 3) 主流程：构建邻接/距离/时间矩阵
# =========================
def main():
    api_key = load_api_key()
    ensure_dirs()

    # 读取城市
    df = pd.read_excel(INPUT_FILE)
    required_cols = {"地级市", "X", "Y"}
    if not required_cols.issubset(set(df.columns)):
        raise ValueError(f"输入文件必须包含列：{required_cols}，当前列为：{list(df.columns)}")

    cities = df["地级市"].astype(str).tolist()
    coords = df.set_index("地级市")[["X", "Y"]].to_dict("index")
    n = len(cities)

    adj_matrix = np.zeros((n, n), dtype=int)
    dist_matrix = np.zeros((n, n), dtype=float)
    duration_matrix = np.zeros((n, n), dtype=float)

    # 公开友好摘要：不含 steps
    summary_index = {
        "meta": {
            "api": "Amap v3 direction/driving",
            "strategy_highway": STRATEGY_HIGHWAY,
            "strategy_fallback": STRATEGY_SHORTEST,
            "neighbor_threshold": NEIGHBOR_THRESHOLD,
            "fallback_low_ratio": FALLBACK_LOW_RATIO,
            "request_delay_sec": REQUEST_DELAY,
            "max_retries": MAX_RETRIES,
        },
        "pairs": {}
    }

    for i in range(n):
        for j in range(i + 1, n):  # 上三角（无向图）
            c1, c2 = cities[i], cities[j]
            origin, dest = coords[c1], coords[c2]

            # 1) 高速优先
            info = get_route_amap_v3(api_key, origin, dest, strategy=STRATEGY_HIGHWAY)
            time.sleep(REQUEST_DELAY)

            # 2) 高速占比很低或失败 → fallback 最短/默认策略
            if (info is None) or (info.get("highway_ratio", 0.0) < FALLBACK_LOW_RATIO):
                info = get_route_amap_v3(api_key, origin, dest, strategy=STRATEGY_SHORTEST)
                time.sleep(REQUEST_DELAY)

            if info is None:
                print(f"[FAIL] {c1} -> {c2}")
                continue

            # 保存
            json_path = JSON_DIR / f"{safe_filename(c1)}_{safe_filename(c2)}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)

            # 填矩阵
            dist_matrix[i, j] = dist_matrix[j, i] = info["distance"]
            duration_matrix[i, j] = duration_matrix[j, i] = info["duration"]
            adj_val = 1 if info["highway_ratio"] >= NEIGHBOR_THRESHOLD else 0
            adj_matrix[i, j] = adj_matrix[j, i] = adj_val


            pair_key = f"{c1}__{c2}"
            summary_index["pairs"][pair_key] = {
                "distance_m": info["distance"],
                "duration_s": info["duration"],
                "highway_ratio": info["highway_ratio"],
                "adj": int(adj_val),
            }

    # 保存矩阵（可公开）
    adj_df = pd.DataFrame(adj_matrix, index=cities, columns=cities)
    dist_df = pd.DataFrame(dist_matrix, index=cities, columns=cities)
    duration_df = pd.DataFrame(duration_matrix, index=cities, columns=cities)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    adj_df.to_excel(OUTPUT_DIR / "adjacency_matrix.xlsx")
    dist_df.to_excel(OUTPUT_DIR / "distance_matrix.xlsx")
    duration_df.to_excel(OUTPUT_DIR / "duration_matrix.xlsx")

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary_index, f, ensure_ascii=False, indent=2)

    print("完成：邻接矩阵、距离矩阵、时间矩阵已保存。")
    print(f"JSON（含 steps）保存路径：{JSON_DIR} ）")


if __name__ == "__main__":
    main()
