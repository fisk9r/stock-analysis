# -*- coding: utf-8 -*-
"""周末发酵要闻抓取 —— 生成 news.json，供 build.py 的周末条件推送使用。

为什么需要它：
    push_weekend() 读 news.json，没有要闻就静默跳过（这是用户要的"没有就不发"）。
    但如果 news.json 是一个静态占位文件，云端跑起来后周末推送会**永久不触发**——
    表面正常，实际功能已死。所以必须有真实新闻源。

数据源（多源互备，任一可用即可）：
    1. 东方财富快讯   np-listapi.eastmoney.com    结构最干净，showTime 已是标准格式
    2. 同花顺快讯     news.10jqka.com.cn          备源
    实测两者对境外/云出口 IP 均无封锁，与 push2 的限制无关。

关键设计 —— 相关性打分：
    快讯流里绝大多数是噪音（国际冲突、台风、楼盘广告）。周末推送只关心
    "会影响下周一 A 股的政策/产业/市场信息"，所以必须打分过滤，
    否则推送里全是"以色列总理拒绝和平计划"这种跟持仓毫无关系的内容。

用法：
    python pipeline/news_fetch.py              # 抓最近 3 天，写 news.json
    python pipeline/news_fetch.py --days 2     # 自定义窗口
    python pipeline/news_fetch.py --dry-run    # 只打印不写文件
"""
import json
import os
import re
import sys
import time
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import em_api  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "news.json")


def log(msg):
    print("[news] %s" % msg)


# --------------------------------------------------------------- 相关性词表
# 强相关：直接影响 A 股定价的政策/资金/监管信号
STRONG = [
    "证监会", "央行", "国务院", "发改委", "财政部", "工信部", "商务部", "银保监",
    "金融监管总局", "政治局", "中央经济工作会议", "国常会", "两会", "降准", "降息",
    "LPR", "MLF", "逆回购", "印花税", "IPO", "注册制", "退市", "减持", "回购",
    "北向资金", "外资", "汇金", "国家队", "养老金", "社保基金", "险资",
    "涨停", "跌停", "A股", "沪指", "创业板", "科创板", "北交所", "指数",
    "关税", "制裁", "出口管制", "国产替代", "自主可控",
]
# 中相关：产业与主题层面的催化
MEDIUM = [
    "新能源", "光伏", "储能", "锂电", "芯片", "半导体", "算力", "人工智能", "AI",
    "大模型", "机器人", "军工", "航天", "医药", "创新药", "白酒", "消费",
    "地产", "基建", "券商", "银行", "保险", "汽车", "智能驾驶", "固态电池",
    "数据中心", "低空经济", "可控核聚变", "稀土", "有色", "煤炭", "石油",
    "补贴", "招标", "中标", "订单", "扩产", "涨价", "业绩预增", "重组", "并购",
]
# 负相关：明显是噪音，直接排除
EXCLUDE = [
    "楼盘", "去化", "认购热潮", "开盘当日", "顶豪", "大宅",
    "球员", "赛事", "夺冠", "演唱会", "票房", "综艺",
    "车祸", "火灾", "地震", "台风", "暴雨", "登陆", "气象台",
    "彩票", "星座", "菜谱", "旅游攻略",
]


def score(title, summary=""):
    """给一条快讯打相关性分。分数越高越值得进周末推送。"""
    text = "%s %s" % (title or "", summary or "")
    for w in EXCLUDE:
        if w in text:
            return -1
    s = 0
    for w in STRONG:
        if w in text:
            s += 3
    for w in MEDIUM:
        if w in text:
            s += 1
    # 标题里出现的权重更高（摘要常是标题的重复展开）
    for w in STRONG:
        if w in (title or ""):
            s += 2
    return s


# --------------------------------------------------------------- 数据源
def from_eastmoney(pages=5, page_size=30):
    """东方财富 7×24 快讯。sortEnd 为游标，翻页要把上一页的值带上。"""
    out, cursor = [], ""
    for _ in range(pages):
        path = ("/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102"
                "&sortEnd=%s&pageSize=%d&req_trace=%d" % (cursor, page_size, int(time.time())))
        d = json.loads(em_api.fetch_text(
            "np-listapi.eastmoney.com", path, retry=3,
            referer="https://finance.eastmoney.com/"))
        data = (d or {}).get("data") or {}
        lst = data.get("fastNewsList") or []
        if not lst:
            break
        for it in lst:
            out.append({
                "title": (it.get("title") or "").strip(),
                "summary": re.sub(r"^【.*?】", "", (it.get("summary") or "").strip()),
                "date": it.get("showTime") or "",
                "source": "东方财富",
            })
        cursor = data.get("sortEnd") or ""
        if not cursor:
            break
    return out


def from_ths(pages=3, page_size=30):
    """同花顺快讯（备源）。ctime 是 Unix 秒。"""
    out = []
    for p in range(1, pages + 1):
        path = ("/tapp/news/push/stock/?page=%d&tag=&track=website&pagesize=%d"
                % (p, page_size))
        d = json.loads(em_api.fetch_text(
            "news.10jqka.com.cn", path, retry=3,
            referer="https://news.10jqka.com.cn/realtimenews.html"))
        lst = ((d or {}).get("data") or {}).get("list") or []
        if not lst:
            break
        for it in lst:
            ts = it.get("ctime") or it.get("rtime")
            try:
                when = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                when = ""
            out.append({
                "title": (it.get("title") or "").strip(),
                "summary": (it.get("digest") or "").strip(),
                "date": when,
                "source": "同花顺",
            })
    return out


# --------------------------------------------------------------- 主流程
def collect(days=3, min_score=3, limit=30):
    raw, errs = [], []
    for name, fn in (("东方财富", from_eastmoney), ("同花顺", from_ths)):
        try:
            got = fn()
            log("%s 拉到 %d 条" % (name, len(got)))
            raw.extend(got)
        except Exception as e:
            errs.append("%s: %r" % (name, e))
            log("%s 抓取失败：%r" % (name, repr(e)[:80]))
    if not raw:
        raise IOError("所有新闻源都不可用：%s" % "; ".join(errs))

    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    seen, scored = set(), []
    for it in raw:
        title = it["title"]
        if not title:
            continue
        key = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", title)[:24]
        if key in seen:
            continue
        seen.add(key)
        try:
            when = dt.datetime.strptime(it["date"][:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if when < cutoff:
            continue
        sc = score(title, it["summary"])
        if sc < min_score:
            continue
        scored.append((sc, when, it))

    # 先按相关性、再按时间新旧排序
    scored.sort(key=lambda x: (-x[0], -x[1].timestamp()))
    items = []
    for sc, when, it in scored[:limit]:
        items.append({"title": it["title"], "date": it["date"],
                      "summary": it["summary"][:200], "source": it["source"],
                      "score": sc})
    log("去重后 %d 条，达到相关性门槛(≥%d) %d 条" % (len(seen), min_score, len(items)))
    return items


def main():
    days = 3
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
    items = collect(days=days)
    payload = {
        "items": items,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": "近 %d 天财经要闻，按对 A 股的相关性排序，共 %d 条" % (days, len(items)),
    }
    if "--dry-run" in sys.argv:
        for it in items[:12]:
            print("  [%2d] %s  %s" % (it["score"], it["date"][5:16], it["title"][:60]))
        log("dry-run，未写文件")
        return 0
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    log("已写出 %s（%d 条）" % (os.path.relpath(OUT, ROOT), len(items)))
    for it in items[:8]:
        print("  [%2d] %s  %s" % (it["score"], it["date"][5:16], it["title"][:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
