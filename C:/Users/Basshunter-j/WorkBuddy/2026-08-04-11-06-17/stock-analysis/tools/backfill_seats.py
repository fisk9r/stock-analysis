# -*- coding: utf-8 -*-
"""席位画像样本回填：用东财 RPT_OPERATEDEPT_TRADE_DETAILS 的历史日期查询能力，
把近 N 个交易日的知名席位命中行一次性灌入 seat_daily，让 win_rates 尽快过 ≥8 样本门槛。

用法：
  python tools/backfill_seats.py            # 回填最近 90 个交易日（跳过已有日期）
  python tools/backfill_seats.py 180        # 自定义天数

说明：
  - 交易日列表取自本地 bars（与引擎同源）；
  - seats.scan 每日 1~6 页请求，约 0.5~1.5 秒/日，90 日约 2 分钟；
  - 已存在的 (date, dept_code, code) 主键自动跳过，可重复运行（幂等）。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import store        # noqa: E402
import seats        # noqa: E402


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    con = store.connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM bars ORDER BY date DESC LIMIT ?", (days,))]
    have = {r[0] for r in con.execute("SELECT DISTINCT date FROM seat_daily")}
    todo = [d for d in dates if d not in have]
    print("bars 最近 %d 个交易日，seat_daily 已有 %d 日，待回填 %d 日"
          % (len(dates), len(have), len(todo)))
    n_rows = 0
    for i, d in enumerate(todo):
        try:
            r = seats.scan(d)
        except Exception as e:
            print("  [%d/%d] %s 扫描失败：%r" % (i + 1, len(todo), d, e))
            time.sleep(2)
            continue
        if r and r.get("hits"):
            store.upsert_seats(con, d, r["hits"])
            n_rows += len(r["hits"])
            print("  [%d/%d] %s：%d 条命中" % (i + 1, len(todo), d, len(r["hits"])))
        else:
            print("  [%d/%d] %s：无知名席位" % (i + 1, len(todo), d))
        time.sleep(0.6)
    con.commit()
    total = con.execute("SELECT COUNT(*), COUNT(DISTINCT date) FROM seat_daily").fetchone()
    print("完成：本次新增 %d 行；seat_daily 现有 %d 行 / %d 个交易日" % (n_rows, total[0], total[1]))
    # 立刻看胜率画像是否已过样本门槛
    import engine  # 仅用于复用 con？不必——直接调 seats.win_rates
    stats = seats.win_rates(con)
    print("\n当前席位胜率画像（≥8 样本才显示）：")
    for label, st in sorted(stats.items(), key=lambda kv: -kv[1].get("win_rate", 0)):
        print("  %s：胜率 %s%%（%d 次，均值 %s%%）"
              % (label, st.get("win_rate"), st.get("n"), st.get("avg_ret")))
    if not stats:
        print("  （样本仍不足 8，继续逐日积累）")
    con.close()


if __name__ == "__main__":
    main()
