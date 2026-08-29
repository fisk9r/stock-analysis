# -*- coding: utf-8 -*-
"""多假说选股回测：断板反包/反包确认/二板过滤/超跌反弹/高度溢价
口径与 rec_picks 对齐：T 收盘买入，next_pct = T+1收盘/T收盘-1；另测 T+3。
"""
import sqlite3
from collections import defaultdict

DB = "cache/market.db"


def limit_ratio(code):
    if code[:3] in ("300", "301", "688", "689"):
        return 19.8
    if code[0] in ("8", "4", "9"):
        return 29.5
    return 9.8


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    st_names = {r[0] for r in cur.execute(
        "SELECT code FROM stocks WHERE name LIKE '%ST%' OR name LIKE '%退%'").fetchall()}

    bars = defaultdict(list)
    for code, date, o, c, h, l, vol, turn, pct in cur.execute(
            "SELECT code,date,open,close,high,low,vol,turn,pct FROM bars "
            "WHERE close>0 AND high>0 AND low>0 ORDER BY code,date"):
        bars[code].append((date, o, c, h, l, vol, turn, pct))
    con.close()

    dates_all = sorted({b[0] for bs in bars.values() for b in bs[:1]} | set())
    # 交易日序列（用某只大票的日期做基准）
    idx = sorted({b[0] for bs in [bars.get("600519")] for b in bs}) if "600519" in bars else []
    if not idx:
        idx = sorted({b[0] for bs in bars.values() for b in bs[:1]})
    dpos = {d: i for i, d in enumerate(idx)}

    def stat(samples, label):
        if not samples:
            print(f"{label:<34} 样本=0")
            return
        n = len(samples)
        win = sum(1 for x in samples if x > 0)
        avg = sum(samples) / n
        med = sorted(samples)[n // 2]
        mx = max(samples)
        print(f"{label:<34} n={n:<5} 胜率={win/n*100:5.1f}%  均值={avg:+6.2f}%  中位={med:+6.2f}%  最大={mx:+7.2f}%")

    # ── 通用扫描 ──
    hyp_break_rebound = []      # 断板反包：st>=2 → 断板日收盘买
    hyp_break_open = []         # 断板次日开盘买（T+1开盘 vs T+2收盘）暂用收盘口径
    hyp_rebounce_confirm = []   # 断板后反包涨停 → 次日买
    hyp_firstyin = []           # 连板后首阴（st>=2且当日跌>2%）→ 次日买
    hyp_oversold = []           # 连续3日跌累计>=12% → 次日买
    by_height_t1 = defaultdict(list)   # st=1..7 → next_pct
    by_height_t3 = defaultdict(list)
    board2_turn = defaultdict(list)    # 二板：换手分层
    board2_volx = defaultdict(list)    # 二板：量比分层
    first_open_gap = defaultdict(list) # 首板：次日开盘溢价分层 vs next_pct

    for code, bs in bars.items():
        if code in st_names or len(bs) < 30:
            continue
        lr = limit_ratio(code)
        lim_flags = [b[7] >= lr for b in bs]
        for i in range(10, len(bs) - 4):
            # 当前连板高度（连续涨停到 i 日）
            st = 0
            j = i
            while j >= 0 and lim_flags[j]:
                st += 1
                j -= 1
            d, o, c, h, l, vol, turn, pct = bs[i]
            # next_pct（T+1 收盘口径）
            n1 = (bs[i+1][2] / c - 1) * 100 if bs[i+1][2] > 0 else None
            n3 = (bs[i+3][2] / c - 1) * 100 if bs[i+3][2] > 0 else None
            if lim_flags[i]:
                by_height_t1[min(st, 8)].append(n1)
                by_height_t3[min(st, 8)].append(n3)
                if st == 2 and turn:
                    tv = "换手<10%" if turn < 10 else ("换手10-20%" if turn < 20 else "换手>=20%")
                    board2_turn[tv].append(n1)
                    vprev = bs[i-1][5]
                    if vprev and vprev > 0:
                        vx = vol / vprev
                        vk = "量比<1.2" if vx < 1.2 else ("量比1.2-2" if vx < 2 else "量比>=2")
                        board2_volx[vk].append(n1)
                if st == 1 and i + 1 < len(bs):
                    og = (bs[i+1][1] / c - 1) * 100
                    gk = ("低开<-2%" if og < -2 else ("平开-2~2%" if og <= 2 else ("高开2-5%" if og <= 5 else "高开>5%")))
                    first_open_gap[gk].append(n1)
            # 断板反包：T-1 及之前 st>=2 连板，T 日未涨停（断板）
            if not lim_flags[i] and st == 0:
                prev_st = 0
                k = i - 1
                while k >= 0 and lim_flags[k]:
                    prev_st += 1
                    k -= 1
                if prev_st >= 2:
                    hyp_break_rebound.append(n1)          # 断板当日收盘买 → 次日
                    # 反包确认：断板后次日涨停
                    if i + 1 < len(bs) and lim_flags[i+1]:
                        hyp_rebounce_confirm.append(
                            (bs[i+2][2] / bs[i+1][2] - 1) * 100 if bs[i+2][2] > 0 else None)
                    # 首阴：断板且当日跌>2%
                    if pct < -2:
                        hyp_firstyin.append(n1)
            # 超跌反弹：3日累计跌>=12%
            if i >= 3:
                cum = (c / bs[i-3][2] - 1) * 100 if bs[i-3][2] > 0 else 0
                if cum <= -12 and bs[i][7] < 0 and bs[i-1][7] < 0:
                    hyp_oversold.append(n1)

    def clean(xs):
        return [x for x in xs if x is not None]

    print("=" * 78)
    print("【假说1】断板反包：st>=2连板 → 断板日收盘买 → 次日卖")
    stat(clean(hyp_break_rebound), "全部断板（T+1）")
    print()
    print("【假说2】反包确认：断板 → 次日涨停反包 → 反包日收盘买 → 次日卖")
    stat(clean(hyp_rebounce_confirm), "反包确认（T+1）")
    print()
    print("【假说3】首阴反包：st>=2 → 断板日跌>2%（首阴）→ 收盘买 → 次日卖")
    stat(clean(hyp_firstyin), "首阴买入（T+1）")
    print()
    print("【假说4】超跌反弹：3日累计跌>=12% 且连续2日收跌 → 买 → 次日卖")
    stat(clean(hyp_oversold), "超跌反弹（T+1）")
    print()
    print("【对照】连板高度 → 次日收益（全市场大样本，非 rec_picks）")
    for h in sorted(by_height_t1):
        stat(clean(by_height_t1[h]), f"  st={h}（T+1）")
    print()
    print("【假说5】二板过滤：st=2 当日特征 → 次日")
    for k in sorted(board2_turn):
        stat(clean(board2_turn[k]), f"  {k}")
    for k in sorted(board2_volx):
        stat(clean(board2_volx[k]), f"  {k}")
    print()
    print("【假说6】首板次日开盘溢价 vs 次日收益")
    for k in sorted(first_open_gap):
        stat(clean(first_open_gap[k]), f"  {k}")


if __name__ == "__main__":
    main()
