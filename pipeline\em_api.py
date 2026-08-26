# -*- coding: utf-8 -*-
"""东方财富公开行情接口封装（纯标准库，连接复用 + 重试 + 并发）"""
import gzip
import http.client
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
UT = "7eea3edcaed734bea9cbfc24409ed989"

_local = threading.local()
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


class RateLimiter(object):
    """按 host 的自适应令牌桶：被限流自动降速，稳定后缓慢恢复"""

    def __init__(self, rate=10.0, lo=3.0, hi=20.0):
        self.rate, self.lo, self.hi = rate, lo, hi
        self.tokens = rate
        self.ts = time.time()
        self.ok_streak = 0
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                self.tokens = min(self.rate, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(min(wait, 0.5))

    def penalize(self):
        with self.lock:
            self.rate = max(self.lo, self.rate * 0.7)
            self.ok_streak = 0

    def reward(self):
        """连续成功即快速回升，避免一次抖动把速率永久压死"""
        with self.lock:
            self.ok_streak += 1
            if self.ok_streak >= 25:
                self.rate = min(self.hi, self.rate * 1.25 + 0.3)
                self.ok_streak = 0


_limiters = {}
_lim_lock = threading.Lock()


def limiter(host):
    with _lim_lock:
        if host not in _limiters:
            _limiters[host] = RateLimiter(10.0)
        return _limiters[host]


def _conn(host):
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    c = pool.get(host)
    if c is None:
        c = pool[host] = http.client.HTTPSConnection(host, timeout=20, context=_ctx)
    return c


def _drop(host):
    pool = getattr(_local, "pool", None) or {}
    c = pool.pop(host, None)
    if c:
        try:
            c.close()
        except Exception:
            pass


def fetch_text(host, path, retry=5, referer="https://quote.eastmoney.com/"):
    """GET https://{host}{path} -> str（短连接 + 令牌桶限速 + 指数退避）"""
    lim = limiter(host)
    last = None
    for i in range(retry):
        lim.acquire()
        c = None
        try:
            c = http.client.HTTPSConnection(host, timeout=20, context=_ctx)
            c.request("GET", path, headers={
                "User-Agent": UA, "Referer": referer, "Accept": "*/*",
                "Accept-Encoding": "gzip", "Connection": "close",
            })
            r = c.getresponse()
            raw = r.read()
            if r.getheader("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            if r.status != 200:
                raise IOError("HTTP %d" % r.status)
            lim.reward()
            return raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            lim.penalize()
            time.sleep(min(5.0, 0.5 * (2 ** i)) * (0.6 + random.random() * 0.8))
        finally:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
    raise last


def fetch_json(host, path, retry=5, referer="https://quote.eastmoney.com/"):
    return json.loads(fetch_text(host, path, retry, referer))


def _ts():
    return int(time.time() * 1000)


# ------------------------------------------------------- push2 主机自适应选择
# 背景：push2.eastmoney.com 对境外/云厂商出口 IP 会直接 RST 断连
#      （RemoteDisconnected，非 HTTP 错误码，重试无用）。
#      实测 push2delay.eastmoney.com 不做该限制，字段/分页/total 完全一致，
#      区别仅在盘中为延迟行情；盘后收盘数据两者相同。
# 策略：首次调用探测 push2，通则一直用；被 RST 则永久切到 push2delay。
#      可用环境变量 EM_PUSH2_HOST 强制指定，跳过探测。
PUSH2_PRIMARY = "push2.eastmoney.com"
PUSH2_FALLBACK = "push2delay.eastmoney.com"

_push2_host = None
_push2_lock = threading.Lock()


def _probe_push2():
    """轻量探测：拉 1 行数据，通则返回主域名，否则返回延迟域名"""
    path = ("/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3"
            "&fs=m:1+t:2&fields=f12&_=%d" % _ts())
    try:
        fetch_json(PUSH2_PRIMARY, path, retry=1)
        return PUSH2_PRIMARY
    except Exception:
        return PUSH2_FALLBACK


def push2_host():
    """返回本次进程实际可用的 push2 域名（探测结果全进程缓存）"""
    global _push2_host
    if _push2_host:
        return _push2_host
    with _push2_lock:
        if _push2_host:
            return _push2_host
        forced = (os.environ.get("EM_PUSH2_HOST") or "").strip()
        _push2_host = forced or _probe_push2()
        if _push2_host != PUSH2_PRIMARY:
            sys.stderr.write(
                "[em_api] %s 不可达，已切换到 %s（盘中为延迟行情，盘后数据一致）\n"
                % (PUSH2_PRIMARY, _push2_host))
        return _push2_host


def push2_json(path, retry=5, referer="https://quote.eastmoney.com/"):
    """访问 push2 系接口：自动选主机，主域名中途失效时再兜一次延迟域名"""
    host = push2_host()
    try:
        return fetch_json(host, path, retry=retry, referer=referer)
    except Exception:
        if host == PUSH2_PRIMARY:
            global _push2_host
            with _push2_lock:
                _push2_host = PUSH2_FALLBACK
            sys.stderr.write("[em_api] push2 运行中失效，降级到 %s\n" % PUSH2_FALLBACK)
            return fetch_json(PUSH2_FALLBACK, path, retry=retry, referer=referer)
        raise


# ---------------------------------------------------------------- 全市场清单
# fs 说明: m:0 t:6 深主板 / t:80 创业板 / m:1 t:2 沪主板 / t:23 科创板 / m:0 t:81 s:2048 北交所
MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


PZ = 100  # push2 clist 服务端硬上限，传更大只会返回 100 行


def _clist_page(fs, fields, pn, fid="f3", retry=4):
    path = ("/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1&fltt=2&invt=2&fid=%s"
            "&fs=%s&fields=%s&_=%d" % (pn, PZ, fid, fs, fields, _ts()))
    return (push2_json(path, retry=retry) or {}).get("data") or {}


def clist_paged(fs, fields, max_pages=90, fid="f3", workers=10):
    """通用分页拉取：先探 total，再并发拉页，失败页补抓"""
    first = _clist_page(fs, fields, 1, fid)
    total = first.get("total") or 0
    rows = list(first.get("diff") or [])
    if not rows:
        return [], total
    pages = min(max_pages, (total + PZ - 1) // PZ)
    todo = list(range(2, pages + 1))
    got = {1: rows}
    for attempt in range(3):
        if not todo:
            break
        fails = []
        lock = threading.Lock()

        def work(pn):
            try:
                d = _clist_page(fs, fields, pn, fid, retry=2)
                r = d.get("diff") or []
                if not r:
                    raise IOError("empty")
                with lock:
                    got[pn] = r
            except Exception:
                with lock:
                    fails.append(pn)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
        todo = fails
        if todo:
            time.sleep(0.8)
    out = []
    for pn in sorted(got):
        out.extend(got[pn])
    return out, total


def _num(r, k):
    v = r.get(k)
    return None if v in ("-", None, "") else float(v)


SNAP_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21"


def all_stocks_em():
    """东财源：全市场 A 股当日快照"""
    rows, _ = clist_paged(MARKET_FS, SNAP_FIELDS)
    seen, out = set(), []
    for r in rows:
        code = str(r.get("f12"))
        if code in seen or not code or code == "None":
            continue
        seen.add(code)
        out.append({
            "code": code, "market": int(r.get("f13") or 0),
            "name": r.get("f14") or "", "price": _num(r, "f2"), "pct": _num(r, "f3"),
            "chg": _num(r, "f4"), "vol": _num(r, "f5"), "amount": _num(r, "f6"),
            "amp": _num(r, "f7"), "turnover": _num(r, "f8"), "vol_ratio": _num(r, "f10"),
            "high": _num(r, "f15"), "low": _num(r, "f16"), "open": _num(r, "f17"),
            "prev_close": _num(r, "f18"), "total_mv": _num(r, "f20"), "float_mv": _num(r, "f21"),
        })
    return out


SINA_HOST = "vip.stock.finance.sina.com.cn"
SINA_REF = "https://finance.sina.com.cn/"


def _sina_page(page, num=80):
    path = ("/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
            "?page=%d&num=%d&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page" % (page, num))
    txt = fetch_text(SINA_HOST, path, retry=4, referer=SINA_REF).strip()
    if not txt or txt in ("null", "[]"):
        return []
    return json.loads(txt)


def all_stocks_sina(num=80, max_pages=110, workers=8):
    """新浪源：全市场 A 股当日快照（字段更完整，含开高低收/昨收/换手/市值）"""
    first = _sina_page(1, num)
    if not first:
        return []
    got = {1: first}
    todo = list(range(2, max_pages + 1))
    lock = threading.Lock()
    stop = {"at": None}

    def work(p):
        if stop["at"] and p > stop["at"]:
            return
        try:
            rows = _sina_page(p, num)
        except Exception:
            rows = None
        with lock:
            if rows:
                got[p] = rows
                if len(rows) < num and (stop["at"] is None or p < stop["at"]):
                    stop["at"] = p
            elif rows == []:
                if stop["at"] is None or p < stop["at"]:
                    stop["at"] = p - 1

    # 分批推进，遇到空页即停，避免无谓请求
    batch = 12
    for i in range(0, len(todo), batch):
        chunk = [p for p in todo[i:i + batch] if not stop["at"] or p <= stop["at"] + 1]
        if not chunk:
            break
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, chunk))
        if stop["at"] and max(chunk) >= stop["at"]:
            break

    out, seen = [], set()
    for p in sorted(got):
        for r in got[p]:
            code = str(r.get("code") or "")
            sym = str(r.get("symbol") or "")
            if not code or code in seen:
                continue
            seen.add(code)

            def f(k):
                v = r.get(k)
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            pc = f("settlement")
            hi, lo = f("high"), f("low")
            out.append({
                "code": code, "market": 1 if sym.startswith("sh") else 0,
                "name": r.get("name") or "", "price": f("trade"),
                "pct": f("changepercent"), "chg": f("pricechange"),
                "vol": (f("volume") or 0) / 100.0,          # 股 -> 手
                "amount": f("amount"), "turnover": f("turnoverratio"),
                "high": hi, "low": lo, "open": f("open"), "prev_close": pc,
                "amp": ((hi - lo) / pc * 100) if (hi and lo and pc) else None,
                "vol_ratio": None,
                "total_mv": (f("mktcap") or 0) * 1e4 or None,
                "float_mv": (f("nmc") or 0) * 1e4 or None,
            })
    return out


def all_stocks():
    """全市场快照：新浪主源（字段全、限流宽松），东财备源"""
    try:
        rows = all_stocks_sina()
        if len(rows) > 3000:
            return rows
    except Exception:
        rows = []
    em = all_stocks_em()
    return em if len(em) > len(rows) else rows


# ---------------------------------------------------------------- 日 K 线
KFIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def kline(secid, limit=130, fqt=1):
    path = ("/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3&fields2=%s"
            "&klt=101&fqt=%d&end=20500101&lmt=%d&_=%d" % (secid, KFIELDS, fqt, limit, _ts()))
    d = (fetch_json("push2his.eastmoney.com", path, retry=3) or {}).get("data") or {}
    return d.get("klines") or []


def tx_symbol(code, market):
    if code.startswith(("4", "8")) or code.startswith("920"):
        return "bj" + code
    return ("sh" if market == 1 else "sz") + code


def kline_tencent(code, market, limit=130):
    """腾讯备源：[[date,open,close,high,low,vol(手)]] -> 东财同构原始行"""
    sym = tx_symbol(code, market)
    path = ("/appstock/app/fqkline/get?param=%s,day,,,%d,qfq&r=%.6f"
            % (sym, limit, random.random()))
    d = fetch_json("web.ifzq.gtimg.cn", path, retry=3, referer="https://gu.qq.com/") or {}
    node = (d.get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out, prev = [], None
    for r in rows:
        try:
            dt, o, c, h, l, v = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (ValueError, IndexError):
            continue
        pc = prev if prev else o
        pct = (c - pc) / pc * 100 if pc else 0.0
        amp = (h - l) / pc * 100 if pc else 0.0
        amt = v * 100 * (o + c + h + l) / 4.0
        # 东财 klines 字段序: 日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手
        out.append("%s,%.3f,%.3f,%.3f,%.3f,%.0f,%.0f,%.2f,%.2f,%.3f,%.2f"
                   % (dt, o, c, h, l, v, amt, amp, pct, c - pc, 0.0))
        prev = c
    return out


def kline_batch(items, limit=130, workers=12, on_progress=None, fallback=True):
    """items: [(code, market)] -> {code: [rawline,...]}；东财失败自动切腾讯"""
    res, done, total = {}, [0], len(items)
    fails = []
    lock = threading.Lock()

    def work(it):
        code, mkt = it
        try:
            ks = kline("%d.%s" % (mkt, code), limit)
        except Exception:
            ks = []
        with lock:
            if not ks:
                fails.append(it)
            res[code] = ks
            done[0] += 1
            if on_progress and done[0] % 300 == 0:
                on_progress(done[0], total)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, items))

    if fallback and fails:
        def work2(it):
            code, mkt = it
            try:
                ks = kline_tencent(code, mkt, limit)
            except Exception:
                ks = []
            with lock:
                if ks:
                    res[code] = ks
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(work2, fails))

    if on_progress:
        on_progress(total, total)
    return res, len(fails)


# ---------------------------------------------------------------- 涨停/强势/炸板池
def _pool(name, sort="fbt%3Aasc", date=None):
    date = date or time.strftime("%Y%m%d")
    out, pi = [], 0
    while pi < 15:
        path = ("/%s?ut=%s&dpt=wz.ztzt&Pageindex=%d&pagesize=170&sort=%s&date=%s&_=%d"
                % (name, UT, pi, sort, date, _ts()))
        try:
            d = (fetch_json("push2ex.eastmoney.com", path) or {}).get("data") or {}
        except Exception:
            break
        rows = d.get("pool") or []
        out.extend(rows)
        if len(out) >= (d.get("tc") or 0) or not rows:
            break
        pi += 1
    return out


def _pct_limit_up(code, pct):
    """粗略涨停判定（仅用于 push2ex 不可达时的兜底推导）。
    北交所 30% / 科创·创业板 20% / 主板·ST 10%（取 9.8 阈值，ST 5% 会漏但兜底可接受）。"""
    c0 = code[0] if code else "0"
    if code.startswith("688") or c0 in ("3", "8"):
        return pct >= 19.8
    if c0 == "4":
        return pct >= 29.8
    return pct >= 9.8


def _pct_limit_down(code, pct):
    """粗略跌停判定（兜底推导用，阈值同 _pct_limit_up 取反）。"""
    c0 = code[0] if code else "0"
    if code.startswith("688") or c0 in ("3", "8"):
        return pct <= -19.8
    if c0 == "4":
        return pct <= -29.8
    return pct <= -9.8


def _pool_derived(limit_fn, pages=5):
    """push2ex 不可达时的兜底：从全市场涨幅榜(clist 走 push2→push2delay 降级，境外可用)
    按涨幅降序取前 N 页，挑选触及涨停/跌停的标的。元数据(连板/封板时间/炸板)缺失，仅降级呈现。"""
    try:
        rows, _ = clist_paged("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                              "f12,f14,f3,f100", max_pages=pages)
    except Exception:
        return []
    out = []
    for r in rows:
        code = str(r.get("f12") or "")
        pct = _num(r, "f3")
        if not code or pct is None or not limit_fn(code, pct):
            continue
        out.append({"c": code, "n": r.get("f14") or "",
                    "lbc": 1, "fbt": "", "zbc": 0,
                    "hybk": (r.get("f100") or "—"),
                    "derived": True, "pct": pct})
    return out


def zt_pool(date=None):
    rows = _pool("getTopicZTPool", "fbt%3Aasc", date)
    if rows:
        return rows
    # push2ex 不可达（境外 CI 偶发 RST）→ 用涨幅榜推导兜底，
    # 保证盘中异动与每日复盘的涨停数据不空（降级呈现，缺连板/封板时间）。
    return _pool_derived(_pct_limit_up)


def zb_pool(date=None):
    return _pool("getTopicZBPool", "fbt%3Aasc", date)


def qs_pool(date=None):
    return _pool("getTopicQSPool", "zdp%3Adesc", date)


def dt_pool(date=None):
    rows = _pool("getTopicDTPool", "fund%3Aasc", date)
    if rows:
        return rows
    # 跌停池同理兜底（恐慌检测依赖跌停家数）
    return _pool_derived(_pct_limit_down)


# ---------------------------------------------------------------- 板块
def lhb_day_list(date):
    """东方财富龙虎榜日榜（免费公开接口，无需密钥）。

    返回 {code: {...}}，字段含：
      net_amt      龙虎榜净买入额(元，正=净抢筹)
      explanation  上榜原因（含『连续三个交易日内涨幅偏离值达20%』=连板妖股特征）
      buy_seat     买方席位数（多家席位合力抢筹=游资合力）
      sell_seat    卖方席位数
      change_rate  涨跌幅%
      turnover     换手率%
      free_cap     流通市值(元)
      name         名称
    用于妖股潜力『龙虎榜·游资合力』因子（净买入+买方席位数+连板上榜特征）。
    注意：date 需为 'YYYY-MM-DD' 格式；失败返回 {}（优雅降级，不影响主流程）。"""
    if not date:
        return {}
    # 支持 20260814 / 2026-08-14 两种写法
    d = str(date).replace("-", "")
    hyp = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else str(date)
    host = "datacenter-web.eastmoney.com"
    cols = ("SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,CHANGE_RATE,BILLBOARD_NET_AMT,"
            "TURNOVERRATE,EXPLANATION,BUY_SEAT,SELL_SEAT,FREE_MARKET_CAP")
    url = ("https://%s/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILS"
           "&columns=%s&filter=(TRADE_DATE%%3D%%27%s%%27)"
           "&sortColumns=BILLBOARD_NET_AMT&sortTypes=-1&pageSize=300&source=WEB&client=WEB"
           ) % (host, cols, hyp)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/lhb/"})
        d_json = json.loads(urllib.request.urlopen(req, timeout=15).read())
        rows = (d_json.get("result") or {}).get("data") or []
        out = {}
        for r in rows:
            code = str(r.get("SECURITY_CODE") or "").strip()
            if not code:
                continue
            out[code] = {
                "name": r.get("SECURITY_NAME_ABBR"),
                "net_amt": float(r.get("BILLBOARD_NET_AMT") or 0),
                "change_rate": float(r.get("CHANGE_RATE") or 0),
                "turnover": float(r.get("TURNOVERRATE") or 0),
                "explanation": r.get("EXPLANATION") or "",
                # BUY_SEAT 是 5 位编码（每位 1=该买方席位有披露），真实买方席位数 = 编码中「1」的个数
                # （如 11111=5席、13333=1席）；<1000 视为原始整数（兼容旧格式）。
                "buy_seat": (str(int(r.get("BUY_SEAT") or 0)).count("1")
                             if int(r.get("BUY_SEAT") or 0) >= 1000
                             else int(r.get("BUY_SEAT") or 0)),
                "sell_seat": (str(int(r.get("SELL_SEAT") or 0)).count("1")
                              if int(r.get("SELL_SEAT") or 0) >= 1000
                              else int(r.get("SELL_SEAT") or 0)),
                "free_cap": float(r.get("FREE_MARKET_CAP") or 0),
            }
        return out
    except Exception:
        return {}


def board_list(kind="industry"):
    """kind: industry(m:90 t:2) / concept(m:90 t:3)"""
    fs = "m:90+t:2+f:!50" if kind == "industry" else "m:90+t:3+f:!50"
    rows, _ = clist_paged(fs, "f2,f3,f12,f14,f62,f104,f105,f128,f136,f140", max_pages=20)
    seen, out = set(), []
    for r in rows:
        bk = r.get("f12")
        if not bk or bk in seen:
            continue
        seen.add(bk)
        out.append({
            "bk": bk, "name": r.get("f14"), "pct": _num(r, "f3"),
            "main_net": _num(r, "f62"), "up": _num(r, "f104"), "down": _num(r, "f105"),
            "lead": r.get("f128"), "lead_code": r.get("f140"), "lead_pct": _num(r, "f136"),
            "kind": kind,
        })
    return out


def board_members(bk):
    """板块成分股代码列表"""
    rows, _ = clist_paged("b:%s+f:!50" % bk, "f12,f13,f14", max_pages=25)
    return sorted({str(r.get("f12")) for r in rows if r.get("f12")})


def board_members_batch(bks, workers=16, on_progress=None):
    res, done = {}, [0]
    lock = threading.Lock()

    def work(bk):
        try:
            m = board_members(bk)
        except Exception:
            m = []
        with lock:
            res[bk] = m
            done[0] += 1
            if on_progress and done[0] % 100 == 0:
                on_progress(done[0], len(bks))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, bks))
    return res


# ---------------------------------------------------------------- 指数
INDEXES = [("1.000001", "上证指数"), ("0.399001", "深证成指"), ("0.399006", "创业板指"),
           ("1.000688", "科创50"), ("0.399005", "中小100"), ("1.000016", "上证50"),
           ("1.000905", "中证500"), ("0.399303", "国证2000")]


def index_snapshot():
    secids = ",".join(s for s, _ in INDEXES)
    path = ("/api/qt/ulist.np/get?fltt=2&secids=%s&fields=f2,f3,f4,f6,f12,f13,f14,f104,f105&_=%d"
            % (secids, _ts()))
    d = (push2_json(path) or {}).get("data") or {}
    out = []
    for r in d.get("diff") or []:
        out.append({"code": r.get("f12"), "name": r.get("f14"), "price": r.get("f2"),
                    "pct": r.get("f3"), "chg": r.get("f4"), "amount": r.get("f6"),
                    "up": r.get("f104"), "down": r.get("f105")})
    return out


def index_kline(secid, limit=130):
    return kline(secid, limit)


# ---------------------------------------------------------------- 外围市场（美股 / 日股 / 韩股）
# 数据源说明：本机环境仅能稳定访问国内源。
#  · 美股：腾讯 qt.gtimg.cn（s_usDJI / s_usIXIC / s_usINX），字段 f[3]=现价 f[4]=涨跌点 f[5]=涨跌幅%
#  · 日股/韩股：东财 push2 单指数接口 stock/get（secid 100.N225 / 100.KS11），
#    该域名间歇性限流，故带重试+退避，失败则跳过（引擎按可得数据推断 A 股方向）
US_TENCENT = [
    ("美股", "s_usDJI", "道琼斯"), ("美股", "s_usIXIC", "纳斯达克综指"),
    ("美股", "s_usINX", "标普500"),
]
JP_KR_EASTMONEY = [
    ("日股", "100.N225", "日经225"), ("韩股", "100.KS11", "韩国KOSPI"),
]


def global_index_snapshot(retry=3):
    """抓取外围主要指数最新涨跌，返回 [{'region','name','price','pct',...}]。失败返回 []。"""
    out = []
    # 1) 美股：腾讯源（稳定）
    try:
        raw = fetch_text("qt.gtimg.cn", "/q=" + ",".join(s for _, s, _ in US_TENCENT),
                         retry=retry, referer="https://gu.qq.com/")
        for line in raw.strip().split("\n"):
            if not line.strip() or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip().strip('"')
            if not val:
                continue
            f = val.split("~")
            if len(f) < 6:
                continue
            sym = key.replace("v_", "").strip()
            meta = next(((rg, n) for rg, s, n in US_TENCENT if s == sym), None)
            if not meta:
                continue
            region, name = meta
            try:
                price = float(f[3]) if f[3] not in ("", "--") else None
                if region == "港股":
                    # 腾讯港股指数字段顺序与个股不同：f[3]=现价 f[4]=昨收 f[5]=开盘，
                    # f[5] 不是涨跌幅，需用 (现价-昨收)/昨收 推算。
                    prev = float(f[4]) if f[4] not in ("", "--") else None
                    pct = round((price / prev - 1) * 100, 2) if (price and prev) else None
                else:
                    pct = float(f[5]) if f[5] not in ("", "--") else None
            except (ValueError, ZeroDivisionError, TypeError):
                price = pct = None
            out.append({"region": region, "code": sym, "name": name,
                        "price": price, "pct": pct, "chg": None})
    except Exception:
        pass
    # 1.5) 港股指数 + 关键 ETF：腾讯源（与美股同源，解析一致）
    HK_ETF_TENCENT = [
        ("港股", "hkHSI", "恒生指数"),
        ("港股", "hkHSTECH", "恒生科技"),
        ("ETF", "sh510300", "沪深300ETF"),
        ("ETF", "sz159915", "创业板ETF"),
        ("ETF", "sh518880", "黄金ETF"),
        ("ETF", "sh513100", "纳指ETF"),
    ]
    try:
        raw = fetch_text("qt.gtimg.cn", "/q=" + ",".join(s for _, s, _ in HK_ETF_TENCENT),
                         retry=retry, referer="https://gu.qq.com/")
        for line in raw.strip().split("\n"):
            if not line.strip() or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip().strip('"')
            if not val:
                continue
            f = val.split("~")
            if len(f) < 6:
                continue
            sym = key.replace("v_", "").strip()
            meta = next(((rg, n) for rg, s, n in HK_ETF_TENCENT if s == sym), None)
            if not meta:
                continue
            region, name = meta
            try:
                price = float(f[3]) if f[3] not in ("", "--") else None
                if region == "港股":
                    # 腾讯港股指数字段顺序与个股不同：f[3]=现价 f[4]=昨收 f[5]=开盘，
                    # f[5] 不是涨跌幅，需用 (现价-昨收)/昨收 推算。
                    prev = float(f[4]) if f[4] not in ("", "--") else None
                    pct = round((price / prev - 1) * 100, 2) if (price and prev) else None
                else:
                    pct = float(f[5]) if f[5] not in ("", "--") else None
            except (ValueError, ZeroDivisionError, TypeError):
                price = pct = None
            out.append({"region": region, "code": sym, "name": name,
                        "price": price, "pct": pct, "chg": None})
    except Exception:
        pass
    # 2) 日股/韩股：东财单指数接口（带退避重试，限流时跳过）
    for region, secid, name in JP_KR_EASTMONEY:
        for attempt in range(max(1, retry)):
            try:
                path = ("/api/qt/stock/get?secid=%s&fields=f43,f58,f170&_=%d"
                        % (secid, _ts()))
                d = (push2_json(path, retry=2) or {}).get("data") or {}
                if not d:
                    break
                raw_p = d.get("f43")
                raw_pct = d.get("f170")
                price = float(raw_p) / 100.0 if raw_p not in (None, "", "--") else None
                pct = float(raw_pct) / 100.0 if raw_pct not in (None, "", "--") else None
                out.append({"region": region, "code": secid, "name": d.get("f58") or name,
                            "price": price, "pct": pct, "chg": None})
                break
            except Exception:
                if attempt < retry - 1:
                    time.sleep(2)
                continue
    return out


# ---------------------------------------------------------------- 资金流
def market_fundflow():
    """沪深两市主力资金流历史（近 60 日）"""
    path = ("/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=1.000001&secid2=0.399001"
            "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65&_=%d" % _ts())
    d = (fetch_json("push2his.eastmoney.com", path) or {}).get("data") or {}
    return (d.get("klines") or [])[-60:]
