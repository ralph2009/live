#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给没有现成台标的频道生成一张占位台标
--------------------------------------
台标库（fanmingming / wanglindl / samkolau 合集 2500+ 张）覆盖不到的多是县级台，
这里按频道名生成一张深色底的文字台标，风格与常见台标接近，播放器里看起来不空。

输出：
  logos/<频道名>.png      台标图片（提交进仓库，由 jsDelivr / GitHub Pages 提供）
  data/auto-logos.json    频道 -> 文件名 的索引，供 build.py 使用

依赖 Pillow（只在生成时用，日常跑 build.py 不需要）。
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from hashlib import md5

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    print("需要先安装 Pillow: pip install pillow", file=sys.stderr)
    raise

W, H = 240, 120
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

# 深色系配色，按频道名散列挑一个，保证同一频道每次颜色一致
PALETTE = [
    ((28, 38, 64), (58, 96, 168)),
    ((45, 27, 60), (128, 62, 152)),
    ((18, 48, 44), (36, 130, 110)),
    ((60, 30, 26), (176, 74, 52)),
    ((24, 34, 46), (66, 120, 168)),
    ((46, 40, 20), (156, 124, 40)),
    ((30, 30, 34), (96, 100, 112)),
    ((20, 40, 56), (44, 118, 150)),
]


def pick_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=0)
            except OSError:
                continue
    return None


def split_name(name: str):
    """把频道名拆成最多两行，去掉「频道 / 电视台 / 高清」这类尾巴。"""
    clean = re.sub(r"(高清|超清|标清|4K|8K|HD|频道|电视台|台)$", "", name.strip(), flags=re.I)
    clean = clean.strip() or name.strip()
    if len(clean) <= 6:
        return [clean]
    mid = (len(clean) + 1) // 2
    return [clean[:mid], clean[mid:]]


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "", name.strip())
    name = unicodedata.normalize("NFKC", name)
    return (name or "channel")[:40] + ".png"


def render(name: str, out_path: str, font_bold, font_small):
    seed = int(md5(name.encode("utf-8")).hexdigest()[:8], 16)
    bg, accent = PALETTE[seed % len(PALETTE)]

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # 左上角一条亮色装饰，模拟台标的高光
    d.rectangle([0, 0, W, 6], fill=accent)
    d.rectangle([0, H - 4, W, H], fill=accent)

    lines = split_name(name)
    while lines and (max(font_bold.getlength(l) for l in lines) > W - 24) and len(lines) > 1:
        # 太长就把第一行拆短一点
        first = lines[0]
        lines = [first[: max(1, len(first) - 1)]] + lines[1:]

    gap = 6
    heights = [font_bold.size if len(lines) == 1 else font_small.size] * len(lines)
    total = sum(heights) + gap * (len(lines) - 1)
    y = (H - total) / 2
    for i, line in enumerate(lines):
        font = font_bold if len(lines) == 1 else font_small
        tw = font.getlength(line)
        # 单行太长就整体缩放
        d.text(((W - tw) / 2, y), line, font=font, fill=(255, 255, 255))
        y += font.size + gap

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path, "PNG", optimize=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m3u", default="output/live.m3u")
    ap.add_argument("--outdir", default="logos")
    ap.add_argument("--index", default="data/auto-logos.json")
    ap.add_argument("--force", action="store_true", help="已有图片也重新生成")
    args = ap.parse_args()

    font_bold = pick_font(40)
    font_small = pick_font(30)
    if not font_bold:
        print("找不到中文字体，无法生成台标", file=sys.stderr)
        return 1

    # 从输出的 m3u 里找出没有台标的频道（同名频道只做一次）
    targets, seen = [], set()
    for line in open(args.m3u, encoding="utf-8"):
        if not line.startswith("#EXTINF"):
            continue
        has_logo = "tvg-logo=" in line
        name = line.rsplit(",", 1)[-1].strip()
        key = name
        if key in seen or has_logo or not name:
            continue
        seen.add(key)
        targets.append(name)

    index = {}
    if os.path.exists(args.index):
        try:
            index = json.load(open(args.index, encoding="utf-8"))
        except ValueError:
            index = {}

    made = 0
    for name in targets:
        fname = index.get(name) or safe_filename(name)
        path = os.path.join(args.outdir, fname)
        if args.force or not os.path.exists(path):
            render(name, path, font_bold, font_small)
            made += 1
        index[name] = fname

    os.makedirs(os.path.dirname(args.index) or ".", exist_ok=True)
    with open(args.index, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=0, sort_keys=True)

    print(f"[台标] 缺台标频道 {len(targets)} 个，新生成 {made} 张 -> {args.outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
