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
from urllib.parse import quote
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
    "内蒙", "内蒙古", "香港", "澳门", "台湾",
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


def idna_url(url: str) -> str:
    """把 http://饭太硬.cc/tv 这种中文域名转成可请求的 punycode 形式。"""
    m = re.match(r"^(https?://)([^/:]+)(.*)$", url)
    if not m:
        return url
    scheme, host, rest = m.groups()
    try:
        host.encode("ascii")
        return url
    except UnicodeEncodeError:
        try:
            return scheme + host.encode("idna").decode("ascii") + rest
        except UnicodeError:
            return url


def fetch(url: str, retries: int = 3, timeout: int = 30) -> str:
    last_err = None
    for attempt in range(retries):
        for candidate in mirror_urls(url):
            req = urllib.request.Request(idna_url(candidate), headers={
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


def clean_url(url: str) -> str:
    """去掉地址后面的线路说明标签，例如 `http://x/a.m3u8$LR•IPV4『线路2』`。"""
    url = url.strip()
    if "$" in url:
        url = url.split("$", 1)[0].strip()
    return url


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
        url = clean_url(url)
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


# ---------------------------------------------------------------- TVBox 接口（饭太硬等）

ENDPOINT_RE = re.compile(r"https?://[^\s\"'<>|)\]}]+")
TVBOX_HINTS = ("饭太硬", "xxooo.cf", "fanying", "xn--", "fty.")


def load_remembered_endpoints(source: dict):
    path = (source.get("remember") or "data/endpoints.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [u for u in data.get(source["name"], []) if isinstance(u, str)]


def remember_endpoints(source: dict, urls):
    """把这次成功的接口地址记下来，下次（尤其是换域名后）优先尝试。"""
    path = source.get("remember") or "data/endpoints.json"
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    old = [u for u in data.get(source["name"], []) if isinstance(u, str)]
    merged = list(dict.fromkeys(list(urls) + old))[:12]
    merged += [u for u in list(source.get("urls") or []) if u not in merged]
    data[source["name"]] = merged[:12]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def discover_endpoints(pages):
    """所有已知地址都失效时，去公开索引页里重新找饭太硬的接口地址。"""
    found = []
    for page in pages:
        try:
            text = fetch(page, retries=1, timeout=20)
        except Exception as e:  # noqa: BLE001
            log(f"[发现] 索引页 {page} 拉取失败: {e}")
            continue
        for m in ENDPOINT_RE.finditer(text):
            u = m.group(0).rstrip(".,，、")
            if any(h in u for h in TVBOX_HINTS):
                found.append(u)
    return list(dict.fromkeys(found))


def load_tvbox_lives(source: dict, urls=None):
    """TVBox 接口（饭太硬这类）：从配置里取出 lives 列表，再把每个直播列表拉下来解析。

    返回 (条目列表, 成功解析出直播源的接口地址列表)。
    """
    urls = urls if urls is not None else list(source.get("urls") or ([source["url"]] if source.get("url") else []))
    entries, used = [], []
    for candidate in urls:
        try:
            raw = fetch(candidate, retries=1, timeout=25).strip()
        except Exception as e:  # noqa: BLE001
            log(f"[接口] {candidate} 拉取失败: {str(e)[:80]}")
            continue
        if not raw.startswith("{"):
            log(f"[接口] {candidate} 不是 JSON 配置（可能被加密或反爬），跳过")
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            log(f"[接口] {candidate} JSON 解析失败")
            continue
        lives = data.get("lives") or []
        if not lives:
            log(f"[接口] {candidate} 里没有直播源")
            continue
        log(f"[接口] {candidate} 里找到 {len(lives)} 个直播源")
        for item in lives:
            if not isinstance(item, dict) or item.get("api"):
                continue
            live_url = (item.get("url") or "").strip()
            if not live_url.lower().startswith(("http://", "https://")):
                continue
            label = (item.get("name") or "直播").strip()
            sub = {"name": f"{source['name']}-{label}"[:26], "priority": source.get("priority", 4)}
            try:
                text = fetch(live_url, retries=1, timeout=25)
            except Exception as e:  # noqa: BLE001
                log(f"[接口]   直播源「{label}」拉取失败: {str(e)[:60]}")
                continue
            part = parse_any(text, sub)
            log(f"[接口]   {label}: {len(part)} 条")
            entries.extend(part)
            if candidate not in used:
                used.append(candidate)
    return entries, used


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
    raw = PROVINCE_PREFIX_RE.sub("", (tvg_name or name).strip())
    if re.match(r"(?i)^cctv[\s\-_]*4\s*k", raw):
        return "CCTV4K"
    if re.match(r"(?i)^cctv[\s\-_]*8\s*k", raw):
        return "CCTV8K"
    base = strip_quality(tvg_name) or strip_quality(name)
    base = PROVINCE_PREFIX_RE.sub("", base)
    base = base.replace("电视台", "").replace("频道", "")
    m = CCTV_RE.match(base)
    if m:
        num, plus, _rest = m.groups()
        return f"CCTV{int(num)}{'+' if plus else ''}"
    # CCTV 旗下的非数字频道：CCTV世界地理 / 世界地理 / 央视精品 统一成同一个 key
    core = re.sub(r"(?i)^cctv[\s\-_·]*", "", base)
    if core != base or base.startswith("央视"):
        core = re.sub(r"^央视(?=[\u4e00-\u9fa5]{2,})", "", core)
        core = CANON_ALIAS.get(core.upper(), core)
        if core:
            return core
        return "CCTV"
    # 中国教育1台 / 中国教育一台 / CETV-1 都是同一个台
    m = re.match(r"^中国教育(?:电视台)?[\s\-_]*([1-9一二三四五六七八九])\s*(?:台|套|频道)?$", base)
    if m:
        return "CETV" + CN_NUM.get(m.group(1), m.group(1))
    # CETV-1 / CETV1
    if re.match(r"(?i)^cetv", base):
        return re.sub(r"(?i)^cetv[\s\-_]*", "CETV", base)
    # CGTN 阿语/阿拉伯语、英语/CGTN 本身
    if re.match(r"(?i)^cgtn", base):
        tail = re.sub(r"(?i)^cgtn[\s\-_]*", "", base)
        tail = CANON_ALIAS.get("CGTN" + tail.upper(), CANON_ALIAS.get(tail.upper(), tail))
        return "CGTN" + tail
    # 湖南卫视 / 湖南卫视高清 统一为 湖南卫视
    if "卫视" in base:
        return base.split("卫视")[0] + "卫视"
    return base.upper()


# 同一频道的其它写法 -> 标准写法
CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
          "六": "6", "七": "7", "八": "8", "九": "9"}

CANON_ALIAS = {
    "CGTN阿拉伯语": "阿语", "CGTN西班牙语": "西语", "CGTN英语": "", "CGTN频道": "",
    "CGTN记录": "纪录", "CGTN纪录频道": "纪录",
    "央视文化精品": "文化精品", "央视精品": "文化精品",
    "高尔夫•网球": "高尔夫网球", "高尔夫网球频道": "高尔夫网球",
    "央视台球": "台球", "电视指南": "电视指南",
}


# 「上海｜东方卫视」「黑龙江·新闻综合」这种带省份前缀的写法，前缀去掉再比对
PROVINCE_PREFIX_RE = re.compile(r"^(?:%s)\s*[｜|·•\-—_/]\s*" % "|".join(PROVINCES))


def display_name(name: str, key: str) -> str:
    """列表里显示的名字：去掉省份前缀，CCTV 统一成 CCTV-1 这种写法。"""
    out = PROVINCE_PREFIX_RE.sub("", name.strip())
    m = re.match(r"^CCTV(\d+)(\+?)$", key)
    if m:
        return f"CCTV-{m.group(1)}{m.group(2)}"
    return out or name



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
    # 分组直接写成省名（江苏 / 安徽 / 内蒙古 …）也归入地方频道
    bare = g.replace("频道", "").replace("电视台", "").replace("台", "").replace("省", "").strip()
    if bare in PROVINCES or bare + "省" in PROVINCES:
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


LOGO_ALIAS = {
    "CGTN英语": "CGTN", "CGTN纪录": "CGTN纪录", "CGTN记录": "CGTN纪录",
    "CGTN阿拉伯语": "CGTN阿语", "CGTN西班牙语": "CGTN西语", "CGTN俄语": "CGTN俄语",
    "CGTN法语": "CGTN法语", "央视文化精品": "文化精品", "央视精品": "文化精品",
    "CCTV4K": "CCTV4K", "CCTV4K超高清": "CCTV4K",
}


def logo_for(entry: dict, key: str, cfg: dict, logo_index=None, auto_logos=None) -> str:
    base = cfg["output"].get("logo_base", "")
    auto_base = (cfg.get("logos") or {}).get("auto_base",
                "https://cdn.jsdelivr.net/gh/ralph2009/live@main/logos/")
    if logo_index and base:
        cands = [key]
        if key.upper().startswith("CCTV") and len(key) > 4:
            cands += [key[4:], key[4:].replace("央视", "")]
        cands += [entry.get("tvg_name", ""), entry.get("name", "")]
        for c in cands:
            if not c:
                continue
            for variant in (c, strip_quality(c), LOGO_ALIAS.get(c.upper()) or LOGO_ALIAS.get(c)):
                if not variant:
                    continue
                hit = logo_index.get(norm_logo_key(variant))
                if hit:
                    return hit
    if entry.get("logo"):
        return entry["logo"]
    # 台标库里没有的（多是县级台），用我们自己生成的占位台标
    if auto_logos and auto_base:
        for c in (entry.get("name", ""), entry.get("tvg_name", ""), key):
            fname = auto_logos.get(c)
            if fname:
                return f"{auto_base}{quote(fname)}"
    if base and re.match(r"^[\u4e00-\u9fa5]{2,6}卫视$", key):
        return f"{base}{key}.png"
    return ""


LOGO_CACHE = "data/logos.json"

# 台标库：按顺序匹配，前面的优先。base 后面拼文件名（中文会被 URL 编码）
DEFAULT_LOGO_LIBS = [
    {
        "name": "fanmingming",
        "tree": "https://api.github.com/repos/fanmingming/live/git/trees/main?recursive=1",
        "prefix": "tv/",
        "base": "https://live.fanmingming.cn/tv/",
    },
    {
        "name": "wanglindl",
        "tree": "https://api.github.com/repos/vircloud/TVLogo/git/trees/main?recursive=1",
        "prefix": "",
        "base": "https://cdn.jsdelivr.net/gh/vircloud/TVLogo@main/",
    },
    {
        "name": "samkolau",
        "tree": "https://api.github.com/repos/samkolau/TVLOGO/git/trees/main?recursive=1",
        "prefix": "logo/",
        "base": "https://cdn.jsdelivr.net/gh/samkolau/TVLOGO@main/",
    },
]


def norm_logo_key(s: str) -> str:
    s = s.strip().upper()
    s = re.sub(r"\.(PNG|JPG|JPEG)$", "", s)
    return re.sub(r"[\s\-_·•|/（）()\[\]【】\.,、]", "", s)


def _fetch_logo_lib(lib):
    """返回 {归一化名字: 完整台标地址}"""
    req = urllib.request.Request(lib["tree"], headers={
        "User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    out = {}
    prefix = lib.get("prefix", "")
    for it in data.get("tree", []):
        path = it.get("path", "")
        if it.get("type") != "blob" or not path.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        if prefix and not path.startswith(prefix):
            continue
        name = path[len(prefix):] if prefix else path
        key = norm_logo_key(name)
        if key and key not in out:
            out[key] = lib["base"] + quote(name)
    return out


def load_logo_index(cfg: dict):
    """合并多个台标库（约 2900 张），缓存到 data/logos.json，默认 7 天更新一次。"""
    lg = cfg.get("logos") or {}
    if not lg.get("enabled", True):
        return {}
    cache = lg.get("cache", LOGO_CACHE)
    ttl = float(lg.get("ttl_days", 7)) * 86400
    libs = lg.get("libraries") or DEFAULT_LOGO_LIBS

    data = None
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if data and data.get("index") and time.time() - data.get("fetched_at", 0) < ttl:
            log(f"[台标] 已加载 {len(data['index'])} 个台标（缓存）")
            return data["index"]

    index, got = dict((data or {}).get("index", {})), []
    for lib in libs:
        try:
            part = _fetch_logo_lib(lib)
            for k, v in part.items():
                index.setdefault(k, v)      # 排在前面的库优先
            got.append(f"{lib['name']}:{len(part)}")
        except Exception as e:  # noqa: BLE001 某个库挂了不影响其它库
            log(f"[台标] {lib.get('name')} 拉取失败：{e}")

    if got:
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        with open(cache, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"fetched_at": time.time(), "index": index}, f, ensure_ascii=False)
        log(f"[台标] 索引已更新：{len(index)} 个台标（{'、'.join(got)}）")
    elif data:
        log(f"[台标] 全部台标库拉取失败，使用缓存 {len(index)} 个")
    return index


def build_entries(cfg: dict):
    filters = cfg["filters"]
    all_entries, source_stats = [], []

    for source in cfg["sources"]:
        if not source.get("enabled", True):
            continue
        if source.get("type") == "tvbox":
            # 尝试顺序：上次能用的地址 -> 配置里的候选 -> 全网重新搜到的地址
            tries = []
            remembered = load_remembered_endpoints(source)
            if remembered:
                log(f"[接口] 上次可用地址 {len(remembered)} 个，优先试")
                tries.append(remembered)
            if source.get("urls") or source.get("url"):
                tries.append(list(source.get("urls") or [source["url"]]))
            raw_entries, used = [], []
            for urls in tries:
                raw_entries, used = load_tvbox_lives(source, urls=urls)
                if raw_entries:
                    break
            if not raw_entries and source.get("discover"):
                log(f"[接口] {source['name']} 已知地址都失效，全网重新找…")
                extra = discover_endpoints(source["discover"])
                if extra:
                    log(f"[接口] 搜到 {len(extra)} 个候选地址，逐个尝试")
                    raw_entries, used = load_tvbox_lives({**source, "urls": extra}, urls=extra)
            if used:
                remember_endpoints(source, used)
            ok = bool(raw_entries)
            source_stats.append({"source": source["name"], "ok": ok, "raw": len(raw_entries)})
            log(f"[源] {source['name']:<18} {'拉取成功，' + str(len(raw_entries)) + ' 条' if ok else '没有拿到可用直播源'}")
            all_entries.extend(raw_entries)
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
    url_blocks = [re.compile(p, re.I) for p in filters.get("blocked_url_patterns", []) if p]
    kept, removed_promo, removed_url = [], 0, 0
    for e in all_entries:
        if not e["name"] or not e["url"]:
            continue
        e["url"] = clean_url(e["url"])
        if not e["url"].lower().startswith(allowed_schemes):
            removed_promo += 1
            continue
        if any(p.search(e["url"]) for p in url_blocks):
            removed_url += 1
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
        e["name"] = display_name(e["name"], e["key"])
        kept.append(e)
    log(f"[过滤] 剔除推广/广告频道 {removed_promo} 条，剔除无效地址 {removed_url} 条，剩余 {len(kept)} 条")

    # 2) 按频道归并，同一频道保留多条线路（去重后按 源优先级 -> 画质 排序）
    candidates = {}
    for e in kept:
        candidates.setdefault(e["key"], []).append(e)

    for key, cands in candidates.items():
        cands.sort(key=lambda x: (x["priority"],
                                  -quality_score(x["name"] + " " + x.get("tvg_name", ""))))
        seen, unique = set(), []
        for c in cands:
            if c["url"] in seen:
                continue
            seen.add(c["url"])
            unique.append(c)
        candidates[key] = unique

    total_lines = sum(len(v) for v in candidates.values())
    log(f"[归并] {len(kept)} 条线路 -> {len(candidates)} 个频道（共 {total_lines} 条可用线路）")
    return candidates, source_stats, {"promo": removed_promo, "bad_url": removed_url}


def build_pool(candidates: dict, cfg: dict):
    """挑出要送去体检的地址：默认全部送检；test_per_channel 大于 0 时每台限量，并优先照顾不同主机。"""
    per = int((cfg.get("health") or {}).get("test_per_channel", 0) or 0)
    pool = []
    for key, cands in candidates.items():
        if per <= 0:
            pool.extend(cands)
            continue
        chosen, hosts = [], set()
        for c in cands:                      # 第一轮：每个主机各取一条
            host = (re.sub(r"^https?://", "", c["url"]).split("/")[0]).lower()
            if host in hosts:
                continue
            hosts.add(host)
            chosen.append(c)
            if len(chosen) >= per:
                break
        if len(chosen) < per:                # 第二轮：不够再按顺序补齐
            for c in cands:
                if c not in chosen:
                    chosen.append(c)
                if len(chosen) >= per:
                    break
        pool.extend(chosen)
    return pool


def name_rank(name: str):
    """给同一频道的候选名字打分：越小越适合做展示名。
    优先保留带品牌前缀的写法（央视台球 好于 台球），其次是更短的（东方卫视 好于 东方卫视4K）。
    """
    brand = bool(re.match(r"(?i)^(cctv|cgtn|cetv|chc|央视|中国教育)", name)) or "卫视" in name
    return (0 if brand else 1, len(name), name)


def select_entries(candidates: dict, cfg: dict, health: dict):
    """结合体检结果挑最终线路：能用(ok) > 未确认(unknown/未测) > 失效(bad，默认丢弃)。"""
    hcfg = cfg.get("health") or {}
    max_per = int(cfg["output"].get("max_per_channel", 0) or 0)   # 0 或负数 = 不限线路数
    drop_bad = bool(hcfg.get("drop_bad", True))

    def rank(url):
        r = (health or {}).get(url)
        if not r:
            return 2, "未测"
        if r["status"] == "ok":
            return 3, "可用"
        if r["status"] == "bad":
            return 0, "失效"
        return 2, "未确认"

    entries, dropped_channels, bad_dropped = [], 0, 0
    for key, cands in candidates.items():
        scored = []
        for c in cands:
            r, label = rank(c["url"])
            scored.append((r, c, label))
        if drop_bad:
            alive = [x for x in scored if x[0] > 0]
            bad_dropped += len(scored) - len(alive)
            if not alive:
                dropped_channels += 1
                continue
            scored = alive
        # 先按体检结果，再按源优先级和画质
        scored.sort(key=lambda x: (-x[0], x[1]["priority"],
                                   -quality_score(x[1]["name"] + " " + x[1].get("tvg_name", ""))))
        picked = []
        for r, c, label in (scored if max_per <= 0 else scored[:max_per]):
            c = dict(c)
            c["health"] = label
            picked.append(c)
        # 同一个频道所有线路统一用同一个名字（东方卫视4K / 东方卫视 -> 东方卫视），避免列表看着重复
        disp = min((c["name"] for c in cands if c.get("name")),
                   key=lambda n: name_rank(n), default="")
        # 分组也统一（CETV1 有的源放在央视频道、有的放在卫视频道，不能两边都出现）
        gdisp = min((c["group"] for c in cands if c.get("group")),
                    key=lambda g: GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER),
                    default="")
        if disp:
            for c in picked:
                c["name"] = disp
        if gdisp:
            for c in picked:
                c["group"] = gdisp
        entries.extend(picked)

    log(f"[选线] {sum(len(v) for v in candidates.values())} 条线路 -> 输出 {len(entries)} 条"
        f"（丢弃失效 {bad_dropped} 条，整台无可用线路 {dropped_channels} 个）")

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
    return entries, {"bad_lines_dropped": bad_dropped, "channels_dropped": dropped_channels}



# ---------------------------------------------------------------- 输出

def write_m3u(entries, cfg, path, logo_index=None, auto_logos=None):
    out = io.StringIO()
    with_logo = 0
    header = f'#EXTM3U x-tvg-url="{cfg["output"].get("epg", "")}"'
    out.write(header + "\n")
    for e in entries:
        key = e["key"]
        tvg_name = e.get("tvg_name") or key
        attrs = []
        if cfg["output"].get("include_tvg_id", True) and e.get("tvg_id"):
            attrs.append(f'tvg-id="{e["tvg_id"]}"')
        attrs.append(f'tvg-name="{tvg_name}"')
        logo = logo_for(e, key, cfg, logo_index, auto_logos)
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
            with_logo += 1
        attrs.append(f'group-title="{e["group"]}"')
        out.write(f'#EXTINF:-1 {" ".join(attrs)},{e["name"]}\n{e["url"]}\n')
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(out.getvalue())
    return with_logo


def write_txt(entries, path, multi_url=True):
    """DIYP / 影视仓 txt：同一个频道有多条线路时用 # 连接，播放器会自动切线路。"""
    reps, urls = [], {}
    if multi_url:
        for e in entries:
            if e["key"] not in urls:
                urls[e["key"]] = []
                reps.append(e)
            urls[e["key"]].append(e["url"])
    else:
        reps = entries

    lines = ["更新时间,#genre#", now_cn() + ","]
    current = None
    for e in reps:
        if e["group"] != current:
            current = e["group"]
            lines.append("")
            lines.append(f"{current},#genre#")
        if multi_url:
            lines.append(f'{e["name"]},{"#".join(urls[e["key"]])}')
        else:
            lines.append(f'{e["name"]},{e["url"]}')
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def build_channel_catalog(cfg: dict, filters: dict) -> dict:
    """抓「高清电视」App 的官方频道包，去掉推广台，只保留频道清单（该包的播放地址是加密令牌，无法转成标准 m3u）。"""
    fc = cfg.get("fengcai") or {}
    if not fc.get("enabled"):
        return {}

    req = urllib.request.Request(fc["url"], headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8", "ignore"))
    items = data.get("data") or []

    promo_kw = list(filters.get("promo_keywords", []))
    blocked = list(filters.get("blocked_groups", []))
    kept, removed = [], []
    for it in items:
        title = (it.get("title") or "").strip()
        group = (it.get("province") or "").strip() or "其他频道"
        if not title:
            continue
        if any(k in title or k in group for k in promo_kw) or any(b and b in group for b in blocked):
            removed.append({"title": title, "group": group})
            continue
        kept.append({"title": title, "group": group, "num": it.get("num"),
                     "id": it.get("id")})

    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    kept.sort(key=lambda x: (order.get(x["group"], len(GROUP_ORDER)), str(x["num"]).zfill(4)))

    groups = {}
    for c in kept:
        groups[c["group"]] = groups.get(c["group"], 0) + 1

    result = {
        "updated_at": now_cn(),
        "source": fc["url"],
        "total_raw": len(items),
        "total_kept": len(kept),
        "removed_promo": len(removed),
        "groups": groups,
        "removed_samples": [r["title"] for r in removed[:20]],
        "channels": kept,
    }

    out_json = fc.get("output_json", "output/channel-catalog.json")
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    with open(out_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    out_txt = fc.get("output_txt", "output/channel-catalog.txt")
    lines = [f"官方频道清单（已去推广台）  更新时间：{result['updated_at']}",
             f"原始 {len(items)} 个频道，保留 {len(kept)} 个，剔除推广 {len(removed)} 个", ""]
    current = None
    for c in kept:
        if c["group"] != current:
            current = c["group"]
            lines.append(f"\n【{current}】")
        lines.append(f'  {c["title"]}')
    with open(out_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    log(f"[清单] 官方频道包 {len(items)} 条 -> 保留 {len(kept)} 条, 剔除推广 {len(removed)} 条 -> {out_json}")
    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="直播源聚合与清洗")
    parser.add_argument("--no-health", action="store_true", help="跳过地址体检，直接输出")
    parser.add_argument("--pool-only", action="store_true", help="只生成待测地址池 output/.pool.json，不输出列表")
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)

    candidates, source_stats, removed = build_entries(cfg)
    if not candidates:
        log("[错误] 没有可用频道，保留上一次的输出文件不动")
        return 1

    hcfg = cfg.get("health") or {}
    pool = build_pool(candidates, cfg)
    pool_path = "output/.pool.json"
    os.makedirs("output", exist_ok=True)
    with open(pool_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump([{"key": c["key"], "name": c["name"], "url": c["url"]} for c in pool],
                  f, ensure_ascii=False, indent=1)
    log(f"[体检] 待测地址池 {len(pool)} 条 -> {pool_path}")

    if args.pool_only:
        return 0

    health_results, health_summary = {}, None
    if hcfg.get("enabled", True) and not args.no_health:
        try:
            from health import check_and_save  # 同目录模块
            health_results, health_summary = check_and_save([c["url"] for c in pool], hcfg)
            summary_path = hcfg.get("summary", "output/health-summary.json")
            os.makedirs(os.path.dirname(summary_path) or ".", exist_ok=True)
            with open(summary_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"checked_at": now_cn(), **health_summary}, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001 体检失败不影响出列表
            log(f"[体检] 失败，本次不做线路淘汰: {e}")
    elif not hcfg.get("enabled", True):
        log("[体检] 配置里已关闭")

    entries, sel_stats = select_entries(candidates, cfg, health_results)
    if not entries:
        log("[错误] 体检后没有可用频道，本次不覆盖输出")
        return 1

    catalog = {}
    try:
        catalog = build_channel_catalog(cfg, cfg["filters"])
    except Exception as e:  # noqa: BLE001 官方清单拉取失败不影响主列表
        log(f"[清单] 官方频道包拉取失败: {e}")

    groups = {}
    for e in entries:
        groups[e["group"]] = groups.get(e["group"], 0) + 1

    logo_index = load_logo_index(cfg)
    auto_logos = {}
    auto_path = (cfg.get("logos") or {}).get("auto_index", "data/auto-logos.json")
    if os.path.exists(auto_path):
        try:
            with open(auto_path, encoding="utf-8") as f:
                auto_logos = json.load(f)
        except (OSError, ValueError):
            auto_logos = {}
    with_logo = write_m3u(entries, cfg, cfg["output"]["m3u"], logo_index, auto_logos)
    log(f"[台标] {len(entries)} 条中有台标 {with_logo} 条（含自制 {len(auto_logos)} 个）")
    if cfg["output"].get("txt"):
        write_txt(entries, cfg["output"]["txt"],
                  multi_url=bool(cfg["output"].get("txt_multi_url", True)))

    # 附加精简版：按分组白名单各切一份（例如只要央视 + 卫视）
    variant_stats = {}
    for v in (cfg["output"].get("variants") or []):
        if not v.get("enabled", True):
            continue
        allow = set(v.get("groups") or [])
        name_re = re.compile(v["name_regex"]) if v.get("name_regex") else None
        sub = [e for e in entries
               if e["group"] in allow
               or (name_re and name_re.search(e["name"] + " " + e.get("tvg_name", "")))]
        vname = v.get("name") or "variant"
        if not sub:
            log(f"[精简] {vname} 没有匹配频道，跳过")
            continue
        v_m3u = v.get("m3u") or f"output/{vname}.m3u"
        write_m3u(sub, cfg, v_m3u, logo_index, auto_logos)
        if v.get("txt"):
            write_txt(sub, v["txt"], multi_url=bool(cfg["output"].get("txt_multi_url", True)))
        chans = len({e["key"] for e in sub})
        variant_stats[vname] = {"channels": chans, "lines": len(sub), "groups": sorted(allow),
                                "m3u": v_m3u, "txt": v.get("txt")}
        log(f"[精简] {vname}：{chans} 个频道 / {len(sub)} 条线路 -> {v_m3u}")

    stats = {
        "updated_at": now_cn(),
        "total_channels": len(entries),
        "total_lines": sum(len(v) for v in candidates.values()),
        "removed_promo": removed["promo"],
        "removed_bad_url": removed["bad_url"],
        "selection": sel_stats,
        "logos": {"channels_with_logo": with_logo, "logo_index": len(logo_index)},
        "health": health_summary,
        "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])),
        "variants": variant_stats,
        "sources": source_stats,
        "catalog": {k: catalog.get(k) for k in ("total_raw", "total_kept", "removed_promo", "groups")} if catalog else None,
    }
    with open(cfg["output"]["stats"], "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    log(f"[输出] 共 {len(entries)} 个频道条目 -> {cfg['output']['m3u']}")
    for g, n in stats["groups"].items():
        log(f"       {g}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
