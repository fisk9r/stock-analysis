#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 cache/market.db 导出全市场 code->name 名录，生成 dist/stock_names.js。

产物格式（紧凑，约 70KB，gzip 后 ~20KB）：
    window.SA_NAMES = "600500:中化国际|002580:圣阳股份|...";
    window.SA_NAMES_TS = "2026-08-28";

用途：前端「自选股智能输入」离线纠错（输代码/名称/拼音，都能匹配到正确标的）。
联网时前端还会用腾讯 smartbox 做在线兜底（能匹配拼音与更全的标的）。
"""
import os
import sqlite3
import sys
import datetime

try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PY = True
except ImportError:
    _HAS_PY = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'cache', 'market.db')
OUT = os.path.join(ROOT, 'dist', 'stock_names.js')

# 拼音首字母表（用于「zhgj -> 中化国际」这类输入）
PY_INIT = {}
_PY_TABLE = """
a阿 ai爱 an安 ang昂 ao奥
ba八 bai白 ban班 bang邦 bao保 bei北 ben本 beng崩 bi比 bian边 biao标 bie别 bin宾 bing冰 bo波 bu不
ca擦 cai才 can参 cang仓 cao曹 ce策 cen岑 ceng层 cha查 chai柴 chan产 chang昌 chao超 che车 chen陈 cheng成 chi吃 chong充 chu出 chuan川 chuang创 chun春 chuo戳 ci次 cong从 cu粗 cuan窜 cui崔 cun村 cuo错
da打 dai代 dan单 dang当 dao到 de的 dei得 deng登 di地 dian点 diao调 die跌 ding定 diu丢 dong东 dou都 du读 duan段 dui对 dun吨 duo多
e鹅 en恩 er儿
fa发 fan反 fang方 fei飞 fen分 feng风 fo佛 fou否 fu福
ga嘎 gai改 gan干 gang刚 gao高 ge个 gei给 gen跟 geng更 gong工 gou购 gu古 gua瓜 guai乖 guan关 guang光 gui规 gun滚 guo国
ha哈 hai海 han汉 hang航 hao好 he和 hei黑 hen很 heng恒 hong红 hou后 hu胡 hua花 huai怀 huan欢 huang黄 hui回 hun婚 huo火
ji机 jia家 jian见 jiang江 jiao交 jie接 jin金 jing京 jiong窘 jiu九 ju居 juan卷 jue决 jun军
ka卡 kai开 kan看 kang康 kao考 ke科 ken肯 keng坑 kong空 kou口 ku苦 kua夸 kuai快 kuan宽 kuang框 kui亏 kun困 kuo阔
la拉 lai来 lan蓝 lang朗 lao老 le乐 lei雷 leng冷 li李 lian联 liang良 liao聊 lie列 lin林 ling灵 liu六 long龙 lou楼 lu路 lv绿 luan乱 lue略 lun轮 luo罗
ma马 mai买 man满 mang忙 mao毛 me么 mei美 men门 meng梦 mi米 mian面 miao苗 mie灭 min民 ming明 miu谬 mo摸 mou某 mu木
na拿 nai乃 nan南 nang囊 nao脑 ne呢 nei内 neng能 ni你 nian年 niang娘 niao鸟 nie捏 nin您 ning宁 niu牛 nong农 nou耨 nu奴 nuan暖 nuo诺 nv女
ou欧
pa怕 pai派 pan判 pang旁 pao跑 pei培 pen盆 peng鹏 pi皮 pian片 piao票 pie撇 pin品 ping平 po坡 pou剖 pu普
qi七 qia恰 qian千 qiang强 qiao桥 qie切 qin秦 qing青 qiong穷 qiu秋 qu区 quan全 que缺 qun群
ran然 rang让 rao绕 re热 ren人 reng扔 ri日 rong荣 rou肉 ru如 ruan软 rui锐 run润 ruo弱
sa洒 sai赛 san三 sang桑 sao扫 se色 sen森 seng僧 sha沙 shai晒 shan山 shang上 shao少 she蛇 shen深 sheng生 shi十 shou收 shu书 shua刷 shuai帅 shuan拴 shuang双 shui水 shun顺 shuo说 si四 song松 sou搜 su苏 suan算 sui岁 sun孙 suo所
ta他 tai太 tan谈 tang唐 tao涛 te特 teng疼 ti体 tian天 tiao条 tie铁 ting听 tong通 tou头 tu图 tuan团 tui推 tun吞 tuo托
wa挖 wai外 wan万 wang王 wei为 wen文 weng翁 wo我 wu五
xi西 xia下 xian先 xiang想 xiao小 xie写 xin新 xing星 xiong兄 xiu修 xu须 xuan选 xue学 xun寻
ya压 yan言 yang阳 yao要 ye也 yi一 yin因 ying英 yo哟 yong用 you有 yu于 yuan元 yue月 yun云
za杂 zai再 zan咱 zang藏 zao早 ze则 zei贼 zen怎 zeng增 zha扎 zhai摘 zhan占 zhang张 zhao找 zhe这 zhen真 zheng正 zhi知 zhong中 zhou周 zhu朱 zhua抓 zhuai拽 zhuan专 zhuang庄 zhui追 zhun准 zhuo桌 zi字 zong总 zou走 zu组 zuan钻 zui最 zun尊 zuo做
"""
# 只要「拼音 -> 常用姓/字」的反查不必精确，这里用「单字首字母」映射即可：
# 实际做法：把每个汉字映射到其拼音首字母，需要完整汉字->拼音表（太大）。
# 折中：只保留常见汉字的码表（GB2312 一级字库常用部分），由 PY_TABLE 展开。
for _line in _PY_TABLE.strip().split('\n'):
    for _tok in _line.split():
        # token 形如 "ba八"：开头连续 ASCII 字母是拼音，其余是汉字
        _i = 0
        while _i < len(_tok) and _tok[_i].isascii() and _tok[_i].isalpha():
            _i += 1
        _py = _tok[:_i]
        for _c in _tok[_i:]:
            PY_INIT.setdefault(_c, _py[0])


def py_initials(name):
    if _HAS_PY:
        try:
            pys = lazy_pinyin(name, style=Style.FIRST_LETTER, errors=lambda x: [x])
            out = []
            for seg in pys:
                seg = seg if isinstance(seg, list) else [seg]
                for s in seg:
                    out.append(s[0].lower() if s and s[0].isascii() and s[0].isalpha() else '#')
            return ''.join(out)
        except Exception:
            pass
    out = []
    for ch in name:
        if PY_INIT.get(ch):
            out.append(PY_INIT[ch])
        elif '\u4e00' <= ch <= '\u9fff':
            out.append('#')  # 生僻字占位，匹配时容忍
        else:
            out.append(ch.lower())
    return ''.join(out)


def main():
    if not os.path.exists(DB):
        print('[gen_names] 未找到 %s，跳过' % DB)
        return 1
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "select code, name from stocks where name is not null and name != ''"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print('[gen_names] 读取失败：%s' % e)
        return 1
    con.close()

    rows = [(str(c).zfill(6), str(n).strip()) for c, n in rows if c and n]
    rows = [r for r in rows if len(r[0]) == 6]
    # 名称归一：去内部空格、全角转半角（万 科Ａ → 万科A），便于前端匹配
    def _norm(nm):
        out = []
        for ch in nm:
            o = ord(ch)
            if ch.isspace() or o == 0x3000:
                continue
            if 0xFF01 <= o <= 0xFF5E:
                ch = chr(o - 0xFEE0)
            out.append(ch)
        return ''.join(out)
    rows = [(c, _norm(n)) for c, n in rows]
    rows.sort()
    pairs = ['%s:%s:%s' % (c, n, py_initials(n)) for c, n in rows]
    body = '|'.join(pairs)
    ts = datetime.date.today().isoformat()
    js = (
        '/* 全市场股票名录（自动生成，勿手改）。源：cache/market.db 表 stocks */\n'
        '/* 条目格式：代码:名称:拼音首字母 */\n'
        'window.SA_NAMES = "%s";\n'
        'window.SA_NAMES_TS = "%s";\n'
        'window.SA_NAMES_N = %d;\n'
    ) % (body, ts, len(rows))
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write(js)
    print('[gen_names] 写出 %s（%d 条，%d 字节）' % (OUT, len(rows), len(js.encode('utf-8'))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
