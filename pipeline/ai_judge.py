# -*- coding: utf-8 -*-
"""多模型综合判断层：Hy3（宿主叙事文件）/ DeepSeek / Kimi / Qwen

职责：
  · 若 dist/ai_narrative.json 存在且日期匹配 → 视为 Hy3 引擎的判断（由宿主模型在自动化中撰写）。
  · 若 config/models.json 配置了 DeepSeek/Kimi/Qwen 的 api_key → 并行征询各模型对次日 A 股方向、
    重点标的、风险的判断，并综合成「多模型共识」。
  · 无外部密钥时优雅降级：仅用 Hy3 叙事或模板，不影响主流程。

外部模型均走 OpenAI 兼容 /chat/completions 接口；调用失败即跳过该模型。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

ROOT = store.ROOT
CFG_PATH = os.path.join(ROOT, "config", "models.json")
DIST = os.path.join(ROOT, "dist")


def load_model_config():
    if os.path.exists(CFG_PATH):
        try:
            return json.load(open(CFG_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _chat(base_url, api_key, model, prompt, timeout=40):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model, "temperature": 0.3,
        "messages": [
            {"role": "system", "content": "你是专业的A股复盘分析师，只输出紧凑JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % api_key},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    content = d["choices"][0]["message"]["content"]
    return json.loads(content)


def _build_prompt(data):
    m = data.get("meta", {})
    date = m.get("date", "")
    sent = data.get("market", {}).get("sentiment", {}) or {}
    cyc = data.get("market", {}).get("cycle", {}) or {}
    lus = data.get("limit_ups", []) or []
    rec = data.get("recommend", {}) or {}
    g = data.get("global_market") or {}
    top = sorted(lus, key=lambda x: -x.get("streak", 0))[:8]
    core = (rec.get("core") or [])[:5]
    ctx = (
        "【%s A股盘后数据】\n"
        "情绪温度计=%.1f(%s)；周期=%s；涨停%d只，最高%d连板；晋级率=%s，封板率=%s。\n"
        % (date, sent.get("score", 0), sent.get("label", ""), cyc.get("phase", ""),
           len(lus), max([x.get("streak", 0) for x in lus], default=0),
           sent.get("promote_rate"), sent.get("seal_rate"))
    )
    if g.get("available"):
        ctx += "外围：%s（A股次日上涨概率约%.0f%%）。\n" % (g.get("detail", ""), g.get("a_up_prob", 0))
    ctx += "高度板：" + "，".join("%s(%d板)" % (t.get("name"), t.get("streak", 0)) for t in top) + "\n"
    ctx += "核心候选：" + "，".join("%s(%d板,续板%.0f%%)" % (c.get("name"), c.get("streak", 0), c.get("p_continue", 0)) for c in core) + "\n"
    ctx += ("请基于以上给出判断，严格输出JSON："
            '{"direction":"看多/看空/中性","confidence":0-100,'
            '"key_picks":["代码或名称"],"risks":["风险点"],"comment":"一句话综述"}')
    return ctx


def _norm_verdict(v, name):
    if not isinstance(v, dict):
        return None
    d = (v.get("direction") or "").strip()
    if d not in ("看多", "看空", "中性"):
        # 兼容英文/其他表述
        if "多" in d or "bull" in d.lower():
            d = "看多"
        elif "空" in d or "bear" in d.lower():
            d = "看空"
        else:
            d = "中性"
    return {
        "model": name, "direction": d,
        "confidence": float(v.get("confidence") or 50),
        "key_picks": v.get("key_picks") or [],
        "risks": v.get("risks") or [],
        "comment": v.get("comment") or "",
    }


def judge(data):
    """返回共识结构；若无任何模型参与返回 None（调用方回退模板）。"""
    cfg = load_model_config()
    verdicts = []
    # Hy3：宿主模型在自动化中撰写的叙事文件
    ai_path = os.path.join(DIST, "ai_narrative.json")
    if os.path.exists(ai_path):
        try:
            ai = json.load(open(ai_path, encoding="utf-8"))
            if ai.get("bullets") and ai.get("date") == data.get("meta", {}).get("date"):
                verdicts.append({
                    "model": ai.get("generated_by", "Hy3"), "direction": "—",
                    "confidence": 80, "key_picks": [], "risks": [],
                    "comment": (ai.get("bullets") or [""])[0] if ai.get("bullets") else "",
                    "headline": ai.get("headline", ""), "bullets": ai.get("bullets", []),
                })
        except Exception:
            pass
    # 外部模型：DeepSeek / Kimi / Qwen
    prompt = _build_prompt(data)
    for name in ("deepseek", "kimi", "qwen"):
        mc = cfg.get(name)
        if not mc or not mc.get("api_key"):
            continue
        try:
            raw = _chat(mc.get("base_url"), mc["api_key"], mc.get("model"), prompt)
            nv = _norm_verdict(raw, name)
            if nv:
                verdicts.append(nv)
        except Exception as e:
            print("[ai_judge] %s 调用失败，跳过：%r" % (name, e))

    if not verdicts:
        return None
    # 共识：方向按票数+置信度加权，关键标的按提及次数汇总
    dir_score = {"看多": 0.0, "看空": 0.0, "中性": 0.0}
    for v in verdicts:
        w = (v.get("confidence") or 50) / 100.0
        if v["direction"] in dir_score:
            dir_score[v["direction"]] += w
    direction = max(dir_score, key=dir_score.get)
    avg_conf = round(sum(v.get("confidence", 50) for v in verdicts) / len(verdicts), 1)
    picks = {}
    for v in verdicts:
        for p in v.get("key_picks", []):
            picks[p] = picks.get(p, 0) + 1
    top_picks = sorted(picks.items(), key=lambda x: -x[1])[:6]
    risks = []
    for v in verdicts:
        risks.extend(v.get("risks", [])[:2])
    # Hy3 叙事若有 bullets，优先保留
    hy3 = next((v for v in verdicts if v.get("bullets")), None)
    consensus = {
        "models": [v["model"] for v in verdicts],
        "direction": direction,
        "confidence": avg_conf,
        "key_picks": [p for p, _ in top_picks],
        "risks": risks[:6],
        "comment": "；".join(v.get("comment", "") for v in verdicts if v.get("comment"))[:300],
        "hy3_headline": hy3.get("headline") if hy3 else None,
        "hy3_bullets": hy3.get("bullets") if hy3 else None,
        "n_models": len(verdicts),
    }
    return consensus


if __name__ == "__main__":
    # 仅做配置探测
    c = load_model_config()
    print("已配置外部模型：", [k for k in ("deepseek", "kimi", "qwen") if c.get(k, {}).get("api_key")] or "无（仅 Hy3）")
