# 直播源每日自动清洗

自动从多个公开直播源下载频道列表，**删除购物 / 推广 / 广告频道**，同名频道只保留最优质的一条线路，每天定时输出一份与 [fanmingming](https://github.com/fanmingming/live) 完全同格式的 M3U。

生成的播放列表可以直接填进任何支持 M3U 的播放器（电视直播、VsTV、Televizo、Kodi、DIYP、影视仓等）。

## 为什么需要它

电视盒子上的直播 App 经常把购物台、导视台、"精选推荐" 混进央视频道列表里，还会在启动时强制从服务器同步回来，本地删了也没用。这个项目把"干净列表"放在云端，每天重新生成，App 只要订阅这个地址就行——不需要改固件、不需要 Root、不需要常开的电脑。

## 使用

播放列表地址（把 `<用户名>` `<仓库名>` 换成你自己的）：

| 用途 | 地址 |
| --- | --- |
| jsDelivr（国内较快，有缓存） | `https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/output/live.m3u` |
| GitHub 源站 | `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/live.m3u` |
| DIYP / 影视仓 txt 格式 | `https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/output/live.txt` |

> jsDelivr 会缓存数小时，想要"完全实时"就用源站地址。

## 它做了什么

1. **聚合**：并发从 `config.json` 里配置的多个上游源拉取（支持 m3u 和 `分组,#genre#` 两种格式，GitHub raw 地址自动走 jsDelivr / ghproxy 镜像兜底）。
2. **去推广**：按关键词（购物、推广、好物、欢乐购、导视、精选、秒杀、优惠、带货、试用……）过滤频道名、分组名；`Undefined`、`Shop`、`Religious` 这类无意义分组整组丢弃。
3. **归一化**：`CCTV-1综合` / `CCTV1高清` / `cctv-1` 统一识别为同一频道；英文分组翻译成中文分组。
4. **选优**：同一频道有多条线路时，按「源优先级 → 画质（4K > 蓝光 > 1080 > 超清 > 高清）」选一条，URL 去重。
5. **输出**：按 fanmingming 的格式写出 M3U，附带台标和 EPG 信息。

输出格式示例：

```m3u
#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"
#EXTINF:-1 tvg-name="CCTV1" tvg-logo="https://live.fanmingming.cn/tv/CCTV1.png" group-title="央视频道",CCTV-1综合
http://.../1.m3u8
```

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
| `output.max_per_channel` | 每个频道保留几条线路，默认 1 |
| `output.epg` | 写入 M3U 头部的 EPG 地址 |
| `filters.allowed_schemes` | 允许的协议，默认只留 `http`/`https`（组播 `rtp://`/`udp://` 绝大多数播放器用不了） |
| `fengcai.enabled` | 是否每天抓官方频道清单一并输出 |

新增一个源，只要往 `sources` 里加一条，提交后 Action 会自动跑一次。

## 本地运行

```bash
python scripts/build.py
```

只用 Python 标准库，不需要装任何依赖。输出在 `output/` 下。

## 说明

- 项目只做列表的抓取、过滤和重新排版，**不存储、不转播任何视频流**，播放地址均来自第三方公开列表。
- 直播源随时可能失效，因此才需要每天重新生成；如果某个频道放不出来，通常是上游线路挂了，等下一次更新或换 `priority` 更高的源。
- 上游列表的版权归各自作者所有，本项目仅供个人学习与自用。
