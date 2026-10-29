# 直播源每日自动清洗

自动从多个公开直播源下载频道列表，**删除购物 / 推广 / 广告频道**，同一个频道保留多条可用线路，并对每条地址做一次真实探测，每天定时输出一份与 [fanmingming](https://github.com/fanmingming/live) 完全同格式的 M3U。

生成的播放列表可以直接填进任何支持 M3U 的播放器（电视直播、VsTV、Televizo、Kodi、DIYP、影视仓等）。

## 为什么需要它

电视盒子上的直播 App 经常把购物台、导视台、"精选推荐" 混进央视频道列表里，还会在启动时强制从服务器同步回来，本地删了也没用。这个项目把"干净列表"放在云端，每天重新生成，App 只要订阅这个地址就行——不需要改固件、不需要 Root、不需要常开的电脑。

## 使用

播放列表地址：

| 用途 | 地址 |
| --- | --- |
| 全部频道 · jsDelivr（国内较快，有缓存） | `https://cdn.jsdelivr.net/gh/ralph2009/live@main/output/live.m3u` |
| 全部频道 · fastly 节点（部分网络更通） | `https://fastly.jsdelivr.net/gh/ralph2009/live@main/output/live.m3u` |
| 全部频道 · GitHub 源站 | `https://raw.githubusercontent.com/ralph2009/live/main/output/live.m3u` |
| 全部频道 · DIYP / 影视仓 txt | `https://cdn.jsdelivr.net/gh/ralph2009/live@main/output/live.txt` |
| **只要央视 + 卫视** · m3u | `https://cdn.jsdelivr.net/gh/ralph2009/live@main/output/cctv-sat.m3u` |
| **只要央视 + 卫视** · fastly 节点 | `https://fastly.jsdelivr.net/gh/ralph2009/live@main/output/cctv-sat.m3u` |
| **只要央视 + 卫视** · txt | `https://cdn.jsdelivr.net/gh/ralph2009/live@main/output/cctv-sat.txt` |

> `cctv-sat` 只保留 `央视频道`（CCTV-1~17、CCTV-4K/8K、CETV、CGTN、央视付费频道）和 `卫视频道`（各省卫视 + 中国教育台）两组，约 90 个台、1200 条线路，不含地方台和港澳台。和主列表同一份数据、同一次体检，每天一起更新。

> jsDelivr 会缓存数小时，想要"完全实时"就用源站地址。

## 它做了什么

1. **聚合**：并发从 `config.json` 里配置的多个上游源拉取（支持 m3u 和 `分组,#genre#` 两种格式，GitHub raw 地址自动走 jsDelivr / ghproxy 镜像兜底）。目前包含 `vbskycn/iptv`、`Guovin/iptv-api`、`ralph2009/ttvv`（IPTV.txt，4000+ 条线路，含大量地方台和港澳台）、`fanmingming` IPv6、`yuechan/Live`。
2. **去推广**：按关键词（购物、推广、好物、欢乐购、导视、精选、秒杀、优惠、带货、试用、支持作者……）过滤频道名、分组名；`Undefined`、`Shop`、`Religious` 这类无意义分组整组丢弃；`/rtp/239.x.x.x:1025` 这种运营商内网组播代理地址也直接丢掉。
3. **归一化**：`CCTV-1综合` / `CCTV1高清` / `cctv-1` 统一识别为同一频道，显示名统一成 `CCTV-1`；`上海｜东方卫视` 这种带省份前缀的写法会自动并到 `东方卫视`；英文分组翻译成中文分组，省名分组归入"地方频道"。
4. **多线路**：同一个频道的所有线路全部保留（`max_per_channel` 设为 `0` 不限），地址去重，能做多少条就做多少条。txt 输出用 `#` 连接，播放器可自动切线路；m3u 则是同名的多条 `#EXTINF`。
5. **体检**：每次更新都对地址池里的每条地址发起一次真实请求（见下）。
6. **输出**：按 fanmingming 的格式写出 M3U，附带台标和 EPG 信息。

## 地址体检（每次更新都会跑）

`scripts/health.py` 会并发探测每条地址（默认 64 线程、连接 4 秒、读取 5 秒超时），判定分三档：

| 结果 | 含义 | 处理 |
| --- | --- | --- |
| `ok` | 返回 `#EXTM3U` / `#EXTINF` 播放列表，或 TS 流 | 排最前面 |
| `bad` | DNS 解析不了、HTTP 404 / 410 / 451 | 直接丢弃（`drop_bad` 可关） |
| `unknown` | 超时、连接被拒、403 限地域、无 IPv6 环境等 | 保留，排在 `ok` 之后 |

只丢弃"明确已死"的地址，超时一律保留——因为大量直播源是运营商内网/限地域的，GitHub Actions 的海外 IP 连不上不代表你家里放不出来。整台频道所有线路都 `bad` 时，这个频道才会被去掉。

体检结果：

- `output/health-summary.json`：总数、可用/失效/未确认的数量、本次是否有 IPv6（会提交进仓库，Actions 运行摘要里也能看到）
- `output/health.json`：逐条明细（本地中间产物，不进仓库）

> 注意：GitHub Actions 的机器没有 IPv6，所以带 IPv6 的地址（目前约 1100 条三大运营商 `[2409:...]` 源）在那里一律算"未测试"。这些源在你家宽带（尤其移动）上往往能直接播，所以默认保留而不是删除。

输出格式示例：

```m3u
#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"
#EXTINF:-1 tvg-id="CCTV-1" tvg-name="CCTV-1" tvg-logo="https://live.fanmingming.cn/tv/CCTV1.png" group-title="央视频道",CCTV-1
http://.../1.m3u8
```

同一频道有多条线路时，就重复出现多条同名 `#EXTINF`，播放器切下一个源即可。

## 定时任务

`.github/workflows/update.yml` 里默认每天北京时间 04:00 跑一次：

```yaml
schedule:
  - cron: "0 20 * * *"   # UTC 20:00 = 北京时间次日 04:00
```

想改频率（例如每 6 小时一次，直播源失效快的时候更实用）：

```yaml
  - cron: "0 */6 * * *"
```

也可以在 Actions 页面点 **Run workflow** 手动立刻跑一次。每次运行结果会写进 `output/stats.json`，同时显示在 Actions 的运行摘要里。

## 附带：官方频道清单（去推广）

除了可播放的 M3U，每天还会抓一份「高清电视」类 App 的官方频道包（`tvlive.fengcaizb.com/channels/gqds3.gz`，1759 个频道），把购物/推广台剔除后存成清单：

| 文件 | 内容 |
| --- | --- |
| `output/channel-catalog.json` | 频道名 + 分组 + 排序号，含剔除统计 |
| `output/channel-catalog.txt` | 按分组排好的人读版 |

> 该频道包里的播放地址是服务端加密令牌（`urls` 字段），只有它自己的 App 能解析，因此清单**不能转成可播放的 M3U**，仅作频道对照/整理用。想要能播的列表，用上面的 `output/live.m3u`。

## 配置

`config.json`：

| 字段 | 说明 |
| --- | --- |
| `sources[].url` | 上游列表地址，m3u 或 txt 均可 |
| `sources[].priority` | 数字越小越优先，同频道选线路时先看它 |
| `sources[].enabled` | 是否启用 |
| `filters.promo_keywords` | 出现即判为推广频道的关键词 |
| `filters.blocked_groups` | 整组丢弃的分组名 |
| `filters.blocked_url_patterns` | 地址正则黑名单，默认丢掉 `/rtp/239.x.x.x:1025` 这类内网组播代理 |
| `output.max_per_channel` | 每个频道最多保留几条线路，`0` 表示不限（默认 0，有几条留几条） |
| `output.txt_multi_url` | txt 里同频道多条线路用 `#` 连接（DIYP 标准写法），默认开 |
| `output.epg` | 写入 M3U 头部的 EPG 地址 |
| `output.variants[]` | 精简版输出：按 `groups` 白名单切出另一份列表（现在配的是 `cctv-sat` = 央视频道 + 卫视频道），可选 `name_regex` 兜底按台名匹配 |
| `filters.allowed_schemes` | 允许的协议，默认只留 `http`/`https`（组播 `rtp://`/`udp://` 绝大多数播放器用不了） |
| `health.enabled` | 是否在每次更新时体检地址，默认开 |
| `health.concurrency` / `connect_timeout` / `read_timeout` | 体检并发与超时，默认 64 / 4s / 5s |
| `health.test_per_channel` | 每个频道最多送检几条线路，`0` 表示全部送检（默认 0） |
| `health.max_seconds` | 单次体检总时间预算，默认 1200 秒，超时就标记为未测试 |
| `health.drop_bad` / `drop_status` | 是否丢弃明确失效（404/410/451）的地址 |
| `fengcai.enabled` | 是否每天抓官方频道清单一并输出 |

新增一个源，只要往 `sources` 里加一条，提交后 Action 会自动跑一次。

## 本地运行

```bash
python scripts/build.py              # 抓取 + 清洗 + 体检 + 输出
python scripts/build.py --no-health  # 只抓取清洗，不体检（快）
python scripts/build.py --pool-only  # 只生成待测地址池 output/.pool.json
python scripts/health.py            # 单独对地址池做一次体检
```

只用 Python 标准库，不需要装任何依赖。输出在 `output/` 下。

## 说明

- 项目只做列表的抓取、过滤和重新排版，**不存储、不转播任何视频流**，播放地址均来自第三方公开列表。
- 直播源随时可能失效，因此才需要每天重新生成；如果某个频道放不出来，通常是上游线路挂了，等下一次更新或换 `priority` 更高的源。
- 上游列表的版权归各自作者所有，本项目仅供个人学习与自用。
