# -*- coding: utf-8 -*-
"""风控闸门（risk_gate）：所有委托指令必须先过闸门才能到达 broker。

设计原则（不可妥协）：
  1. 闸门先于一切下单逻辑，broker 收到的指令 100% 已过闸
  2. 熔断状态持久化（本地 json），进程重启不复位
  3. 幂等：同 code+同交易日只允许成交一次（防重复下单）
  4. 所有拒绝/放行都有记录，可审计
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "risk_state.json")

DEFAULTS = {
    "max_position_pct": 0.15,     # 单票最大仓位占总资金比例
    "max_orders_per_day": 6,      # 单日最大委托笔数
    "min_trade_amount": 1000,     # 单笔最小金额（元）
    "max_trade_amount": 20000,    # 单笔最大金额（元）——模拟盘/初期实盘硬顶
    "daily_loss_stop_pct": -3.0,  # 当日组合亏损熔断线（%）
    "enabled": True,              # 总开关（False = 只记录不下单）
}


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"trades": [], "circuit_break": None, "day": "", "orders_today": 0}


def _save_state(st):
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


class RiskGate:
    def __init__(self, config=None):
        cfg = dict(DEFAULTS)
        cfg.update(config or {})
        self.cfg = cfg
        self.state = _load_state()
        today = time.strftime("%Y-%m-%d")
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["orders_today"] = 0
            _save_state(self.state)

    @property
    def tripped(self):
        return self.state.get("circuit_break") is not None

    def trip(self, reason: str):
        """熔断：写盘 + 不再自动复位。恢复需人工删 risk_state.json 里的 circuit_break。"""
        self.state["circuit_break"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
        _save_state(self.state)

    def resume(self):
        self.state["circuit_break"] = None
        _save_state(self.state)

    def check(self, sig: dict, total_asset: float = None) -> dict:
        """检查一条 BUY 信号。返回 {ok: bool, reason: str, amount: int}。"""
        if not self.cfg.get("enabled"):
            return {"ok": False, "reason": "闸门总开关关闭（enabled=false）", "amount": 0}
        if self.tripped:
            cb = self.state["circuit_break"]
            return {"ok": False,
                    "reason": "熔断中（%s：%s），人工恢复后才可交易"
                              % (cb["at"], cb["reason"]), "amount": 0}
        code = sig.get("code")
        # 幂等：同 code 当日已委托则拒绝
        today_trades = [t for t in self.state["trades"] if t.get("date") == self.state["day"]]
        if any(t.get("code") == code for t in today_trades):
            return {"ok": False, "reason": "幂等拒绝：%s 今日已委托" % code, "amount": 0}
        if self.state["orders_today"] >= self.cfg["max_orders_per_day"]:
            self.trip("单日委托数超限 %d" % self.cfg["max_orders_per_day"])
            return {"ok": False, "reason": "单日委托数已达上限", "amount": 0}
        # 金额闸门
        amount = self.cfg["max_trade_amount"]
        if total_asset:
            by_pos = total_asset * self.cfg["max_position_pct"]
            amount = int(min(amount, by_pos))
        if amount < self.cfg["min_trade_amount"]:
            return {"ok": False, "reason": "可用资金不足最小单笔 %d 元" % self.cfg["min_trade_amount"],
                    "amount": 0}
        return {"ok": True, "reason": "过闸（金额 %d 元）" % amount, "amount": amount}

    def record(self, sig: dict, verdict: str, amount: int, detail: str = ""):
        """记录每条信号的处理结果（含拒绝），供审计与复盘。"""
        self.state["trades"].append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "date": self.state["day"],
            "code": sig.get("code"), "name": sig.get("name"),
            "verdict": verdict, "amount": amount,
            "open_gap": sig.get("open_gap"), "detail": detail,
        })
        if verdict == "BUY":
            self.state["orders_today"] += 1
        # 只保留最近 500 条
        self.state["trades"] = self.state["trades"][-500:]
        _save_state(self.state)

    def check_daily_loss(self, pnl_pct: float):
        """盘中组合亏损检查（由 runner 定期喂当日浮盈 %）。"""
        if pnl_pct <= self.cfg["daily_loss_stop_pct"] and not self.tripped:
            self.trip("当日组合亏损 %.2f%% 触发熔断线 %.2f%%"
                      % (pnl_pct, self.cfg["daily_loss_stop_pct"]))
