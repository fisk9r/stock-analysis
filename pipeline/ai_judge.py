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
                  "model": "kimi-k2.6", "temperature": 1, "label": "Moonshot Kimi"},
    "qwen":      {"kind": "openai",   "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-max", "label": "阿里通义 Qwen"},
    # 智谱 GLM：保底模型。2026-08-27 实测本账号 key：
    #   glm-4-plus / glm-4.6 @通用端点 → 1113 余额不足（体验套餐不覆盖）；
    #   GLM Coding 端点（coding/paas/v4）的 glm-4.6 免费可用（吃体验套餐额度）。
    # glm-4.6 是 thinking 模型：正文在 message.content，思考在 reasoning_content；
    # 用 thinking.type=disabled 可关思考提速，部分网关不支持该字段时需去掉重试。
    "zhipu":     {"kind": "openai",   "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                  "model": "glm-4.6", "label": "智谱 GLM-4.6",
                  "thinking": {"type": "disabled"}},
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
    # Cloudflare Workers AI（2026-08-29 二次选型）：OpenAI 兼容端点，零额外成本
    # （复用部署站点的 CLOUDFLARE_API_TOKEN）。base_url 里 {CF_ACCOUNT_ID} 由
    # get_active_providers 动态解析（CI 的 CLOUDFLARE_ACCOUNT_ID / config 覆盖均可）。
    # 选型（2026-08-29 官方文档核对）：
    #   · glm-4.5-air 已从 CF 模型目录下线（docs 404）→ 弃用；
    #   · kimi-k2.6 / glm-5.3(-flash) / deepseek-v4 需 Workers Paid 付费计划，免费额度不可用；
    #   · 免费额度（10k Neurons/天）内最强中文模型 = glm-4.7-flash（GLM 家族中文/金融
    #     文本质量最好，131K 上下文，Reasoning+FC，$0.06/M input，日用量几百 Neurons）；
    #   · 热备链：qwen3-30b（中文次优最便宜）→ gpt-oss-120b（英文系最强推理）→ llama-3.3-70b。
    "cloudflare": {"kind": "openai",
                   "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1",
                   "model": "@cf/zai-org/glm-4.7-flash", "label": "Cloudflare GLM-4.7-Flash"},
    "cloudflare_qwen": {"kind": "openai",
                        "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1",
                        "model": "@cf/qwen/qwen3-30b-a3b-fp8", "label": "Cloudflare Qwen3-30B-A3B"},
    "cloudflare_gptoss": {"kind": "openai",
                          "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1",
                          "model": "@cf/openai/gpt-oss-120b", "label": "Cloudflare GPT-OSS-120B"},
    "cloudflare_llama": {"kind": "openai",
                         "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1",
                         "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "label": "Cloudflare Llama-3.3-70B"},
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


def _openai_chat(base_url, api_key, model, prompt, system, timeout, temperature=0.3,
                 max_tokens=None, extra=None, want_text=False):
    url = base_url.rstrip("/") + "/chat/completions"
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]

    def _do(use_rf, use_extra):
        payload = {"model": model, "temperature": temperature, "messages": messages}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if use_rf:
            payload["response_format"] = {"type": "json_object"}
        # GLM-4.6 等 thinking 模型的开关字段（如 {"thinking":{"type":"disabled"}}）
        if use_extra and extra:
            payload.update(extra)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % api_key},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        msg = d["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content.strip():
            # 思考模型偶发把正文吞进思考区：兜底取 reasoning_content（只取其中 JSON 部分）
            content = msg.get("reasoning_content") or ""
        return content

    # 重试矩阵（2026-08-27）：429 退避重试；400/422 可能是不支持 response_format
    # 或 thinking 字段，逐级去掉再试——保证"换脑保底链"在最弱环境下也能出结果。
    last = None
    combos = [(True, True), (False, True), (False, False), (False, False)]
    for attempt, (use_rf, use_extra) in enumerate(combos):
        try:
            return _do(use_rf, use_extra)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < len(combos) - 1:
                time.sleep(3 * min(attempt + 1, 3))
                continue
            if e.code in (400, 422) and attempt < len(combos) - 1:
                continue  # 下一个组合：去掉可疑字段再试
            break
        except Exception as e2:
            last = e2
            break
    if isinstance(last, Exception):
        raise last
    raise RuntimeError(str(last))


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
    # Cloudflare 账户 ID：CI 有 CLOUDFLARE_ACCOUNT_ID（部署 Pages 用），本地可 config 覆盖
    cf_account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID")
                  or _env_resolve((cfg.get("cloudflare") or {}).get("account_id")) or "")
    for name, preset in PROVIDER_PRESETS.items():
        pc = cfg.get(name) or {}
        key = _env_resolve(pc.get("api_key"))
        if not key:
            if name.startswith("cloudflare"):
                # CF 专用：key = CLOUDFLARE_API_TOKEN（部署已配置，零额外成本）
                key = (os.environ.get("CLOUDFLARE_API_TOKEN")
                       or _env_resolve(pc.get("account_id") and None) or "")
            else:
                key = os.environ.get("AI_%s_KEY" % name.upper()) \
                    or os.environ.get("%s_API_KEY" % name.upper())
        if not key:
            continue
        merged = dict(preset)
        merged.update({k: v for k, v in pc.items() if k != "api_key"})
        merged["api_key"] = key
        # {CF_ACCOUNT_ID} 占位符替换；无账户 ID 时该接口不可用
        if "{CF_ACCOUNT_ID}" in (merged.get("base_url") or ""):
            if not cf_account:
                continue
            merged["base_url"] = merged["base_url"].replace("{CF_ACCOUNT_ID}", cf_account)
        out[name] = merged
    return out


def _chat_once(name, cfg, prompt, system=None, timeout=40, max_tokens=None):
    kind = cfg.get("kind") or "openai"
    base = cfg.get("base_url")
    model = cfg.get("model")
    key = _env_resolve(cfg.get("api_key")) or cfg.get("api_key")
    system = system or _SYSTEM
    if not (base and model and key):
        raise RuntimeError("接口 %s 缺少 base_url / model / api_key" % name)
    temperature = cfg.get("temperature", 0.3)
    if kind == "anthropic":
        raw = _anthropic_chat(base, key, model, prompt, system, timeout)
    elif kind == "gemini":
        raw = _gemini_chat(base, key, model, prompt, system, timeout)
    else:
        raw = _openai_chat(base, key, model, prompt, system, timeout,
                           temperature=temperature, max_tokens=max_tokens,
                           extra=cfg.get("request_extra") or cfg.get("thinking"))
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
    """精简、只问 headline/bullets/outlook 的叙事 prompt。

    不复用共识(方向判断)长模板——曾因要求模型同时输出『方向/置信/标的/风险』+
    『标题/要点/展望』超长组合 JSON，导致 kimi-k2.6 在 45s 内生成不完而超时；
    且 _norm_narrative 只读 headline/bullets/outlook，组合 schema 下这些字段常缺失→静默返回 None。
    改为简短数据摘要 + 单一叙事 schema，输出短、不易超时、字段齐全。"""
    m = data.get("meta", {}) or {}
    date = m.get("date", "")
    sent = data.get("market", {}).get("sentiment", {}) or {}
    cyc = data.get("market", {}).get("cycle", {}) or {}
    lus = data.get("limit_ups", []) or []
    rec = data.get("recommend", {}) or {}
    core = rec.get("core") or []
    top = "、".join("%s(%d板)" % (r.get("name"), r.get("streak", 0) or 0)
                     for r in core[:5]) or "无"
    hi = max([r.get("streak", 0) for r in lus], default=0)
    summary = ("【%s A股盘后】情绪温度计%.1f(%s)，周期%s；涨停%d只，最高%d连板；"
               "核心候选：%s。" % (
                   date, sent.get("score", 0), sent.get("label", ""),
                   cyc.get("phase", ""), len(lus), hi, top))
    return summary + (
        "\n请撰写盘后综述，严格只输出JSON："
        '{"headline":"一句话标题","bullets":["要点1","要点2","要点3"],"outlook":"次日展望一句话"}'
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
    # 拒绝占位/垃圾输出（2026-08-28 实测：模型偶发只回 "..." 却被当成有效叙事，
    # 推送里出现三行「- ✨ ...」）。有实质内容的判定：去除标点后仍有 ≥4 个有效字符。
    def _substantial(s):
        t = "".join(ch for ch in str(s) if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
        return len(t) >= 4
    bullets = [b for b in bullets if _substantial(b)]
    if headline and not _substantial(headline):
        headline = ""
    if outlook and not _substantial(outlook):
        outlook = ""
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

    优先级：preferred，或 config/models.json 的 narrative_backup 列表；
    未配置时默认 GLM(zhipu) 优先 → kimi 次之 → 其余（2026-08-25 用户拍板）。
    置顶，其余按预设顺序逐个尝试；首个成功即返回，全部失败返回 None（调用方保留模板）。

    返回结构含 headline/bullets/outlook/generated_by/ai_generated/source。
    """
    cfg = load_model_config()
    if preferred is None:
        nb = cfg.get("narrative_backup")
        # 主力链（2026-08-29 用户拍板：AI 换成 Cloudflare，二次选型按官方模型目录核对）：
        # Cloudflare GLM-4.7-Flash 主力（免费额度内中文/金融质量最好）→ Cloudflare
        # Qwen3-30B → Cloudflare GPT-OSS-120B → zhipu → kimi（国产 key 末端兜底）。
        pref_list = nb if isinstance(nb, list) and nb else \
            ["cloudflare", "cloudflare_qwen", "cloudflare_gptoss", "zhipu", "kimi"]
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
        nv = None
        # 内重试：Kimi 等国产模型的 json_mode 偶发抽风（返回非 JSON / 截断），
        # 首次拿到响应但解析为 None 时，再试一次往往成功；真正抛异常（429/超时）才跳下一接口。
        for _try in range(2):
            try:
                raw = _chat_once(name, providers[name], prompt, system=system,
                                timeout=90, max_tokens=700)
            except Exception as e:
                print("[ai_judge] 备用叙事 %s 失败，尝试下一接口：%r" % (name, e))
                break
            nv = _norm_narrative(raw, providers[name].get("label", name))
            if nv:
                break
        if nv:
            print("[ai_judge] HY3 不可用，已用备用模型 %s 生成叙事" % name)
            return nv
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
