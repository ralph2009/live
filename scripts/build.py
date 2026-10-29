#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播源聚合与清洗工具
------------------
1. 从 config.json 里配置的多个上游源下载直播列表（m3u / txt 两种格式都支持）
2. 去掉购物、推广、广告类频道
3. 同名频道去重，优先保留高优先级源、高清画质的线路
4. 输出与 fanmingming 源相同格式的 m3u，另附 DIYP 格式 txt

只依赖 Python 标准库，方便在 GitHub Actions 里零配置运行。
"""

import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 分组排序：常见的国内分组放前面，其余按出现顺序
GROUP_ORDER = [
    "央视频道", "卫视频道", "地方频道", "港澳台", "国际频道",
    "体育频道", "电影频道", "纪录频道", "少儿频道", "音乐频道",
]

# 英文分组 -> 中文分组
GROUP_ALIAS_EN = {
    "movies": "电影频道", "movie": "电影频道", "cinema": "电影频道",
    "sports": "体育频道", "sport": "体育频道",
    "documentary": "纪录频道", "docs": "纪录频道",
    "kids": "少儿频道", "children": "少儿频道", "animation": "少儿频道",
    "music": "音乐频道", "news": "新闻频道",
    "education": "教育频道", "science": "科教频道",
    "culture": "文化频道", "entertainment": "娱乐频道",
    "business": "财经频道", "finance": "财经频道",
    "weather": "气象频道", "family": "家庭频道",
    "legislative": "政务频道", "classic": "经典频道",
    "general": "综合频道", "lifestyle": "生活频道", "travel": "旅游频道",
}

# 这些分组整组丢弃（购物/宗教/未分类等噪音）
DROP_GROUPS = {"undefined", "shop", "shopping", "religious", "religion", "xxx", "adult"}

# 省级分组（内蒙频道 / 浙江频道 …）统一归入地方频道
PROVINCES = [
    "北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
    "内蒙", "香港", "澳门", "台湾",
]

# 画质关键词 -> 分值（分越高越优先）
QUALITY_SCORE = [
    (("4k", "2160", "uhd"), 60),
    (("蓝光", "bluray", "blu-ray"), 50),
    (("1080",), 40),
    (("超清", "超高清", "fhd"), 35),
    (("高清", "hd", "hq"), 30),
    (("720",), 20),
    (("标清", "sd", "流畅"), 10),
]

QUALITY_NOISE = [
    "4k", "2160p", "1080p", "720p", "uhd", "fhd", "hd", "hq", "sd",
    "蓝光", "超高清", "超清", "高清", "标清", "流畅", "高清版", "超清版",
]

# 中央台统一写法：CCTV-1综合 / CCTV1 / cctv-1 -> CCTV1
CCTV_RE = re.compile(r"(?i)^cctv[\s\-_]*(\d{1,2})\s*(\+|plus|k)?\s*(.*)$")


def now_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 下载

def mirror_urls(url: str):
    """为 GitHub raw 地址补充国内可用的镜像，提升拉取成功率。"""
    urls = [url]
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
    if m:
        owner, repo, branch, path = m.groups()
        urls += [
            f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}",
            f"https://gcore.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}",
            f"https://ghproxy.net/{url}",
            f"https://gh-proxy.com/{url}",
        ]
    return urls


def fetch(url: str, retries: int = 3, timeout: int = 30) -> str:
    last_err = None
    for attempt in range(retries):
        for candidate in mirror_urls(url):
            req = urllib.request.Request(candidate, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
            })
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                        try:
                            raw = gzip.decompress(raw)
                        except OSError:
                            pass
                    if len(raw) < 32:
                        raise ValueError("响应内容过短")
                    for enc in ("utf-8-sig", "utf-8", "gb18030"):
                        try:
                            return raw.decode(enc)
                        except UnicodeDecodeError:
                            continue
                    return raw.decode("utf-8", "ignore")
            except Exception as e:  # noqa: BLE001 网络错误种类繁多，统一重试
                last_err = f"{candidate} -> {e}"
                continue
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"全部线路失败: {last_err}")


# ---------------------------------------------------------------- 解析

ATTR_RE = re.compile(r'([a-zA-Z0-9\-_]+)="([^"]*)"')


def parse_m3u(text: str, source: dict):
    entries = []
    pending = None
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            pending = {"attrs": attrs, "name": name}
        elif line.startswith("#"):
            continue
        elif pending is not None:
            entries.append({
                "name": pending["name"],
                "url": line,
                "group": pending["attrs"].get("group-title", "").strip(),
                "logo": pending["attrs"].get("tvg-logo", "").strip(),
                "tvg_name": pending["attrs"].get("tvg-name", "").strip(),
                "tvg_id": pending["attrs"].get("tvg-id", "").strip(),
                "source": source["name"],
                "priority": source.get("priority", 5),
            })
            pending = None
    return entries


def parse_txt(text: str, source: dict):
    """DIYP / 影视仓常用的 txt 格式：`分组,#genre#` 后面跟 `频道名,地址`"""
    entries = []
    group = ""
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.endswith("#genre#"):
            group = line.replace("#genre#", "").strip(" ,，")
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        url = url.strip()
        if not re.match(r"^(https?|rtmp|rtsp|udp|rtp|igmp|p3p|mitv)://", url, re.I):
            continue
        entries.append({
            "name": name.strip(),
            "url": url,
            "group": group,
            "logo": "",
            "tvg_name": "",
            "tvg_id": "",
            "source": source["name"],
            "priority": source.get("priority", 5),
        })
    return entries


def parse_any(text: str, source: dict):
    if "#EXTM3U" in text[:2048] or "#EXTINF" in text[:4096]:
        return parse_m3u(text, source)
    return parse_txt(text, source)


# ---------------------------------------------------------------- 清洗

def strip_quality(name: str) -> str:
    out = name
    for token in QUALITY_NOISE:
        out = re.sub(re.escape(token), "", out, flags=re.I)
    return re.sub(r"[\s\-_·|/（）()\[\]【】]+", "", out).strip()


def quality_score(text: str) -> int:
    low = text.lower()
    for tokens, score in QUALITY_SCORE:
        if any(t in low for t in tokens):
            return score
    return 0


def channel_key(name: str, tvg_name: str = "") -> str:
    """把同一频道的各种写法归一到同一个 key，便于去重。"""
    base = strip_quality(tvg_name) or strip_quality(name)
    base = base.replace("电视台", "").replace("频道", "")
    m = CCTV_RE.match(base)
    if m:
        num, plus, _rest = m.groups()
        return f"CCTV{int(num)}{'+' if plus else ''}"
    if re.match(r"(?i)^cctv", base):
        return base.upper()
    # 湖南卫视 / 湖南卫视高清 统一为 湖南卫视
    if "卫视" in base:
        return base.split("卫视")[0] + "卫视"
    return base.upper()


GROUP_EMOJI_RE = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fa5]+")


def normalize_group(group: str, name: str) -> str:
    g = GROUP_EMOJI_RE.sub("", group.strip()).replace("·", "").strip()
    if not g:
        g = "其他频道"
    low = g.lower()
    if any(k in g for k in ("央视", "CCTV", "cctv")) or low in ("cctv", "central"):
        return "央视频道"
    if "卫视" in g:
        return "卫视频道"
    if any(k in g for k in ("地方", "城市", "省内", "本地")):
        return "地方频道"
    if any(k in g for k in ("港澳", "香港", "台湾", "澳门")) or low in ("hk", "tw", "mo"):
        return "港澳台"
    if any(k in g for k in ("国际", "海外")) or low in ("international",):
        return "国际频道"
    alias = {
        "体育": "体育频道", "电影": "电影频道", "纪录": "纪录频道", "记录": "纪录频道",
        "少儿": "少儿频道", "儿童": "少儿频道", "音乐": "音乐频道", "戏曲": "戏曲频道",
        "数字": "数字频道",
    }
    for k, v in alias.items():
        if k in g:
            return v
    if g.endswith("频道") and any(p in g for p in PROVINCES):
        return "地方频道"
    # 英文分组统一翻成中文，避免列表里中英混杂
    for seg in re.split(r"[;,/&|]", low):
        seg = seg.strip()
        if seg in GROUP_ALIAS_EN:
            return GROUP_ALIAS_EN[seg]
    return g


def is_dropped_group(group: str) -> bool:
    low = group.strip().lower()
    if low in DROP_GROUPS:
        return True
    return any(seg.strip() in DROP_GROUPS for seg in re.split(r"[;,/&|]", low))


def is_promo(entry: dict, filters: dict) -> bool:
    name = entry["name"]
    group = entry["group"]
    blob = f"{name} {group} {entry.get('tvg_name', '')}"
    for kw in filters["promo_keywords"]:
        if kw in blob:
            return True
    for blocked in filters["blocked_groups"]:
        if blocked and blocked in group:
            return True
    for pat in filters.get("blocked_name_patterns", []):
        if pat and re.search(pat, name):
            return True
    if not filters.get("keep_foreign", True):
        if any(k in group for k in ("Religious", "Shopping", "Shop", "Religion")):
            return True
    return False


def logo_for(entry: dict, key: str, cfg: dict) -> str:
    base = cfg["output"].get("logo_base", "")
    known = re.match(r"^(CCTV\d{1,2}\+?)$", key)
    if known and base:
        return f"{base}{key}.png"
    if entry.get("logo"):
        return entry["logo"]
    if base and re.match(r"^[\u4e00-\u9fa5]{2,6}卫视$", key):
        return f"{base}{key}.png"
    return ""


def build_entries(cfg: dict):
    filters = cfg["filters"]
    all_entries, source_stats = [], []

    for source in cfg["sources"]:
        if not source.get("enabled", True):
            continue
        try:
            text = fetch(source["url"])
            raw_entries = parse_any(text, source)
            source_stats.append({"source": source["name"], "ok": True, "raw": len(raw_entries)})
            log(f"[源] {source['name']:<18} 拉取成功，{len(raw_entries)} 条")
        except Exception as e:  # noqa: BLE001
            source_stats.append({"source": source["name"], "ok": False, "raw": 0, "error": str(e)[:200]})
            log(f"[源] {source['name']:<18} 拉取失败: {e}")
            continue
        all_entries.extend(raw_entries)

    log(f"\n[汇总] 原始条目 {len(all_entries)} 条")

    # 1) 过滤推广 / 无效
    allowed_schemes = tuple(filters.get("allowed_schemes", ("http", "https")))
    kept, removed_promo = [], 0
    for e in all_entries:
        if not e["name"] or not e["url"]:
            continue
        if not e["url"].lower().startswith(allowed_schemes):
            removed_promo += 1
            continue
        if is_promo(e, filters):
            removed_promo += 1
            continue
        e["group"] = normalize_group(e["group"], e["name"])
        if is_dropped_group(e["group"]):
            removed_promo += 1
            continue
        e["key"] = channel_key(e["name"], e.get("tvg_name", ""))
        if not e["key"]:
            continue
        kept.append(e)
    log(f"[过滤] 剔除推广/广告频道 {removed_promo} 条，剩余 {len(kept)} 条")

    # 2) 同频道选最优线路（优先级 -> 画质），地址去重
    max_per = int(cfg["output"].get("max_per_channel", 1) or 1)
    candidates = {}
    for e in kept:
        candidates.setdefault(e["key"], []).append(e)

    entries = []
    for cands in candidates.values():
        cands.sort(key=lambda x: (x["priority"],
                                  -quality_score(x["name"] + " " + x.get("tvg_name", ""))))
        seen, picked = set(), []
        for c in cands:
            url_key = c["url"].split("?")[0]
            if url_key in seen:
                continue
            seen.add(url_key)
            picked.append(c)
            if len(picked) >= max_per:
                break
        entries.extend(picked)
    log(f"[去重] {len(kept)} 条线路归并为 {len(entries)} 个频道条目")

    # 3) 排序：分组顺序 -> 频道自然顺序（CCTV 数字在前）
    def sort_key(e):
        g = e["group"]
        gi = GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)
        key = e["key"]
        m = re.match(r"^CCTV(\d+)(\+?)$", key)
        if m:
            name_rank = (0, int(m.group(1)), m.group(2))
        else:
            name_rank = (1, 0, key)
        return (gi, g, name_rank)

    entries.sort(key=sort_key)
    return entries, source_stats, removed_promo


# ---------------------------------------------------------------- 输出

def write_m3u(entries, cfg, path):
    out = io.StringIO()
    header = f'#EXTM3U x-tvg-url="{cfg["output"].get("epg", "")}"'
    out.write(header + "\n")
    for e in entries:
        key = e["key"]
        tvg_name = e.get("tvg_name") or key
        attrs = []
        if cfg["output"].get("include_tvg_id", True) and e.get("tvg_id"):
            attrs.append(f'tvg-id="{e["tvg_id"]}"')
        attrs.append(f'tvg-name="{tvg_name}"')
        logo = logo_for(e, key, cfg)
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        attrs.append(f'group-title="{e["group"]}"')
        out.write(f'#EXTINF:-1 {" ".join(attrs)},{e["name"]}\n{e["url"]}\n')
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(out.getvalue())


def write_txt(entries, path):
    lines = ["更新时间,#genre#", now_cn() + ","]
    current = None
    for e in entries:
        if e["group"] != current:
            current = e["group"]
            lines.append("")
            lines.append(f"{current},#genre#")
        lines.append(f'{e["name"]},{e["url"]}')
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)

    entries, source_stats, removed = build_entries(cfg)
    if not entries:
        log("[错误] 没有可用频道，保留上一次的输出文件不动")
        return 1

    groups = {}
    for e in entries:
        groups[e["group"]] = groups.get(e["group"], 0) + 1

    write_m3u(entries, cfg, cfg["output"]["m3u"])
    if cfg["output"].get("txt"):
        write_txt(entries, cfg["output"]["txt"])

    stats = {
        "updated_at": now_cn(),
        "total_channels": len(entries),
        "removed_promo": removed,
        "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])),
        "sources": source_stats,
    }
    with open(cfg["output"]["stats"], "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    log(f"[输出] 共 {len(entries)} 个频道 -> {cfg['output']['m3u']}")
    for g, n in stats["groups"].items():
        log(f"       {g}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
