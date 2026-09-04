# -*- coding: utf-8 -*-
"""kronos_official —— 官方 Kronos 模型适配器（中期升级1，可选 GPU 推理，懒加载）。

背景：
  全站已内置纯 Python 蒸馏 kronos_lite（零依赖、CPU 可跑）。
  本模块是【可选】的官方模型通道：若用户本机装了 torch + transformers 且愿意下载
  shiyu-coder/Kronos-mini（4.1M 参数），可用官方模型预测下一根 K 线，作为
  kronos_lite 的增强信号源。CI（CPU-only、零依赖）永远不会 import torch。

设计：
  - 懒加载：import torch/transformers 推迟到 predict() 首次调用；
  - 优雅降级：任何一步失败 → available() 返回 False，调用方回落 kronos_lite；
  - 开关：config/kronos_official.json {"enabled": true, "device": "cpu|cuda"}，
    默认 enabled=false（与全站零依赖约束一致）；
  - 无网络/下载失败 → 降级，不阻塞任何主流程。

用法：
    from kronos_official import KronosOfficial
    ko = KronosOfficial()
    if ko.available():
        pred = ko.predict_next(bars)   # -> {"o","h","l","c","v"} 或 None
    else:
        ...  # 回落 kronos_lite
"""
from __future__ import annotations

import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_PATH = os.path.join(_ROOT, "config", "kronos_official.json")

REPO_ID = "shiyu-coder/Kronos-mini"
TOKENIZER_FILES = ["tokenizer/Kronos_tokenizer_h.bin"]  # 官方 mini 配套 tokenizer


def load_config():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "device": "cpu"}


class KronosOfficial:
    """官方 Kronos 适配器。全部方法容错，绝不抛出到主流程。"""

    def __init__(self, config=None):
        self.cfg = config or load_config()
        self._model = None
        self._tokenizer = None
        self._predictor = None
        self._tried = False
        self._err = None

    def enabled(self):
        return bool(self.cfg.get("enabled"))

    def available(self):
        """torch/transformers/模型权重是否就绪（首次调用会真正尝试加载）。"""
        if not self.enabled():
            return False
        if self._tried:
            return self._predictor is not None
        self._tried = True
        try:
            self._load()
        except Exception as e:
            self._err = "%r" % e
            self._predictor = None
        return self._predictor is not None

    def last_error(self):
        return self._err

    # ------------------------------------------------------------------
    def _load(self):
        import torch  # noqa: F401  (懒加载，缺失则降级)
        from kronos.model import Kronos, KronosPredictor
        from kronos.tokenizer import KronosTokenizer

        device = self.cfg.get("device") or "cpu"
        self._tokenizer = KronosTokenizer.from_pretrained(REPO_ID)
        self._model = Kronos.from_pretrained(REPO_ID)
        self._predictor = KronosPredictor(
            self._model, self._tokenizer, device=device,
            max_context=self.cfg.get("max_context", 512))

    def predict_next(self, bars, T=1):
        """bars: [{d,o,h,l,c,v}, ...] 升序 → 下一根 K 线预测 dict / None。

        无未来函数风险：仅用于「盘前对当日K线的预估参考」，不回填历史。
        """
        if not self.available():
            return None
        try:
            import pandas as pd
            df = pd.DataFrame([{
                "open": float(b["o"]), "high": float(b["h"]),
                "low": float(b["l"]), "close": float(b["c"]),
                "volume": float(b.get("v") or 0),
                "amount": float(b.get("amt") or 0)} for b in bars[-256:]])
            pred = self._predictor.predict(
                df, T=T, top_k=self.cfg.get("top_k", 0.9),
                top_p=self.cfg.get("top_p", 0.95),
                sample_count=self.cfg.get("sample_count", 1))
            row = pred.iloc[-1] if hasattr(pred, "iloc") else pred[-1]
            return {"o": round(float(row["open"]), 3),
                    "h": round(float(row["high"]), 3),
                    "l": round(float(row["low"]), 3),
                    "c": round(float(row["close"]), 3),
                    "v": float(row.get("volume") or 0)}
        except Exception as e:
            self._err = "predict: %r" % e
            return None


# 模块级单例（build 等调用方直接 from kronos_official import OFFICIAL）
OFFICIAL = KronosOfficial()
