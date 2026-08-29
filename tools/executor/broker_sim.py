# -*- coding: utf-8 -*-
"""模拟盘 broker（broker_sim）：不接券商，把「假设成交」记录到本地库。

成交假设（保守口径，接近真实但略悲观）：
  买入价 = 委托价 × (1 + 冲击成本0.1%)   # 开盘价买入
  卖出价 = 实际卖出价 × (1 - 冲击成本0.1%)
  双边费用按回测口径已含在策略统计里（0.15%），此处冲击成本单独叠加，更悲观更安全。

存储：SQLite（tools/executor/sim.db）。
口径：初始资金 100,000 元（config.sim.initial_cash），
     总资产 = 初始资金 + 已实现盈亏 - 未平仓买入占用 + 未平仓市值。
"""
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "sim.db")
CFG_PATH = os.path.join(ROOT, "config.json")


def _load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _initial_cash():
    return float(((_load_cfg().get("sim")) or {}).get("initial_cash") or 100000)


def _impact():
    return float(((_load_cfg().get("sim")) or {}).get("impact_cost_pct") or 0.1) / 100.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_trades(
  ts TEXT, date TEXT, code TEXT, name TEXT,
  action TEXT, price REAL, volume INTEGER, amount REAL,
  open_gap REAL, source TEXT, verdict_reason TEXT,
  PRIMARY KEY(date, code, action)
);
CREATE TABLE IF NOT EXISTS sim_positions(
  buy_date TEXT, code TEXT, name TEXT,
  open_gap REAL, buy_price REAL, volume INTEGER,
  streak INTEGER DEFAULT 0,
  sell_date TEXT DEFAULT NULL, sell_price REAL DEFAULT NULL,
  sell_reason TEXT DEFAULT '',
  pnl_pct REAL DEFAULT NULL,
  PRIMARY KEY(buy_date, code)
);
"""


def _con():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # 旧库迁移：sim_positions 原主键是 (date, code) 且无 streak/sell_reason 列
    cols = [r[1] for r in con.execute("PRAGMA table_info(sim_positions)").fetchall()]
    if cols and "streak" not in cols:
        try:
            con.execute("ALTER TABLE sim_positions ADD COLUMN streak INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    if cols and "sell_reason" not in cols:
        try:
            con.execute("ALTER TABLE sim_positions ADD COLUMN sell_reason TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    if cols and "buy_date" not in cols:
        # 老主键列名是 date —— 改名不便，直接补视图级兼容：检测旧列
        pass
    return con


def _cols(con):
    return [r[1] for r in con.execute("PRAGMA table_info(sim_positions)").fetchall()]


class SimBroker:
    """模拟盘适配器：与 QmtBroker 同接口，信号打分/执行链路完全复用。"""

    def __init__(self):
        self.con = _con()
        # 兼容旧表：若存在旧列名 date 而无 buy_date，做一次性重建
        cols = _cols(self.con)
        if cols and "buy_date" not in cols and "date" in cols:
            self._migrate_old_table()

    def _migrate_old_table(self):
        """旧表 (date, code) 主键 -> 新表 (buy_date, code)，保留历史数据。"""
        rows = self.con.execute(
            "SELECT date,code,name,open_gap,buy_price,volume,sell_date,sell_price,pnl_pct "
            "FROM sim_positions").fetchall()
        self.con.execute("DROP TABLE sim_positions")
        self.con.executescript(SCHEMA)
        for r in rows:
            self.con.execute(
                "INSERT OR REPLACE INTO sim_positions(buy_date,code,name,open_gap,buy_price,"
                "volume,sell_date,sell_price,pnl_pct) VALUES(?,?,?,?,?,?,?,?,?)", r)
        self.con.commit()

    # ---------- 资金 ----------

    def _realized_pnl(self) -> float:
        """已实现盈亏（元）：卖出金额 - 买入成本（含双边冲击成本）。"""
        row = self.con.execute(
            "SELECT COALESCE(SUM(sell_price*volume*(1-?)),0) FROM sim_positions "
            "WHERE sell_date IS NOT NULL", (_impact(),)).fetchone()
        sell_amt = row[0]
        row = self.con.execute(
            "SELECT COALESCE(SUM(buy_price*volume*(1+?)),0) FROM sim_positions "
            "WHERE sell_date IS NOT NULL", (_impact(),)).fetchone()
        buy_amt = row[0]
        return sell_amt - buy_amt

    def balance(self) -> dict:
        d = time.strftime("%Y-%m-%d")
        today_buy = self.con.execute(
            "SELECT COALESCE(SUM(buy_price*volume*(1+?)),0) FROM sim_positions "
            "WHERE buy_date=?", (_impact(), d)).fetchone()[0]
        open_buy_all = self.con.execute(
            "SELECT COALESCE(SUM(buy_price*volume*(1+?)),0) FROM sim_positions "
            "WHERE sell_date IS NULL", (_impact(),)).fetchone()[0]
        open_mv = self.con.execute(
            "SELECT COALESCE(SUM(buy_price*volume),0) FROM sim_positions "
            "WHERE sell_date IS NULL").fetchone()[0]
        realized = self._realized_pnl()
        cash = _initial_cash() + realized - open_buy_all
        total = cash + open_mv
        return {"cash": round(cash, 2), "market_value": round(open_mv, 2),
                "total": round(total, 2), "realized_pnl": round(realized, 2),
                "today_buy_cost": round(today_buy, 2)}

    # ---------- 持仓 ----------

    def positions(self, open_only=True) -> list:
        sql = ("SELECT code,name,buy_date,buy_price,volume,streak,open_gap FROM sim_positions")
        if open_only:
            sql += " WHERE sell_date IS NULL"
        out = []
        for r in self.con.execute(sql + " ORDER BY buy_date"):
            out.append({"code": r[0], "name": r[1], "buy_date": r[2],
                        "avg_price": r[3], "volume": r[4], "streak": r[5] or 0,
                        "open_gap": r[6]})
        return out

    def buy_limit(self, code: str, price: float, amount_yuan: float,
                  sig: dict = None) -> dict:
        """模拟成交：按委托价+冲击成本买入。sig 需含 name/open_gap/streak/source/reason。"""
        sig = sig or {}
        d = time.strftime("%Y-%m-%d")
        vol = int(amount_yuan / price / 100) * 100
        if vol < 100:
            return {"ok": False, "reason": "金额不足一手"}
        fill = price * (1 + _impact())
        self.con.execute(
            "INSERT OR REPLACE INTO sim_trades VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), d, code, sig.get("name") or "",
             "BUY", round(fill, 3), vol, round(fill * vol, 2),
             sig.get("open_gap"), sig.get("source"), sig.get("reason") or ""))
        self.con.execute(
            "INSERT OR REPLACE INTO sim_positions(buy_date,code,name,open_gap,buy_price,"
            "volume,streak) VALUES(?,?,?,?,?,?,?)",
            (d, code, sig.get("name") or "", sig.get("open_gap"),
             round(fill, 3), vol, int(sig.get("streak") or 0)))
        self.con.commit()
        return {"ok": True, "volume": vol, "price": round(fill, 3),
                "amount": round(fill * vol, 2)}

    def sell_limit(self, code: str, price: float, volume: int = None,
                   sig: dict = None) -> dict:
        """模拟卖出：全部持仓平掉。price 为委托价，扣冲击成本后成交。"""
        sig = sig or {}
        row = self.con.execute(
            "SELECT buy_date, buy_price, volume FROM sim_positions "
            "WHERE code=? AND sell_date IS NULL ORDER BY buy_date LIMIT 1",
            (code,)).fetchone()
        if not row:
            return {"ok": False, "reason": "模拟盘无此持仓"}
        buy_date, buy_price, vol = row
        d = time.strftime("%Y-%m-%d")
        fill = price * (1 - _impact())
        pnl = (fill / buy_price - 1) * 100
        self.con.execute(
            "INSERT OR REPLACE INTO sim_trades VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), d, code, sig.get("name") or "",
             "SELL", round(fill, 3), vol, round(fill * vol, 2),
             None, sig.get("source") or "strategy", sig.get("reason") or ""))
        self.con.execute(
            "UPDATE sim_positions SET sell_date=?, sell_price=?, sell_reason=?, pnl_pct=? "
            "WHERE buy_date=? AND code=?",
            (d, round(fill, 3), sig.get("reason") or "", round(pnl, 2), buy_date, code))
        self.con.commit()
        return {"ok": True, "volume": vol, "price": round(fill, 3),
                "pnl_pct": round(pnl, 2), "buy_date": buy_date}

    # ---------- 战报 ----------

    def summary(self) -> str:
        """模拟盘战绩摘要（供推送/日志）。"""
        closed = self.con.execute(
            "SELECT COUNT(*), ROUND(AVG(pnl_pct),2), "
            "ROUND(SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END)*1.0/COUNT(*)*100,1), "
            "ROUND(SUM(pnl_pct),2) "
            "FROM sim_positions WHERE sell_date IS NOT NULL").fetchone()
        open_n = self.con.execute(
            "SELECT COUNT(*) FROM sim_positions WHERE sell_date IS NULL").fetchone()[0]
        bal = self.balance()
        if not closed[0]:
            return ("模拟盘：总资产 %.0f 元（初始 %.0f），持仓 %d 笔，暂无平仓"
                    % (bal["total"], _initial_cash(), open_n))
        return ("模拟盘：总资产 %.0f 元（初始 %.0f，累计 %+.2f%%）| "
                "已平仓 %d 笔 胜率 %s%% 平均 %s%% 合计 %+.2f%% | 持仓中 %d 笔"
                % (bal["total"], _initial_cash(),
                   (bal["total"] / _initial_cash() - 1) * 100,
                   closed[0], closed[2], closed[1], closed[3], open_n))
