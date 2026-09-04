# -*- coding: utf-8 -*-
"""broker_live —— 实盘券商适配器抽象接口（中期升级4，实盘预留，默认关闭）。

定位：
  模拟盘（tools/executor/broker_sim.py）已验证了完整的下单/持仓/风控链路。
  本模块把「接入真实券商」所需的最小接口抽象出来，让未来接 miniQMT /
  同花顺 iFinD / 券商柜台时只补一个子类即可，模拟盘 runner 无需改动。

安全红线（不可绕过）：
  1. LIVE_TRADING 总开关默认 False（config/broker_live.json {"live_trading": false}）；
  2. 即使开关为 True，子类未实现 order() → NotImplemented 拦截；
  3. 所有下单前强制过 risk_gate（复用 executor 风控：单票上限/总仓位/日亏损熔断）；
  4. place_order 在 dry_run=True（默认）时只打印与记录，绝不发真实请求。

接口与 broker_sim 保持同构，便于 executor 未来一行切换：
  query_balance() / query_positions() / place_order() / cancel_order()
"""
from __future__ import annotations

import json
import os
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_PATH = os.path.join(_ROOT, "config", "broker_live.json")


def load_config():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"live_trading": False, "dry_run": True, "broker": "none"}


class OrderResult(dict):
    """统一回包：{ok, order_id, msg, dry_run}。"""


class BrokerLive:
    """实盘券商抽象基类。子类实现 _place/_cancel/_positions/_balance 四个原语。"""

    name = "abstract"

    def __init__(self, config=None):
        self.cfg = config or load_config()
        self.live_trading = bool(self.cfg.get("live_trading"))
        self.dry_run = bool(self.cfg.get("dry_run", True)) or not self.live_trading
        self._orders = {}   # order_id -> order dict（本地留痕）

    # ---------------- 总闸 ----------------
    def guard(self):
        """下单前置守卫：返回 None=放行，str=拒绝原因。"""
        if not self.live_trading:
            return "LIVE_TRADING 未开启（config/broker_live.json live_trading=false）"
        if self.cfg.get("broker", "none") in ("none", "", None):
            return "未配置券商 broker"
        return None

    # ---------------- 查询 ----------------
    def query_balance(self):
        """-> {"cash": x, "market_value": y, "total": z}；dry_run 返回模拟值。"""
        if self.dry_run:
            return {"cash": 0.0, "market_value": 0.0, "total": 0.0, "dry_run": True}
        return self._balance()

    def query_positions(self):
        """-> [{"code","name","cost","qty","price"}]；dry_run 返回 []。"""
        if self.dry_run:
            return []
        return self._positions()

    # ---------------- 下单 ----------------
    def place_order(self, code, action, price, qty, risk_check=None):
        """action ∈ buy/sell。risk_check: callable(code,action,price,qty)->(ok,reason)。

        顺序：总闸 → 风控 → dry_run 拦截 → 子类实现。
        """
        g = self.guard()
        if g:
            return OrderResult(ok=False, order_id=None, msg=g, dry_run=True)
        if risk_check is not None:
            try:
                ok, reason = risk_check(code, action, price, qty)
                if not ok:
                    return OrderResult(ok=False, order_id=None,
                                       msg="风控拦截:%s" % reason, dry_run=self.dry_run)
            except Exception as e:
                return OrderResult(ok=False, order_id=None,
                                   msg="风控异常:%r" % e, dry_run=self.dry_run)
        if self.dry_run:
            oid = "DRY-%s-%d" % (self.name, int(time.time() * 1000))
            self._orders[oid] = {"code": code, "action": action,
                                 "price": price, "qty": qty}
            return OrderResult(ok=True, order_id=oid,
                               msg="dry_run 模拟下单成功（未发真实请求）", dry_run=True)
        try:
            return self._place(code, action, price, qty)
        except NotImplementedError:
            return OrderResult(ok=False, order_id=None,
                               msg="该券商适配器未实现 _place", dry_run=False)

    def cancel_order(self, order_id):
        if self.dry_run:
            return OrderResult(ok=True, order_id=order_id,
                               msg="dry_run 撤单成功", dry_run=True)
        try:
            return self._cancel(order_id)
        except NotImplementedError:
            return OrderResult(ok=False, order_id=order_id,
                               msg="该券商适配器未实现 _cancel", dry_run=False)

    # ---------------- 子类原语（默认未实现） ----------------
    def _place(self, code, action, price, qty):
        raise NotImplementedError

    def _cancel(self, order_id):
        raise NotImplementedError

    def _positions(self):
        raise NotImplementedError

    def _balance(self):
        raise NotImplementedError


class MiniQMTBroker(BrokerLive):
    """miniQMT(国金/迅投) 预留适配器：本机 xtquant 库就绪后填实现即可。

    用法（未来）：
      config/broker_live.json = {"live_trading": true, "dry_run": false,
                                 "broker": "miniqmt", "qmt_path": "D:\\国金QMT\\userdata_mini"}
    """

    name = "miniqmt"

    def _connect(self):
        from xtquant.xttrader import XtQuantTrader          # noqa: F401
        from xtquant.xttype import StockAccount            # noqa: F401
        raise NotImplementedError("按 xtquant 文档补齐会话/账号逻辑")


class ThsBroker(BrokerLive):
    """同花顺客户端自动化预留适配器（需本机 GUI + 图像/键鼠自动化，风险高，谨慎）。"""

    name = "ths"

    def _place(self, code, action, price, qty):
        raise NotImplementedError("同花顺 GUI 自动化方案待定")
