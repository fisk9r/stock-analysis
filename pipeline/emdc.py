# -*- coding: utf-8 -*-
"""东方财富数据中心（datacenter-web.eastmoney.com）统一封装。

说明：
- 盘后 CI 环境下有网络，接口可用；本地沙箱通常无外网 → fetch_json 抛错，
  由 build.py 各引擎 try/except 兜底为 None，不阻断主流程。
- 数据中心要求带 Referer=https://data.eastmoney.com/，否则返回 403。
- 返回结构较稳定：resp["result"]["data"] 为列表；此 helper 做容错抽取。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import em_api


HOST = "datacenter-web.eastmoney.com"
REFERER = "https://data.eastmoney.com/"


def get(report, columns=None, flt=None, page_size=200, sort=None, extra=None):
    """通用数据中心 GET。
    report: reportName；flt: 过滤表达式（自动 URL 编码，实证：不编码会 HTTP 400）
    sort=None 时默认 TRADE_DATE 倒序。
    """
    import urllib.parse
    parts = [
        "/api/data/v1/get?reportName=%s" % report,
        "pageSize=%d" % page_size,
        "pageNumber=1",
        "source=WEB&client=WEB",
    ]
    if sort:
        parts.append("sortColumns=%s&sortTypes=-1" % sort)
    if columns:
        parts.append("columns=%s" % columns)
    if flt:
        parts.append("filter=%s" % urllib.parse.quote(flt))
    if extra:
        for k, v in extra.items():
            parts.append("%s=%s" % (k, v))
    path = "&".join(parts)
    j = em_api.fetch_json(HOST, path, retry=3, referer=REFERER)
    if not isinstance(j, dict):
        return None
    res = j.get("result") or {}
    data = res.get("data")
    if data is None:
        # 个别接口返回在 j["data"]
        data = j.get("data")
    if isinstance(data, dict):
        data = data.get("list") or data.get("items") or []
    return data or []


def extract(rows, mapping):
    """把接口原始行按 mapping={目标键: 原始键列表(取第一个非空)} 投影成干净 dict 列表"""
    out = []
    for r in (rows or []):
        item = {}
        for k, keys in mapping.items():
            val = None
            if isinstance(keys, str):
                keys = [keys]
            for kk in keys:
                if r.get(kk) is not None:
                    val = r[kk]
                    break
            item[k] = val
        out.append(item)
    return out
