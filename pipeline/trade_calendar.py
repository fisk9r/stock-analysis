# -*- coding: utf-8 -*-
"""A股交易日历：判断某天是否为沪深交易日。

为什么需要：主调度 cron 写的是 `* * 1-5`（周一至周五），法定节假日照样点火。
若不拦截，国庆/春节期间会连续多日把「节前那根K线」当作『今日复盘』推给用户，
既误导又白烧 ServerChan 额度（每天仅 5 条）。

规则（沪深交易所）：
    交易日 = 周一~周五  且  不在国务院法定放假期间
    ⚠ 调休补班日（如 2026-02-14 周六上班）A股【不开市】，故只认周一~周五。

安全设计（宁可多推、不可漏推）：
    未收录年份 → 一律视为交易日，绝不因日历缺失而漏掉真实交易日的推送。
    每年国务院发布次年放假安排后（通常 11 月），把新一年补进 HOLIDAYS 并加入 COVERED_YEARS。

数据来源：国务院办公厅《关于2026年部分节假日安排的通知》国办发明电〔2025〕7号（2025-11-04）
"""

import datetime

# 已收录放假安排的年份。不在此集合内的年份一律按「交易日」处理（保守，不漏推）。
COVERED_YEARS = {2026}

# 完整放假区间（含其中的周末，便于与官方通知逐条对照核验）。
# 判定时只有落在周一~周五的日期才真正影响交易日结论。
HOLIDAYS = {
    # 一、元旦：1月1日(周四)至3日(周六)放假调休，共3天。1月4日(周日)上班。
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 二、春节：2月15日(周日)至23日(周一)放假调休，共9天。2月14日(周六)、2月28日(周六)上班。
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 三、清明节：4月4日(周六)至6日(周一)放假，共3天。
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 四、劳动节：5月1日(周五)至5日(周二)放假调休，共5天。5月9日(周六)上班。
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 五、端午节：6月19日(周五)至21日(周日)放假，共3天。
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 六、中秋节：9月25日(周五)至27日(周日)放假，共3天。
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 七、国庆节：10月1日(周四)至7日(周三)放假调休，共7天。9月20日(周日)、10月10日(周六)上班。
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}


def _to_date(d=None):
    """统一成 datetime.date；None 表示『北京时间的今天』。"""
    if d is None:
        tz = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.now(tz).date()
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    return datetime.date.fromisoformat(str(d)[:10])


def is_trade_day(d=None):
    """d 为 None 时判断『北京时间今天』是否为沪深交易日。"""
    day = _to_date(d)
    if day.weekday() >= 5:          # 周六(5)/周日(6) 一律休市
        return False
    if day.year not in COVERED_YEARS:
        return True                  # 未收录年份：保守视为交易日，绝不漏推
    return day.isoformat() not in HOLIDAYS


def why_closed(d=None):
    """返回休市原因（用于日志）；是交易日则返回空串。"""
    day = _to_date(d)
    if day.weekday() >= 5:
        return "周末休市"
    if day.year in COVERED_YEARS and day.isoformat() in HOLIDAYS:
        return "法定节假日休市"
    return ""


def calendar_covered(d=None):
    """该日期所属年份是否已收录放假安排（未收录时调用方可提示补充日历）。"""
    return _to_date(d).year in COVERED_YEARS


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        for a in args:
            print("%s  交易日=%s  %s" % (a, is_trade_day(a), why_closed(a)))
    else:
        today = _to_date()
        print("今天 %s  交易日=%s  %s" % (today, is_trade_day(), why_closed() or "正常开市"))
        print("已收录年份：%s" % sorted(COVERED_YEARS))
        # 列出本年度所有休市工作日，便于人工核对
        closed = sorted(h for h in HOLIDAYS if _to_date(h).weekday() < 5)
        print("%d 年因法定节假日休市的工作日共 %d 天：" % (max(COVERED_YEARS), len(closed)))
        for c in closed:
            print("   ", c, "周%d" % (_to_date(c).weekday() + 1))
