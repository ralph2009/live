#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播地址健康检查
----------------
对聚合出来的每一条直播地址做一次真实探测：连得上、返回的是播放列表/视频流，才算"能用"。

判定结果分三档，目的是"宁可留着也别误杀"：

* ``ok``      —— 服务器有响应，且返回内容像播放列表（#EXTM3U / #EXTINF）或 TS 流
* ``bad``     —— 明确失效：域名解析不了、HTTP 404/410/451，这种必然放不出来
* ``unknown`` —— 连不上或超时（多为运营商内网限制、地域限制、IPv6 不可达），
                 本地播放器仍可能正常播放，所以默认保留，只排在最后

只用 Python 标准库，方便在 GitHub Actions 里零依赖运行。
"""

import concurrent.futures
import json
import os
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_REDIRECT = 2
READ_BYTES = 4096

# IPv6 探测用的公网地址（阿里 DNS），连不上就认为当前环境没有 IPv6
IPV6_PROBE_HOST = ("2400:3200::1", 80)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 地址解析

def parse_url(url):
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    host = parts.hostname or ""
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


def is_ipv6_host(host: str) -> bool:
    return ":" in host


# ---------------------------------------------------------------- 单条探测

def _recv_head(sock, deadline):
    """读到响应头结束，尽量多读一点 body 用于判断内容类型。"""
    buf = b""
    while len(buf) < READ_BYTES:
        remain = deadline - time.time()
        if remain <= 0:
            break
        sock.settimeout(max(0.2, remain))
        try:
            chunk = sock.recv(READ_BYTES - len(buf))
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf:
            head, _, body = buf.partition(b"\r\n\r\n")
            if len(body) >= 512 or b"Content-Length" in head.lower():
                break
    return buf


def _classify(body: bytes, headers: bytes, code: int):
    head = headers.lower()
    if code in (200, 206):
        low = body[:2048].lower()
        if b"#extm3u" in low or b"#extinf" in low:
            return "ok", "播放列表"
        if body[:1] == b"\x47":  # MPEG-TS 包头
            return "ok", "TS流"
        if b".m3u8" in low or b".ts" in low:
            return "ok", "跳转地址"
        for token in (b"mpegurl", b"mp2t", b"video/", b"octet-stream"):
            if token in head:
                return "ok", "视频响应"
        return "unknown", "响应内容未知"
    if code in (301, 302, 303, 307, 308):
        return "redirect", ""
    if code in (404, 410, 451):
        return "bad", f"HTTP {code}"
    if code in (401, 403):
        return "unknown", f"HTTP {code} 需鉴权/限地域"
    if code >= 500:
        return "unknown", f"HTTP {code} 服务端异常"
    return "unknown", f"HTTP {code}"


def probe_once(url, connect_timeout=4.0, read_timeout=5.0, redirects=0):
    scheme, host, port, path = parse_url(url)
    if scheme not in ("http", "https") or not host:
        return {"status": "bad", "reason": "地址格式无效", "code": 0, "ms": 0}

    started = time.time()
    host_header = f"[{host}]" if is_ipv6_host(host) else host
    if (scheme == "https" and port != 443) or (scheme == "http" and port != 80):
        host_header += f":{port}"

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"User-Agent: {UA}\r\n"
        "Accept: */*\r\n"
        "Accept-Encoding: identity\r\n"
        "Range: bytes=0-4095\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1", "ignore")

    sock = None
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        if not infos:
            return {"status": "bad", "reason": "域名无解析结果", "code": 0, "ms": int((time.time() - started) * 1000)}
        family, socktype, proto, _canon, addr = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(connect_timeout)
        sock.connect(addr)
        if scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall(request)
        raw = _recv_head(sock, time.time() + read_timeout)
    except socket.gaierror as e:
        return {"status": "bad", "reason": f"DNS 解析失败({e.errno})", "code": 0, "ms": int((time.time() - started) * 1000)}
    except (socket.timeout, TimeoutError):
        return {"status": "unknown", "reason": "连接超时", "code": 0, "ms": int((time.time() - started) * 1000)}
    except ConnectionRefusedError:
        return {"status": "unknown", "reason": "端口拒绝连接", "code": 0, "ms": int((time.time() - started) * 1000)}
    except OSError as e:
        msg = str(e) or e.__class__.__name__
        if "Network is unreachable" in msg or "unreachable" in msg.lower():
            return {"status": "unknown", "reason": "网络不可达", "code": 0, "ms": int((time.time() - started) * 1000)}
        return {"status": "unknown", "reason": msg[:80], "code": 0, "ms": int((time.time() - started) * 1000)}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    ms = int((time.time() - started) * 1000)
    if not raw:
        return {"status": "unknown", "reason": "无响应", "code": 0, "ms": ms}

    head, _, body = raw.partition(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
    try:
        code = int(first.split()[1])
    except (IndexError, ValueError):
        return {"status": "unknown", "reason": "响应非 HTTP", "code": 0, "ms": ms}

    status, reason = _classify(body, head, code)

    if status == "redirect":
        if redirects >= MAX_REDIRECT:
            return {"status": "unknown", "reason": "重定向过多", "code": code, "ms": ms}
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"location:"):
                target = urljoin(url, line.split(b":", 1)[1].strip().decode("latin-1", "ignore"))
                if target.lower().startswith(("http://", "https://")):
                    return probe_once(target, connect_timeout, read_timeout, redirects + 1)
                break
        return {"status": "unknown", "reason": "重定向无目标", "code": code, "ms": ms}

    return {"status": status, "reason": reason, "code": code, "ms": ms}


# ---------------------------------------------------------------- 批量调度

def ipv6_available(timeout=3.0) -> bool:
    if not socket.has_ipv6:
        return False
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(IPV6_PROBE_HOST)
        s.close()
        return True
    except OSError:
        return False


def check_urls(urls, cfg=None, progress_every=300):
    """并发探测一批地址，返回 {url: result}。"""
    cfg = cfg or {}
    workers = int(cfg.get("concurrency", 64))
    ctimeout = float(cfg.get("connect_timeout", 4))
    rtimeout = float(cfg.get("read_timeout", 5))
    max_seconds = float(cfg.get("max_seconds", 1200))

    urls = list(dict.fromkeys(u for u in urls if u))
    v6_ok = ipv6_available()
    skip_v6 = bool(cfg.get("skip_ipv6_without_network", True)) and not v6_ok
    log(f"[体检] 待测 {len(urls)} 条地址，并发 {workers}，IPv6 环境：{'有' if v6_ok else '无'}")

    results = {}
    todo = []
    for u in urls:
        if skip_v6 and is_ipv6_host(parse_url(u)[1]):
            results[u] = {"status": "unknown", "reason": "当前环境无 IPv6，未测试", "code": 0, "ms": 0}
        else:
            todo.append(u)

    deadline = time.time() + max_seconds
    lock = threading.Lock()
    done = [0]

    def work(u):
        if time.time() > deadline:
            return u, {"status": "unknown", "reason": "超出单次体检时间预算", "code": 0, "ms": 0}
        return u, probe_once(u, ctimeout, rtimeout)

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, u) for u in todo]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    u, res = fut.result()
                except Exception as e:  # noqa: BLE001 单条异常不能影响整体
                    continue
                with lock:
                    results[u] = res
                    done[0] += 1
                    if progress_every and done[0] % progress_every == 0:
                        log(f"[体检] 已完成 {done[0]}/{len(todo)}")

    stats = {"ok": 0, "bad": 0, "unknown": 0}
    for r in results.values():
        stats[r["status"]] = stats.get(r["status"], 0) + 1
    log(f"[体检] 可用 {stats['ok']}，失效 {stats['bad']}，未确认 {stats['unknown']}")

    return results, {
        "total": len(results),
        "ok": stats["ok"],
        "bad": stats["bad"],
        "unknown": stats["unknown"],
        "ipv6_available": v6_ok,
        "ipv6_skipped": sum(1 for u in urls if skip_v6 and is_ipv6_host(parse_url(u)[1])),
    }


def check_and_save(urls, cfg):
    results, summary = check_urls(urls, cfg)
    out = cfg.get("output", "output/health.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "summary": summary, "results": results},
                  f, ensure_ascii=False, indent=1)
    return results, summary


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    pool_path = "output/.pool.json"
    if not os.path.exists(pool_path):
        log("[体检] 没有 output/.pool.json，请先运行 python scripts/build.py --pool-only")
        return 1
    with open(pool_path, encoding="utf-8") as f:
        pool = json.load(f)
    results, summary = check_and_save([e["url"] for e in pool], cfg.get("health", {}))
    log(f"[体检] 结果已写入 {cfg.get('health', {}).get('output', 'output/health.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
