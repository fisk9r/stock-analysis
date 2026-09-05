"""市场准入过滤（2026-09-05 用户拍板 #486）。

用户实盘资金**只能交易 沪深主板 + 创业板**：科创板（688/689，需 50 万+2 年
经验）与北交所（43/83/87/88/920，需 50 万+2 年）均未达开通要求，因此
任何选股/推送/模拟盘下单都必须先把这两类票剔除——推了也买不了，纯噪音。

单一职责模块，被 pipeline（推荐/买点/波段）与 tools/executor（模拟盘）共同引用，
保证「推送出去的票 = 能买的票」。
"""

# 允许交易的代码前缀（沪深主板 + 创业板）
_OK_PREFIX = ("600", "601", "603", "605",      # 沪市主板
              "000", "001", "002", "003",      # 深市主板（002/003 原中小板已并入）
              "300", "301")                    # 创业板

# 明确排除的市场：科创板 / 北交所
_KC_PREFIX = ("688", "689")                    # 科创板（含 CDR）
_BJ_PREFIX = ("43", "83", "87", "88", "920")   # 北交所


def market_of(code):
    """返回代码所属市场：沪深主板 / 创业板 / 科创板 / 北交所 / 其它。"""
    c = str(code or "").strip().zfill(6)
    if len(c) != 6 or not c.isdigit():
        return "其它"
    if c.startswith(_KC_PREFIX):
        return "科创板"
    if c.startswith(_BJ_PREFIX):
        return "北交所"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(_OK_PREFIX):
        return "沪深主板"
    return "其它"


def tradable(code):
    """用户资金是否可交易该代码（沪深主板 + 创业板 = True）。

    未知代码段（B股 900/200、指数、ETF 等）一律 False——宁可漏推也不推买不了的票。
    """
    return market_of(code) in ("沪深主板", "创业板")


def filter_codes(codes):
    """批量过滤，返回可交易代码列表（保持原顺序）。"""
    return [c for c in (codes or []) if tradable(c)]


def filter_items(items, key="code"):
    """批量过滤 dict 列表，返回可交易条目列表（保持原顺序）。"""
    return [x for x in (items or []) if tradable((x or {}).get(key))]


def filter_rec(rec):
    """就地过滤 recommend dict：所有「元素带 code 的列表」统一剔除不可交易市场。

    覆盖 core/relay/ambush/all/trend/bull/strategies/band_trade/fused 等全部桶，
    调用方无需逐个列举（新增桶自动生效）。返回剔除条数（便于日志核查）。
    """
    cut = 0
    for k, v in list((rec or {}).items()):
        if isinstance(v, list) and v and isinstance(v[0], dict) and "code" in v[0]:
            before = len(v)
            rec[k] = [x for x in v if tradable(x.get("code"))]
            cut += before - len(rec[k])
    return cut
