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
    "max_position_pct": 0.70,     # 单票最大仓位占总资金比例（硬顶，防御用；2026-09-03 放松：允许强信号集中到 65%）
    "max_positions": 4,           # 最多同时持仓只数（安全上限，非刚性分仓；情况好可梭哈1支或分仓2支）
    "base_position_pct": 0.25,    # 单票基础仓位（兼容旧公式；新公式改用 grade_pct）
    "grade_pct": {"A": 0.65, "B": 0.55, "T": 0.50, "C": 0.30},  # 2026-09-03 用户拍板：按评级定单票目标仓位（不再死守3331/3322），强信号可集中
    "max_orders_per_day": 6,      # 单日最大委托笔数（>max_positions 以便卖出后回补/换仓）
    "min_trade_amount": 1000,     # 单笔最小金额（元）——低于此不买（防几千块的无效小仓）
    "max_trade_amount": 60000,    # 单笔绝对硬顶（元）——防御兜底，正常按仓位百分比算
    "daily_loss_stop_pct": -3.0,  # 当日组合亏损熔断线（%）
    "enabled": True,              # 总开关（False = 只记录不下单）
}


def _default_state():
    return {"trades": [], "circuit_break": None, "day": "", "orders_today": 0,
            "day_base": None}


def _load_state():
    """2026-09-01 修复（致命）：旧实现只在「文件不存在」时才返回含 trades 的默认结构。
    reset_sim.py 重置模拟盘时把 risk_state.json 写成空 {}，文件存在 → json.load 成功
    → self.state["trades"] 直接 KeyError('trades') → 此后所有 BUY 路径（开仓/尾盘）
    全部崩在风控闸门，推送报「开仓数据拉取失败」（2026-09-01 重置后当天实证）。
    修复：无论来源，加载后强制补齐默认键并做类型校验。"""
    st = _default_state()
    if os.path.exists(STATE_PATH):
        try:
            loaded = json.load(open(STATE_PATH, encoding="utf-8"))
            if isinstance(loaded, dict):
                st.update(loaded)
        except Exception:
            pass
    if not isinstance(st.get("trades"), list):
        st["trades"] = []
    if not isinstance(st.get("orders_today"), int):
        st["orders_today"] = 0
    if "circuit_break" not in st:
        st["circuit_break"] = None
    if not st.get("day"):
        st["day"] = ""
    return st


def _save_state(st):
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def day_base():
    """当日开盘基准总资产（2026-09-05 #482：熔断口径从「累计回撤」改「当日亏损」）。

    旧口径 bug：_daily_loss_check 用 total/init_cash-1（累计收益）当「当日组合
    亏损」喂 check_daily_loss——模拟盘累计回撤 3%（可能由数日累积、甚至某日
    亏损后一直没恢复）即触发熔断，且熔断永不自动复位 → 永久停摆。
    正确口径：当日亏损 = 现总资产 / 当日开盘基准 - 1。
    基准在「当日首次查询」时落盘（risk_state.day_base），当日内所有轮次共用，
    次日 RiskGate.__init__ 跨日重置 day 时同步作废。
    """
    st = _load_state()
    today = time.strftime("%Y-%m-%d")
    base = st.get("day_base") or {}
    val = base.get("total") if (base or {}).get("date") == today else None
    if val is None or val <= 0:
        # 首次查询：落盘当日基准（不覆盖已有当日值——并发轮次安全）
        st["day_base"] = {"date": today, "total": float(val or 0)}
        # 调用方先写真实基准：见 set_day_base；这里读不到就返回 None
        b = st.get("day_base") or {}
        v2 = b.get("total") if (b or {}).get("date") == today else None
        if not v2 or v2 <= 0:
            return None
        return float(v2)
    return float(val)


def set_day_base(total: float):
    """记录当日开盘基准总资产（当日已有值则保留，幂等）。"""
    st = _load_state()
    today = time.strftime("%Y-%m-%d")
    base = st.get("day_base") or {}
    if base.get("date") != today or not base.get("total"):
        st["day_base"] = {"date": today, "total": float(total)}
        _save_state(st)


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
            # 2026-09-05 #482 修复（熔断跨日不自动复位）：daily_loss_stop 的语义
            # 是「当日组合亏损熔断线」——当日熔断当日止。旧实现 trip() 后永不
            # 自动复位（恢复需人工删 risk_state.json 的 circuit_break），
            # 纯云端托管下用户不开电脑 → 一旦熔断（甚至误熔断，见下）模拟盘
            # 永久停摆。跨日时自动清除熔断：每日开闸一次，熔断保护的是
            # 「当日剩余时段不再开新仓」，不是「从此永不再开仓」。
            # （连亏纪律 loss_streak ≥3 暂停开新仓的跨日约束仍然生效，兜底仍在。）
            if self.state.get("circuit_break"):
                self.state["circuit_break"] = None
            # 2026-09-05 #482：当日开盘基准同步作废（跨日重置）
            if (self.state.get("day_base") or {}).get("date") != today:
                self.state["day_base"] = None
            _save_state(self.state)

    @property
    def tripped(self):
        return self.state.get("circuit_break") is not None

    def trip(self, reason: str):
        """熔断：写盘。当日剩余时段拦截所有 BUY（SELL 不受限）。
        2026-09-05 #482：跨日由 __init__ 自动复位（当日熔断当日止），
        不再需要人工删 risk_state.json 的 circuit_break。"""
        self.state["circuit_break"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
        _save_state(self.state)

    def resume(self):
        self.state["circuit_break"] = None
        _save_state(self.state)

    def check(self, sig: dict, total_asset: float = None,
               current_positions: int = None) -> dict:
        """检查一条 BUY 信号。返回 {ok: bool, reason: str, amount: int}。

        current_positions：当前已持仓只数（不含本笔）。传了就强制 max_positions 约束，
        达到上限直接拒单（这是 3331/3322 分仓的总闸，之前只查 orders_per_day 形同虚设）。
        """
        if not self.cfg.get("enabled"):
            return {"ok": False, "reason": "闸门总开关关闭（enabled=false）", "amount": 0}
        if self.tripped:
            cb = self.state["circuit_break"]
            return {"ok": False,
                    "reason": "熔断中（%s：%s），人工恢复后才可交易"
                              % (cb["at"], cb["reason"]), "amount": 0}
        code = sig.get("code")
        # 幂等：同 code 当日已**委托买入**则拒绝。
        # 2026-09-05 #481 修复（幂等误杀）：旧实现查「当日 trades 里出现该 code 的
        # 任何记录」——WATCH/SKIP/REJECT 也算。后果：09:25 竞价判 WATCH（平开观望）
        # 的票，14:45 尾盘确认或盘中巡逻轮到达买点时被「今日已委托」误拒，静默
        # 跳过——多时点机动买入形同虚设。幂等的语义边界是「已真实买入的票不再买」，
        # 观望/放弃留痕不锁单（等形态确认后各通道仍可买）。
        today_trades = [t for t in self.state["trades"]
                        if t.get("date") == self.state["day"]
                        and t.get("verdict") == "BUY"]
        if any(t.get("code") == code for t in today_trades):
            return {"ok": False, "reason": "幂等拒绝：%s 今日已买入" % code, "amount": 0}
        # 最多持仓只数（3331/3322 分仓上限）
        if current_positions is not None and current_positions >= self.cfg["max_positions"]:
            return {"ok": False,
                    "reason": "已达最大持仓只数 %d（安全上限，非刚性分仓），本笔拒绝"
                              % self.cfg["max_positions"], "amount": 0}
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
        """盘中组合亏损检查（由 runner 定期喂**当日**浮亏 %——非累计口径，
        2026-09-05 #482：累计收益当当日亏损喂入会把「历史回撤」误判成
        「今日爆亏」，触发不必要的熔断且永不复位）。"""
        if pnl_pct <= self.cfg["daily_loss_stop_pct"] and not self.tripped:
            self.trip("当日组合亏损 %.2f%% 触发熔断线 %.2f%%"
                      % (pnl_pct, self.cfg["daily_loss_stop_pct"]))
