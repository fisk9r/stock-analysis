# -*- coding: utf-8 -*-
"""模拟盘 broker（broker_sim）：不接券商，把「假设成交」记录到本地库。

成交假设（保守口径，接近真实但略悲观）：
  买入价 = 当日开盘价 × (1 + 0.1%)  # 冲击成本
  无滑点模型（涨停买不进等极端情况由调用方按行情判断）
存储：SQLite（tools/executor/sim.db），与正式 rec_picks 口径兼容，方便后续 join 回测。
"""
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "sim.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_trades(
  ts TEXT, date TEXT, code TEXT, name TEXT,
  action TEXT, price REAL, volume INTEGER, amount REAL,
  open_gap REAL, source TEXT, verdict_reason TEXT,
  PRIMARY KEY(date, code, action)
);
CREATE TABLE IF NOT EXISTS sim_positions(
  date TEXT, code TEXT, name TEXT,
  open_gap REAL, buy_price REAL, volume INTEGER,
  sell_date TEXT DEFAULT NULL, sell_price REAL DEFAULT NULL,
  pnl_pct REAL DEFAULT NULL,
  PRIMARY KEY(date, code)
);
"""


def _con():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


class SimBroker:
    """模拟盘适配器：与 QmtBroker 同接口，信号打分/执行链路完全复用。"""

    def __init__(self):
        self.con = _con()

    def balance(self) -> dict:
        row = self.con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM sim_trades WHERE action='BUY' "
            "AND date=?", (time.strftime("%Y-%m-%d"),)).fetchone()
        return {"cash": 100000.0 - row[0], "market_value": 0, "total": 100000.0}

    def positions(self) -> list:
        rows = self.con.execute(
            "SELECT code,name,buy_price,volume FROM sim_positions "
            "WHERE sell_date IS NULL").fetchall()
        return [{"code": r[0], "name": r[1], "avg_price": r[2], "volume": r[3]}
                for r in rows]

    def buy_limit(self, code: str, price: float, amount_yuan: float,
                  sig: dict = None) -> dict:
        """模拟成交：按开盘价+0.1% 冲击成本买入。"""
        sig = sig or {}
        d = time.strftime("%Y-%m-%d")
        vol = int(amount_yuan / price / 100) * 100
        if vol < 100:
            return {"ok": False, "reason": "金额不足一手"}
        fill = price * 1.001  # 冲击成本
        self.con.execute(
            "INSERT OR REPLACE INTO sim_trades VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), d, code, sig.get("name") or "",
             "BUY", round(fill, 3), vol, round(fill * vol, 2),
             sig.get("open_gap"), sig.get("source"), sig.get("reason")))
        self.con.execute(
            "INSERT OR REPLACE INTO sim_positions(date,code,name,open_gap,buy_price,volume) "
            "VALUES(?,?,?,?,?,?)",
            (d, code, sig.get("name") or "", sig.get("open_gap"), round(fill, 3), vol))
        self.con.commit()
        return {"ok": True, "volume": vol, "price": round(fill, 3)}

    def sell_limit(self, code: str, price: float, volume: int = None,
                   sig: dict = None) -> dict:
        row = self.con.execute(
            "SELECT buy_price, volume FROM sim_positions WHERE code=? AND sell_date IS NULL",
            (code,)).fetchone()
        if not row:
            return {"ok": False, "reason": "模拟盘无此持仓"}
        buy_price, vol = row
        d = time.strftime("%Y-%m-%d")
        pnl = (price / buy_price - 1) * 100
        self.con.execute(
            "INSERT OR REPLACE INTO sim_trades VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), d, code, "", "SELL",
             round(price, 3), vol, round(price * vol, 2),
             None, None, ""))
        self.con.execute(
            "UPDATE sim_positions SET sell_date=?, sell_price=?, pnl_pct=? "
            "WHERE code=? AND sell_date IS NULL",
            (d, round(price, 3), round(pnl, 2), code))
        self.con.commit()
        return {"ok": True, "volume": vol, "price": round(price, 3), "pnl_pct": round(pnl, 2)}

    def summary(self) -> str:
        """模拟盘战绩摘要（供推送/日志）。"""
        closed = self.con.execute(
            "SELECT COUNT(*), ROUND(AVG(pnl_pct),2), "
            "ROUND(SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END)*1.0/COUNT(*)*100,1) "
            "FROM sim_positions WHERE sell_date IS NOT NULL").fetchone()
        if not closed[0]:
            return "模拟盘暂无平仓记录"
        return "模拟盘已平仓 %d 笔：胜率 %s%% / 平均 %s%%" % (closed[0], closed[2], closed[1])
