# -*- coding: utf-8 -*-
"""kronos_lite —— Kronos（K线→K线金融基础模型）思想的零依赖纯 Python 落地。

调研结论（2026-08-29，github.com/shiyu-coder/Kronos）：
- Kronos 是「OHLCV+amount 序列 → 未来 K 线」的基础模型，mini 4.1M / small 24.7M / base 102.3M。
- 官方实现需要 torch + GPU 推理，不符合本项目「CI CPU-only + 零运行时依赖」约束，
  **不引入原模型**；这里把它可复用的核心思想蒸馏成纯 Python 特征：

  1) 序列自相似 / 波动结构特征（Kronos 的 tokenizer 本质是 K 线形态离散化）：
     - 幅度序列 AMPl = ln(H/L)、实体占比、上下影占比的近 N 日统计
  2) 连续 N 日「下一根 K 线与上一根的增益结构」：动量持续性
     （对应 Kronos 预测的「下一根 K 线相对上一根的相对变化」）
  3) 量价配合的持续度：放量上行/缩量回调的健康结构打分

输出 kronos_score ∈ [0,100]，作为 engine.screen_uptrend 的趋势质量加成项，
也供 recommend() 的连板票做「结构健康度」参考。所有指标只用已有 K 线，
无未来函数、无网络请求、无第三方库。
"""
from __future__ import annotations

import math


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else 0.0


def kronos_features(bars: list) -> dict:
    """从 K 线列表（最新在末尾；字段 d/o/h/l/c/v，可选 amt/pct）提取 Kronos 式特征。

    返回 dict：amp_mean/amp_trend/body_ratio/up_shadow/dn_shadow/cont_up/cont_dn/
    pv_health/mom_persist。bars 不足 20 根时返回空 dict（调用方跳过加成）。
    """
    if not bars or len(bars) < 20:
        return {}
    b = bars[-20:]
    feats = {}

    # ---- 1) 波动幅度结构：ln(H/L) 近20日均值 + 近5日 vs 前15日的趋势 ----
    amps = []
    for k in b:
        try:
            h, l = float(k.get("h") or 0), float(k.get("l") or 0)
            if h > 0 and l > 0 and h >= l:
                amps.append(math.log(h / l))
        except (TypeError, ValueError):
            continue
    amp_mean = _mean(amps)
    amp_recent = _mean(amps[-5:]) if len(amps) >= 5 else amp_mean
    amp_prior = _mean(amps[:-5]) if len(amps) > 5 else amp_mean
    feats["amp_mean"] = round(amp_mean, 5)
    feats["amp_trend"] = round(amp_recent - amp_prior, 5)  # >0 = 波动放大

    # ---- 2) K 线形态：实体/上下影占比（tokenizer 的形态离散化思想）----
    bodies, ups, dns = [], [], []
    for k in b:
        try:
            o, c = float(k.get("o") or 0), float(k.get("c") or 0)
            h, l = float(k.get("h") or 0), float(k.get("l") or 0)
            rng = h - l
            if rng <= 0 or c <= 0:
                continue
            bodies.append(abs(c - o) / rng)
            ups.append((h - max(o, c)) / rng)
            dns.append((min(o, c) - l) / rng)
        except (TypeError, ValueError):
            continue
    feats["body_ratio"] = round(_mean(bodies), 3)
    feats["up_shadow"] = round(_mean(ups[-5:]) if len(ups) >= 5 else _mean(ups), 3)
    feats["dn_shadow"] = round(_mean(dns[-5:]) if len(dns) >= 5 else _mean(dns), 3)

    # ---- 3) 动量持续性：近5日里「今日收盘 vs 昨收」同向连续段长度 ----
    cont_up = cont_dn = cur_u = cur_d = 0
    prev_c = None
    for k in b:
        c = float(k.get("c") or 0)
        if prev_c and c > 0:
            if c > prev_c:
                cur_u += 1; cur_d = 0
            elif c < prev_c:
                cur_d += 1; cur_u = 0
            else:
                cur_u = cur_d = 0
            cont_up = max(cont_up, cur_u)
            cont_dn = max(cont_dn, cur_d)
        prev_c = c
    feats["cont_up"] = cont_up
    feats["cont_dn"] = cont_dn
    # 相对变化持续度（Kronos 预测目标的轻量代理）：近5日收益的和
    c20 = [float(k.get("c") or 0) for k in b]
    rets = [(c20[i] / c20[i - 1] - 1) for i in range(1, len(c20)) if c20[i - 1] > 0]
    feats["mom_persist"] = round(sum(rets[-5:]), 4) if len(rets) >= 5 else 0.0

    # ---- 4) 量价健康度：上涨日均量比 − 下跌日均量比（>0 = 涨有量跌缩量，健康）----
    vols = [float(k.get("v") or 0) for k in b]
    vavg = _mean(vols)
    up_rs, dn_rs = [], []
    if vavg > 0:
        for i in range(1, len(b)):
            if vols[i] <= 0:
                continue
            r = vols[i] / vavg
            if c20[i] > c20[i - 1]:
                up_rs.append(r)
            elif c20[i] < c20[i - 1]:
                dn_rs.append(r)
    pv = _mean(up_rs) - _mean(dn_rs)
    feats["pv_health"] = round(pv, 3)
    return feats


def kronos_score(feats: dict) -> float:
    """把特征聚合成 0~100 的结构健康分。

    权重设计（对齐回测结论）：
    - 动量持续性为主（Kronos 预测的核心目标），上限 40
    - 量价健康度上限 25（放量上涨健康、放量下跌危险）
    - 实体占比（趋势确定性）上限 15
    - 下影支撑（买盘承接）上限 10、上影压制扣分最多 -10
    - 波动放大（分歧加剧）轻微扣分 -5
    """
    if not feats:
        return 0.0
    s = 0.0
    mp = feats.get("mom_persist") or 0.0
    s += _clamp(mp / 0.15, -1.0, 1.0) * 40          # ±15% 五日累计 → 满格
    s += _clamp((feats.get("pv_health") or 0) * 12.5, -25, 25)
    s += _clamp((feats.get("body_ratio") or 0) - 0.3, 0, 0.3) / 0.3 * 15
    s += _clamp((feats.get("dn_shadow") or 0) - 0.1, 0, 0.2) / 0.2 * 10
    s -= _clamp((feats.get("up_shadow") or 0) - 0.15, 0, 0.25) / 0.25 * 10
    if (feats.get("amp_trend") or 0) > 0.004:        # 近5日波动显著放大
        s -= 5
    return round(_clamp(s, 0, 100), 1)


def annotate_bars(bars: list) -> float:
    """便捷入口：直接给 K 线列表，返回 kronos_score（bars 不足返回 0）。"""
    return kronos_score(kronos_features(bars))
