# -*- coding: utf-8 -*-
"""多模型综合判断层 / 可切换 AI 接口矩阵。

支持接口（OpenAI 兼容 / Anthropic 原生 / Gemini 原生 三种协议）：
  openai    GPT-5.x          https://api.openai.com/v1
  deepseek  DeepSeek          https://api.deepseek.com/v1
  kimi      Kimi (K2)         https://api.moonshot.cn/v1
  qwen      通义 Qwen         https://dashscope.aliyuncs.com/compatible-mode/v1
  zhipu     智谱 GLM          https://open.bigmodel.cn/api/paas/v4
  doubao    字节豆包          https://ark.cn-beijing.volces.com/api/v3
  hunyuan   腾讯混元          https://api.hunyuan.cloud.tencent.com/v1
  yi        零一万物          https://api.lingyiwanwu.com/v1
  grok      xAI Grok          https://api.x.ai/v1
  anthropic Claude (原生)     https://api.anthropic.com/v1
  gemini    Google Gemini     https://generativelanguage.googleapis.com/v1beta (原生)

工作机制：
  · 宿主 Hy3 叙事：若 dist/ai_narrative.json 存在且日期匹配 → 作为 Hy3 判断参与共识。
  · 配置即启用：在 config/models.json 给某接口填 api_key（或设环境变量 AI_<NAME>_KEY）
    即并行征询其对次日 A 股方向 / 重点标的 / 风险的判断，并与其他模型形成共识。
  · 无密钥 / 调用失败 → 优雅跳过，不影响主流程；多模型共识降级为可用部分，全无则回退模板。
  · 换脑兜底：若 Hy3 不可用，只要任一外部接口可用，共识照常产出——这就是"换模型"的兜底。
  · 叙事备用（HY3 缺席时）：若 dist/ai_narrative.json 不存在/日期不符（即 HY3 未撰写），
    自动用首选备用模型（默认 kimi，可在 config/models.json 的 narrative_backup 指定）生成
    同格式叙事（headline/bullets/outlook），避免退回冷模板。

密钥来源（二选一，均不入库）：
  1) config/models.json（已被 .gitignore 忽略，本地保存）；
  2) 环境变量 AI_OPENAI_KEY / AI_ANTHROPIC_KEY / AI_GEMINI_KEY ...（适合 GitHub Actions 用 Secrets 注入）。

依赖：仅标准库（urllib），无第三方包要求。
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

ROOT = store.ROOT
CFG_PATH = os.path.join(ROOT, "config", "models.json")
DIST = os.path.join(ROOT, "dist")

_SYSTEM = "你是专业的A股复盘分析师，只输出紧凑JSON，不要解释。"

# 接口预设：用户可在 config/models.json 覆盖 base_url / model / kind。
PROVIDER_PRESETS = {
    "openai":    {"kind": "openai",   "base_url": "https://api.openai.com/v1",
                  "model": "gpt-5.1", "label": "OpenAI GPT"},
    "deepseek":  {"kind": "openai",   "base_url": "https://api.deepseek.com/v1",
                  "model": "deepseek-chat", "label": "DeepSeek"},
    "kimi":      {"kind": "openai",   "base_url": "https://api.moonshot.cn/v1",
                  "model": "kimi-k2", "label": "Moonshot Kimi"},
    "qwen":      {"kind": "openai",   "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-max", "label": "阿里通义 Qwen"},
    "zhipu":     {"kind": "openai",   "base_url": "https://open.bigmodel.cn/api/paas/v4",
                  "model": "glm-4-plus", "label": "智谱 GLM"},
    "doubao":    {"kind": "openai",   "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                  "model": "doubao-seed-1-6-250615", "label": "字节豆包 Doubao"},
    "hunyuan":   {"kind": "openai",   "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
                  "model": "hunyuan-turbo", "label": "腾讯混元 Hunyuan"},
    "yi":        {"kind": "openai",   "base_url": "https://api.lingyiwanwu.com/v1",
                  "model": "yi-large", "label": "零一万物 Yi"},
    "grok":      {"kind": "openai",   "base_url": "https://api.x.ai/v1",
                  "model": "grok-4", "label": "xAI Grok"},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1",
                  "model": "claude-sonnet-4-6", "label": "Anthropic Claude"},
    "gemini":    {"kind": "gemini",   "base_url": "https://generativelanguage.googleapis.com/v1beta",
                  "model": "gemini-2.5-pro", "label": "Google Gemini"},
}


def load_model_config():
    if os.path.exists(CFG_PATH):
        try:
            return json.load(open(CFG_PATH, encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def _env_resolve(v):
    """支持把 api_key 写成 \"${ENV:OPENAI_API_KEY}\" 从环境变量取值。"""
    if isinstance(v, str) and v.startswith("${ENV:") and v.endswith("}"):
        return os.environ.get(v[6:-1], "")
    return v


def _extract_json(text):
    """从模型返回文本中尽量解析出 JSON 对象（容忍 ```json 围栏 / 前后废话）。"""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", t, re.S)
        if m:
            t = m.group(1)
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b != -1 and b > a:
        t = t[a:b + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


def _openai_chat(base_url, api_key, model, prompt, system, timeout):
    url = base_url.rstrip("/") + "/chat/completions"
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]

    def _do(use_rf):
        payload = {"model": model, "temperature": 0.3, "messages": messages}
        if use_rf:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % api_key},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return d["choices"][0]["message"]["content"]

    try:
        return _do(True)
    except urllib.error.HTTPError:
        # 部分国产兼容接口不支持 response_format，去掉再试一次
        return _do(False)


def _anthropic_chat(base_url, api_key, model, prompt, system, timeout):
    url = base_url.rstrip("/") + "/messages"
    payload = {
        "model": model, "max_tokens": 1024, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    return d["content"][0]["text"]


def _gemini_chat(base_url, api_key, model, prompt, system, timeout):
    url = "%s/models/%s:generateContent?key=%s" % (base_url.rstrip("/"), model, api_key)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    return d["candidates"][0]["content"]["parts"][0]["text"]


def get_active_providers():
    """返回 (name -> 有效配置) 字典，配置来源为 models.json 或环境变量 AI_<NAME>_KEY。"""
    cfg = load_model_config()
    out = {}
    for name, preset in PROVIDER_PRESETS.items():
        pc = cfg.get(name) or {}
        key = _env_resolve(pc.get("api_key"))
        if not key:
            key = os.environ.get("AI_%s_KEY" % name.upper()) \
                or os.environ.get("%s_API_KEY" % name.upper())
        if not key:
            continue
        merged = dict(preset)
        merged.update({k: v for k, v in pc.items() if k != "api_key"})
        merged["api_key"] = key
        out[name] = merged
    return out


def _chat_once(name, cfg, prompt, system=None, timeout=40):
    kind = cfg.get("kind") or "openai"
    base = cfg.get("base_url")
    model = cfg.get("model")
    key = _env_resolve(cfg.get("api_key")) or cfg.get("api_key")
    system = system or _SYSTEM
    if not (base and model and key):
        raise RuntimeError("接口 %s 缺少 base_url / model / api_key" % name)
    if kind == "anthropic":
        raw = _anthropic_chat(base, key, model, prompt, system, timeout)
    elif kind == "gemini":
        raw = _gemini_chat(base, key, model, prompt, system, timeout)
    else:
        raw = _openai_chat(base, key, model, prompt, system, timeout)
    return _extract_json(raw)


def chat(name, prompt, system=None, model=None, timeout=40):
    """对单个已配置接口发起一次对话，返回解析后的 JSON（失败抛异常）。"""
    prov = get_active_providers().get(name)
    if not prov:
        raise RuntimeError("接口 %s 未配置（请在 config/models.json 填 api_key 或设 AI_%s_KEY）"
                           % (name, name.upper()))
    if model:
        prov = dict(prov)
        prov["model"] = model
    return _chat_once(name, prov, prompt, system, timeout)


# ----------------------- 共识（供 build.py 调用） -----------------------

def _build_prompt(data):
    m = data.get("meta", {}) or {}
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
    verdicts = []
    # Hy3：宿主模型在自动化中撰写的叙事文件（日期一致才采用）
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
    # 外部模型：所有已配置接口并行征询
    prompt = _build_prompt(data)
    for name, cfg in get_active_providers().items():
        try:
            raw = _chat_once(name, cfg, prompt)
            nv = _norm_verdict(raw, cfg.get("label", name))
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


# ----------------------- 叙事备用（HY3 缺席时） -----------------------

def _build_narrative_prompt(data):
    """在共识 prompt 基础上，要求模型输出与 Hy3 同格式的盘后综述。"""
    base = _build_prompt(data)
    return base + (
        "\n请撰写盘后综述，严格只输出JSON："
        '{"headline":"一句话标题","bullets":["3-5条要点"],"outlook":"次日展望一句话"}'
    )


def _norm_narrative(v, label):
    if not isinstance(v, dict):
        return None
    headline = (v.get("headline") or "").strip()
    bullets = v.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [b.strip() for b in bullets.replace("。", "\n").split("\n") if b.strip()]
    bullets = [str(b).strip(" 。") for b in bullets if str(b).strip()][:6]
    outlook = (v.get("outlook") or v.get("comment") or "").strip()
    if not (headline or bullets):
        return None
    return {
        "headline": headline,
        "bullets": bullets,
        "outlook": outlook,
        "generated_by": "%s（HY3 备用）" % label,
        "ai_generated": True,
        "source": "model-backup",
    }


def generate_narrative_backup(data, preferred=None):
    """HY3 叙事文件不可用时，用外部模型生成同格式叙事。

    优先级：preferred（默认 kimi，或在 config/models.json 的 narrative_backup 列表指定）
    置顶，其余按预设顺序逐个尝试；首个成功即返回，全部失败返回 None（调用方保留模板）。

    返回结构含 headline/bullets/outlook/generated_by/ai_generated/source。
    """
    cfg = load_model_config()
    if preferred is None:
        nb = cfg.get("narrative_backup")
        pref_list = nb if isinstance(nb, list) and nb else ["kimi"]
    elif isinstance(preferred, str):
        pref_list = [preferred]
    else:
        pref_list = list(preferred)
    providers = get_active_providers()
    if not providers:
        return None
    order = []
    for n in list(pref_list) + list(PROVIDER_PRESETS.keys()):
        if n in providers and n not in order:
            order.append(n)
    prompt = _build_narrative_prompt(data)
    system = "你是专业的A股复盘分析师，负责撰写盘后综述。严格只输出JSON，不要解释。"
    for name in order:
        try:
            raw = _chat_once(name, providers[name], prompt, system=system, timeout=45)
            nv = _norm_narrative(raw, providers[name].get("label", name))
            if nv:
                print("[ai_judge] HY3 不可用，已用备用模型 %s 生成叙事" % name)
                return nv
        except Exception as e:
            print("[ai_judge] 备用叙事 %s 失败，尝试下一接口：%r" % (name, e))
    return None


# ----------------------- CLI：接口自检 -----------------------

def _probe(name):
    """对单个接口做一次最小连通测试，返回 (ok, latency_s, err_or_none)。"""
    prov = get_active_providers().get(name)
    if not prov:
        return False, 0.0, "未配置（无 api_key）"
    t0 = time.time()
    try:
        r = _chat_once(name, prov,
                       '请严格只输出JSON：{"pong": true}',
                       system="你是连通性测试助手，只输出JSON。", timeout=30)
        if isinstance(r, dict) and r.get("pong") is True:
            return True, round(time.time() - t0, 2), None
        return True, round(time.time() - t0, 2), "返回非预期JSON: %r" % r
    except Exception as e:
        return False, round(time.time() - t0, 2), repr(e)


def _cli():
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    if arg == "list":
        print("已知 AI 接口预设（共 %d）：" % len(PROVIDER_PRESETS))
        active = set(get_active_providers().keys())
        for name, p in PROVIDER_PRESETS.items():
            flag = "●已配置" if name in active else "○未配置"
            print("  %-10s %-16s %-9s %s" % (name, p["label"], p["kind"], flag))
        return
    if arg == "test":
        names = [sys.argv[2]] if len(sys.argv) > 2 else list(get_active_providers().keys())
        if not names:
            print("没有任何已配置的接口（请在 config/models.json 填 api_key 或设 AI_<NAME>_KEY）。")
            return
        for n in names:
            ok, lat, err = _probe(n)
            if ok and not err:
                print("  ✓ %-10s 连通正常 (%.2fs)" % (n, lat))
            elif ok:
                print("  △ %-10s 连通但返回异常: %s" % (n, err))
            else:
                print("  ✗ %-10s 失败 (%s): %s" % (n, lat, err))
        return
    # status
    c = load_model_config()
    active = get_active_providers()
    print("已配置外部模型（%d）：%s" % (len(active), list(active.keys()) or "无（仅 Hy3 叙事）"))
    print("提示：运行 `python pipeline/ai_judge.py list` 查看全部预设；"
          "`python pipeline/ai_judge.py test` 自检所有已配置接口。")


if __name__ == "__main__":
    _cli()
