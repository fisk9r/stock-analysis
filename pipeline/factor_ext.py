# -*- coding: utf-8 -*-
"""factor_ext —— a-stock-data（github.com/simonlin1212/a-stock-data, Apache 2.0）
字段知识与数据源踩坑经验的融入落地。

调研结论（2026-08-29）：
- a-stock-data 是 SKILL.md 形态的 A 股数据工具包（19 数据源 / 54 端点 / 11 层架构），
  核心价值是**实测踩坑的字段知识**而非代码本身（mootdx/pandas 等依赖不符合零依赖约束）。
- 本模块把可直接复用的知识固化为纯 Python 函数，供执行器与流水线调用：

  腾讯 qt.gtimg.cn 实时行情字段表（实测，1-based 计数指 fields 列表下标）：
    fields[1]  名称        fields[3]  现价        fields[4]  昨收
    fields[5]  今开        fields[6]  成交量(手)  fields[37] 成交额(万)
    fields[38] 换手率(%)   fields[39] 市盈率TTM   fields[43] 振幅(%)
    fields[44] 流通市值(亿) fields[45] 总市值(亿)  fields[46] 市净率PB
  注意：东财 push2 接口对高频请求封 IP（a-stock-data 实测），仅作兜底且需限流；
  腾讯接口无此限制，是本项目主源——与 a-stock-data 结论一致。

用法：
    from factor_ext import parse_tencent_quote_fields
    extra = parse_tencent_quote_fields(fields)   # 返回换手率/振幅/PB/成交额等
"""
from __future__ import annotations


def parse_tencent_quote_fields(fields: list) -> dict:
    """从腾讯行情 fields 数组提取扩展字段（a-stock-data 实测字段表）。

    全部 best-effort：字段缺失/非数字时置 None/0，绝不抛异常。
    """
    def _f(idx):
        try:
            if len(fields) > idx:
                v = fields[idx]
                return float(v) if v not in ("", None) else 0.0
        except (ValueError, TypeError):
            pass
        return 0.0

    def _s(idx):
        return fields[idx] if len(fields) > idx else ""

    return {
        "name": _s(1),
        "price": _f(3),
        "prev_close": _f(4),
        "open": _f(5),
        "volume_hand": _f(6),        # 成交量（手）
        "amount_wan": _f(37),        # 成交额（万元）
        "turnover": _f(38),          # 换手率 %
        "pe_ttm": _f(39),            # 市盈率 TTM
        "amplitude": _f(43),         # 振幅 %
        "float_mv": _f(44),          # 流通市值（亿）
        "total_mv": _f(45),          # 总市值（亿）
        "pb": _f(46),                # 市净率
    }


def turnover_flag(turnover: float, streak: int) -> dict:
    """换手率 × 连板高度 → 风险/健康标注（a-stock-data 短线口径 + 本项目回测）。

    - 低换手(<3%)高位票：接力度不足，标注「缩量板」
    - 高换手(>25%)高位票：分歧过大，标注「巨量分歧」
    - 中间带：健康
    返回 {flag, note}；flag ∈ healthy / thin / divergent。
    """
    if turnover <= 0:
        return {"flag": "unknown", "note": "无换手数据"}
    if streak >= 3 and turnover < 3.0:
        return {"flag": "thin",
                "note": "高度%d板但换手仅%.1f%%，缩量板接力度存疑" % (streak, turnover)}
    if streak >= 2 and turnover > 25.0:
        return {"flag": "divergent",
                "note": "高度%d板换手%.1f%%，巨量分歧，警惕炸板" % (streak, turnover)}
    return {"flag": "healthy", "note": "换手%.1f%%正常" % turnover}


# ---- a-stock-data 数据源结论备查（防止后续重走弯路）----
SOURCE_NOTES = {
    "tencent": "主源。qt.gtimg.cn 无 CORS/封 IP 问题；fqkline 前复权日 K 稳定。",
    "mootdx": "TCP 7709 通达信协议，不封 IP，a-stock-data 首选备用；需 mootdx 库（未引入）。",
    "eastmoney": "push2 高频封 IP，仅限流兜底（em_get 单次 + sleep）；本项目 em_api 已内置降级。",
    "sina": "无 CORS / JSONP 限制问题但字段少；仅用于交叉校验（multi_source.py）。",
    "baostock": "免费但延迟一天且需登录 token；不适合盘后实时管线。",
}
