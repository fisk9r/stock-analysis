"""多源行情交叉校验：东方财富(延迟) / 新浪 / 腾讯。

目的：东方财富偶有报价错误，用另外两个独立源交叉验证。
- 同一只股票同时取三源最新价；
- 若 ≥2 源可用，比较价差，差异过大（>0.5%）标“数据存疑”，
  并以多源中位数作为权威价。
零依赖（仅标准库 + 复用 pipeline.em_api 的东财通道）。
"""
import sys
import os
import json
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from pipeline import em_api  # noqa: E402
except ImportError:
    import em_api  # noqa: E402

UA = "Mozilla/5.0"
SINA_REF = "https://finance.sina.com.cn"
SPREAD_THRESHOLD = 0.5  # 多源价差超过 0.5% 视为“数据存疑”


def _prefix(code):
    c = (code or "").strip()
    if not c:
        return None
    if c[0] in "68":      # 沪市主板 / 科创板(688)
        return "sh"
    if c[0] in "48":      # 北交所(4/8)
        return "bj"
    return "sz"           # 深市(0/2/3) 等


def _market_digit(code):
    return "1" if _prefix(code) == "sh" else "0"


def _f(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except Exception:
        return None


def _pct(price, prev):
    price = _f(price)
    prev = _f(prev)
    if price is None or prev in (None, 0):
        return None
    return round((price - prev) / prev * 100.0, 2)


# ---------------- 东方财富（延迟通道，海外可达） ----------------
def em_quote(code):
    try:
        secid = "%s.%s" % (_market_digit(code), code)
        d = em_api.push2_json(
            "/api/qt/stock/get?secid=%s&fields=f43,f57,f58,f170&fltt=2&_=%d"
            % (secid, em_api._ts())
        )
        if not d:
            return None
        dd = (d.get("data") or {}) if isinstance(d, dict) else {}
        if not dd:
            return None
        return {"price": _f(dd.get("f43")), "pct": _f(dd.get("f170")), "name": dd.get("f58")}
    except Exception:
        return None


# ---------------- 新浪 ----------------
def sina_quote(code):
    pre = _prefix(code)
    if not pre:
        return None
    try:
        url = "https://hq.sinajs.cn/list=%s%s" % (pre, code)
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": SINA_REF}
        )
        s = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
        inner = s.split('"')[1]
        p = inner.split(",")
        if len(p) < 4:
            return None
        price = _f(p[3])
        prev = _f(p[2])
        return {"price": price, "pct": _pct(price, prev), "name": p[0]}
    except Exception:
        return None


# ---------------- 腾讯 ----------------
def tencent_quote(code):
    pre = _prefix(code)
    if not pre:
        return None
    try:
        url = "https://qt.gtimg.cn/q=%s%s" % (pre, code)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        s = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
        inner = s.split('"')[1]
        q = inner.split("~")
        if len(q) < 5:
            return None
        price = _f(q[3])
        prev = _f(q[4])
        return {"price": price, "pct": _pct(price, prev), "name": q[1]}
    except Exception:
        return None


def _one(code):
    em = em_quote(code)
    sina = sina_quote(code)
    ten = tencent_quote(code)
    prices = {}
    if em:
        prices["em"] = em["price"]
    if sina:
        prices["sina"] = sina["price"]
    if ten:
        prices["tencent"] = ten["price"]
    vals = [v for v in prices.values() if v is not None]
    item = {"code": code, "prices": prices, "flag": False}
    if len(vals) >= 2:
        svals = sorted(vals)
        median = svals[len(svals) // 2]
        spread = (max(vals) - min(vals)) / median * 100.0 if median else 0.0
        item["median"] = median
        item["spread_pct"] = round(spread, 3)
        item["authoritative"] = median
        item["flag"] = spread > SPREAD_THRESHOLD
    elif vals:
        item["authoritative"] = vals[0]
    return item


def cross_check(codes, sample=60, workers=8, timeout=10):
    """对给定股票代码做三源交叉校验。返回汇总 + 逐标的明细。"""
    seen = []
    for c in codes:
        if c and c not in seen:
            seen.append(c)
    if sample and len(seen) > sample:
        seen = seen[:sample]
    items = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for it in ex.map(_one, seen, timeout=timeout + 5):
                items.append(it)
    except Exception:
        pass
    flagged = [it for it in items if it.get("flag")]
    return {
        "checked": len(items),
        "with_data": sum(1 for it in items if it["prices"]),
        "flagged": flagged,
        "items": items,
        "threshold": SPREAD_THRESHOLD,
    }


def quality_for_data(data, sample=60):
    """从分析产物中抽取头条标的代码，做交叉校验，并把结果挂回数据。

    返回 (data_quality 区块, code->quality 映射)。
    网络异常时返回 skipped 标记，不影响主流程。
    """
    codes = []
    try:
        for it in (data.get("limit_ups") or []):
            if it.get("code"):
                codes.append(it["code"])
        for it in (data.get("recommend", {}).get("all") or []):
            if it.get("code"):
                codes.append(it["code"])
        for it in (data.get("demons") or []):
            if it.get("code"):
                codes.append(it["code"])
    except Exception:
        pass

    if os.environ.get("NO_XCHECK"):
        return {"skipped": True, "reason": "NO_XCHECK"}, {}

    try:
        res = cross_check(codes, sample=sample)
    except Exception as e:
        return {"skipped": True, "reason": repr(e)[:120]}, {}

    # 构造 code -> 精简质量字典，供逐标的注入
    qmap = {}
    for it in res.get("items", []):
        p = it.get("prices", {})
        qmap[it["code"]] = {
            "em": p.get("em"),
            "sina": p.get("sina"),
            "tencent": p.get("tencent"),
            "median": it.get("median"),
            "spread_pct": it.get("spread_pct"),
            "flag": it.get("flag", False),
        }
    block = {
        "skipped": False,
        "sources": ["东方财富(延迟)", "新浪", "腾讯"],
        "checked": res["checked"],
        "with_data": res["with_data"],
        "threshold_pct": res["threshold"],
        "flagged_count": len(res["flagged"]),
        "flagged": [
            {
                "code": f["code"],
                "prices": f["prices"],
                "spread_pct": f.get("spread_pct"),
            }
            for f in res["flagged"]
        ],
    }
    return block, qmap


# 供命令行快速验证
if __name__ == "__main__":
    import time
    t0 = time.time()
    r = cross_check(["600519", "000001", "300750", "601318"], sample=4)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("耗时 %.1fs" % (time.time() - t0))
