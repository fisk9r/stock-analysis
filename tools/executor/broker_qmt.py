# -*- coding: utf-8 -*-
"""miniQMT 券商适配模块（broker_qmt）——券商官方合规接口。

依赖（开通 miniQMT 权限后安装）：
  pip install xtquant          # 迅投官方库，QMT 客户端自带亦可
  QMT 客户端登录时必须勾选「独立交易 / 极简模式」

使用前配置（tools/executor/config.json 的 "qmt" 段，由用户后续填写）：
  {
    "qmt_path": "D:\\国金证券QMT交易端\\userdata_mini",   # QMT userdata_mini 路径
    "account_id": "你的资金账号",
    "account_type": "STOCK"                                # 普通 A 股账户
  }

设计：
  - 延迟 import xtquant：未安装/未配置时本模块仍可被加载（is_available()=False）
  - 只实现执行器需要的最小面：下单（限价）/ 查持仓 / 查可用资金 / 委托回报
  - 下单前所有指令已经过 risk_gate，本模块不再重复风控（单一职责）
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
ORDERS_LOG = os.path.join(ROOT, "qmt_orders.jsonl")


def is_available() -> bool:
    """xtquant 已安装且 config 里账户已配置才可用。"""
    cfg = load_config().get("qmt") or {}
    if not cfg.get("qmt_path") or not cfg.get("account_id"):
        return False
    try:
        import xtquant.xttrader  # noqa: F401
        return True
    except ImportError:
        return False


def load_config():
    p = os.path.join(ROOT, "config.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {}


class QmtBroker:
    """miniQMT 下单适配。账户信息由 config.json 提供。"""

    def __init__(self):
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount
        cfg = load_config()["qmt"]
        self.account = StockAccount(cfg["account_id"])
        self.trader = XtQuantTrader(cfg["qmt_path"], int(cfg.get("session_id") or 0))
        self.trader.start()
        # 阻塞连接，2 秒超时
        if not self.trader.connect():
            raise ConnectionError("QMT 连接失败：请确认客户端已启动并勾选「独立交易」模式")
        if self.trader.subscribe(self.account) != 0:
            raise ConnectionError("QMT 订阅账户失败：检查资金账号 %s" % cfg["account_id"])

    def balance(self) -> dict:
        acc = self.trader.query_stock_asset(self.account)
        return {"cash": acc.cash, "market_value": acc.market_value,
                "total": acc.total_asset}

    def positions(self) -> list:
        return [{"code": p.stock_code[-6:], "volume": p.volume,
                 "can_use": p.can_use_volume, "avg_price": p.avg_price}
                for p in self.trader.query_stock_positions(self.account)]

    def buy_limit(self, code: str, price: float, amount_yuan: float) -> dict:
        """按金额估算股数（A 股 100 股整手），限价委托买入。返回委托单号。"""
        vol = int(amount_yuan / price / 100) * 100
        if vol < 100:
            return {"ok": False, "reason": "金额不足一手（%.2f 元/股）" % price}
        from xtquant.xtconstant import STOCK_BUY
        seq = self.trader.order_stock(self.account, code, STOCK_BUY, vol, 0, price)
        self._log({"ts": _now(), "action": "BUY", "code": code,
                   "price": price, "volume": vol, "seq": seq})
        return {"ok": seq >= 0, "seq": seq, "volume": vol, "price": price}

    def sell_limit(self, code: str, price: float, volume: int = None) -> dict:
        pos = next((p for p in self.positions() if p["code"] == code), None)
        if not pos or pos["can_use"] <= 0:
            return {"ok": False, "reason": "无可卖持仓"}
        vol = min(volume or pos["can_use"], pos["can_use"]) // 100 * 100
        if vol <= 0:
            return {"ok": False, "reason": "可卖数量不足一手"}
        from xtquant.xtconstant import STOCK_SELL
        seq = self.trader.order_stock(self.account, code, STOCK_SELL, vol, 0, price)
        self._log({"ts": _now(), "action": "SELL", "code": code,
                   "price": price, "volume": vol, "seq": seq})
        return {"ok": seq >= 0, "seq": seq, "volume": vol, "price": price}

    def _log(self, rec: dict):
        with open(ORDERS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")
