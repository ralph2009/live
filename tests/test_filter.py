#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清洗规则的自检用例：python -m unittest discover -s tests"""

import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("build", os.path.join(ROOT, "scripts", "build.py"))
build = importlib.util.module_from_spec(spec)
sys.modules["build"] = build
spec.loader.exec_module(build)

FILTERS = {
    "promo_keywords": ["购物", "推广", "好物", "导视", "精选", "欢乐购", "秒杀", "带货"],
    "blocked_groups": ["更新时间", "测试", "广告"],
    "blocked_name_patterns": [r"^\s*$", r"^\d+$"],
    "keep_foreign": True,
}


def make_entry(name, group="央视频道", tvg_name=""):
    return {"name": name, "group": group, "tvg_name": tvg_name, "url": "http://x/1.m3u8"}


class TestPromoFilter(unittest.TestCase):
    def test_promo_names_removed(self):
        for name in ["导视精选", "欢乐购", "居家好物", "随心看推广", "秒杀专场", "购物频道"]:
            self.assertTrue(build.is_promo(make_entry(name), FILTERS), name)

    def test_normal_channels_kept(self):
        for name in ["CCTV-1综合", "湖南卫视", "CCTV-5体育", "北京卫视", "CHC动作电影"]:
            self.assertFalse(build.is_promo(make_entry(name), FILTERS), name)

    def test_blocked_group(self):
        self.assertTrue(build.is_promo(make_entry("某频道", group="更新时间"), FILTERS))

    def test_empty_name(self):
        self.assertTrue(build.is_promo(make_entry("   "), FILTERS))


class TestChannelKey(unittest.TestCase):
    def test_cctv_variants_merge(self):
        keys = {
            build.channel_key("CCTV-1综合"),
            build.channel_key("CCTV1"),
            build.channel_key("cctv-1 高清"),
            build.channel_key("CCTV-1综合高清"),
            build.channel_key("", "CCTV1"),
        }
        self.assertEqual(keys, {"CCTV1"})

    def test_cctv5_plus(self):
        self.assertEqual(build.channel_key("CCTV-5+体育赛事"), "CCTV5+")

    def test_satellite(self):
        self.assertEqual(build.channel_key("湖南卫视高清"), "湖南卫视")
        self.assertEqual(build.channel_key("湖南卫视"), "湖南卫视")


class TestGroupNormalize(unittest.TestCase):
    def test_english_groups(self):
        self.assertEqual(build.normalize_group("Movies", "x"), "电影频道")
        self.assertEqual(build.normalize_group("Sports;HD", "x"), "体育频道")

    def test_chinese_groups(self):
        self.assertEqual(build.normalize_group("央视频道", "x"), "央视频道")
        self.assertEqual(build.normalize_group("卫视频道", "x"), "卫视频道")

    def test_junk_groups_dropped(self):
        for g in ["Undefined", "Shop", "Religious", "Shopping"]:
            self.assertTrue(build.is_dropped_group(build.normalize_group(g, "x")), g)

    def test_quality_rank(self):
        self.assertGreater(build.quality_score("CCTV1 4K"), build.quality_score("CCTV1 1080"))
        self.assertGreater(build.quality_score("CCTV1 1080"), build.quality_score("CCTV1 高清"))
        self.assertGreater(build.quality_score("CCTV1 高清"), build.quality_score("CCTV1 标清"))


class TestParsers(unittest.TestCase):
    def test_parse_m3u(self):
        text = ('#EXTM3U\n'
                '#EXTINF:-1 tvg-name="CCTV1" group-title="央视频道",CCTV-1综合\n'
                'http://a/1.m3u8\n')
        rows = build.parse_m3u(text, {"name": "t", "priority": 1})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "http://a/1.m3u8")
        self.assertEqual(rows[0]["group"], "央视频道")

    def test_parse_txt(self):
        text = "央视频道,#genre#\nCCTV-1,http://a/1.m3u8\nCCTV-2,http://a/2.m3u8\n"
        rows = build.parse_txt(text, {"name": "t", "priority": 1})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["group"], "央视频道")

    def test_parse_txt_strips_line_tag(self):
        text = ("港·澳·台,#genre#\n"
                "凤凰资讯,http://e2.woc.iiiya.cn:28888/rtp/239.61.5.47:1025$LR•IPV4『线路1』\n"
                "凤凰资讯,http://php.jdshipin.com/TVOD/iptv.php?id=fhzx$LR•IPV4『线路2』\n")
        rows = build.parse_txt(text, {"name": "t", "priority": 3})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["url"], "http://php.jdshipin.com/TVOD/iptv.php?id=fhzx")
        self.assertEqual(rows[0]["group"], "港·澳·台")

    def test_clean_url(self):
        self.assertEqual(build.clean_url("http://a/b.m3u8$线路2"), "http://a/b.m3u8")
        self.assertEqual(build.clean_url(" http://a/b.m3u8 "), "http://a/b.m3u8")


def _cand(name, url, priority=3, quality=""):
    key = build.channel_key(name)
    return {"key": key, "name": name + quality, "url": url, "group": "央视频道",
            "tvg_name": "", "tvg_id": "", "logo": "", "priority": priority}


class TestMultiSource(unittest.TestCase):
    """一个频道多线路：体检结果决定先后，失效的被淘汰。"""

    def cfg(self, max_per=3):
        return {"output": {"max_per_channel": max_per}, "health": {"drop_bad": True}}

    def test_ok_first_then_unknown(self):
        cands = {"CCTV1": [
            _cand("CCTV-1", "http://dead/1.m3u8", 1),
            _cand("CCTV-1", "http://unknown/1.m3u8", 2),
            _cand("CCTV-1", "http://live/1.m3u8", 9),
        ]}
        health = {
            "http://dead/1.m3u8": {"status": "bad"},
            "http://unknown/1.m3u8": {"status": "unknown"},
            "http://live/1.m3u8": {"status": "ok"},
        }
        entries, stats = build.select_entries(cands, self.cfg(), health)
        self.assertEqual([e["url"] for e in entries],
                         ["http://live/1.m3u8", "http://unknown/1.m3u8"])
        self.assertEqual(stats["bad_lines_dropped"], 1)

    def test_max_per_channel_cap(self):
        cands = {"CCTV1": [_cand("CCTV-1", f"http://h{i}/1.m3u8") for i in range(5)]}
        entries, _ = build.select_entries(cands, self.cfg(max_per=3), {})
        self.assertEqual(len(entries), 3)

    def test_all_bad_channel_dropped(self):
        cands = {"CCTV1": [_cand("CCTV-1", "http://dead/1.m3u8")]}
        health = {"http://dead/1.m3u8": {"status": "bad"}}
        entries, stats = build.select_entries(cands, self.cfg(), health)
        self.assertEqual(entries, [])
        self.assertEqual(stats["channels_dropped"], 1)

    def test_pool_prefers_distinct_hosts(self):
        cands = {"CCTV1": [_cand("CCTV-1", "http://a.com/1.m3u8"),
                           _cand("CCTV-1", "http://a.com/2.m3u8"),
                           _cand("CCTV-1", "http://b.com/1.m3u8")]}
        pool = build.build_pool(cands, {"health": {"test_per_channel": 2}})
        hosts = {c["url"].split("/")[2] for c in pool}
        self.assertEqual(hosts, {"a.com", "b.com"})


class TestHealthClassify(unittest.TestCase):
    def setUp(self):
        import importlib.util as iu
        spec = iu.spec_from_file_location("health", os.path.join(ROOT, "scripts", "health.py"))
        self.health = iu.module_from_spec(spec)
        spec.loader.exec_module(self.health)

    def test_m3u8_body_is_ok(self):
        status, _ = self.health._classify(b"#EXTM3U\n#EXTINF:-1,x\nhttp://a/1.ts\n", b"", 200)
        self.assertEqual(status, "ok")

    def test_ts_packet_is_ok(self):
        status, _ = self.health._classify(b"\x47" + b"\x00" * 187, b"", 200)
        self.assertEqual(status, "ok")

    def test_404_is_bad(self):
        status, _ = self.health._classify(b"not found", b"", 404)
        self.assertEqual(status, "bad")

    def test_403_is_unknown(self):
        status, _ = self.health._classify(b"", b"", 403)
        self.assertEqual(status, "unknown")

    def test_html_is_unknown(self):
        status, _ = self.health._classify(b"<html><body>hello</body></html>", b"", 200)
        self.assertEqual(status, "unknown")


if __name__ == "__main__":
    unittest.main()
