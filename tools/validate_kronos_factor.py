# -*- coding: utf-8 -*-
"""Kronos 因子增量验证 v2：多周期标签 + 旧特征 vs 新特征对比 + 二轮子特征单独 IC。
纯离线、无未来函数。结果用于判断「二轮增强」是否真的提升选股成功率。
"""
import sqlite3, math, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import kronos_lite as K

DB = os.path.join(os.path.dirname(__file__), "..", "cache", "market.db")
N_CODES = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
MIN_DATE = "2026-03-01"
OLD_KEYS = {"amp_mean","amp_trend","body_ratio","up_shadow","dn_shadow","cont_up",
            "cont_dn","mom_persist","pv_health","vol_regime","self_sim"}

con = sqlite3.connect(DB); cur = con.cursor()
codes = [r[0] for r in cur.execute("select distinct code from bars order by code")][:N_CODES]
print("抽样股票数:", len(codes))

def pearson(xs, ys):
    n=len(xs); mx=sum(xs)/n; my=sum(ys)/n
    cov=sum((xs[i]-mx)*(ys[i]-my) for i in range(n))
    sx=math.sqrt(sum((x-mx)**2 for x in xs)); sy=math.sqrt(sum((y-my)**2 for y in ys))
    return cov/(sx*sy) if sx*sy>0 else 0.0

def quintile(scores, rets):
    order=sorted(range(len(scores)), key=lambda i:scores[i]); n=len(order); size=max(1,n//5); out=[]
    for q in range(5):
        seg=order[q*size:(q+1)*size] if q<4 else order[q*size:]
        if not seg: out.append((0,0.0,0.0)); continue
        rs=[rets[i] for i in seg]; out.append((len(rs), sum(rs)/len(rs), sum(1 for x in rs if x>0)/len(rs)))
    return out

# 标签：(score_new, score_old, ent, edge, ret1, ret5, ret10, maxdd5, is_trend)
rows_out=[]
for code in codes:
    rws=cur.execute("select date,open,high,low,close,vol,amount from bars where code=? order by date",(code,)).fetchall()
    if len(rws)<45: continue
    dates=[r[0] for r in rws]
    start=0
    while start<len(dates) and dates[start]<MIN_DATE: start+=1
    if start+40>=len(rws): continue
    closes=[r[4] for r in rws]
    for i in range(start+30, len(rws)-11):
        window=[{"d":rws[j][0],"o":rws[j][1],"h":rws[j][2],"l":rws[j][3],"c":rws[j][4],"v":rws[j][5]} for j in range(i-29,i+1)]
        feats=K.kronos_features(window)
        if not feats: continue
        s_new=K.kronos_score(feats)
        old_feats={k:feats[k] for k in feats if k in OLD_KEYS}
        s_old=K.kronos_score(old_feats)
        ent=feats.get("pattern_entropy",1.0)
        me=feats.get("micro_edge"); edge=me[0] if (isinstance(me,tuple) and me[0] is not None) else 0.0
        ci=rws[i][4]
        if not ci: continue
        ret1=rws[i+1][4]/ci-1 if rws[i+1][4] else None
        ret5=rws[i+5][4]/ci-1 if rws[i+5][4] else None
        ret10=rws[i+10][4]/ci-1 if rws[i+10][4] else None
        # 未来5日最大回撤（相对 i 收盘）
        fut=[rws[j][4] for j in range(i+1,i+6) if rws[j][4]]
        maxdd=min((x/ci-1) for x in fut) if fut else None
        c20=[rws[j][4] for j in range(i-19,i+1)]
        is_trend = (c20[-1]/c20[0]-1>0.10 and sum(c20[-5:])/5>sum(c20[-10:])/10>sum(c20)/20) if c20[0]>0 else False
        rows_out.append((s_new,s_old,ent,edge,ret1,ret5,ret10,maxdd, is_trend))

def report(subset, name):
    sub=[r for r in rows_out if subset(r)]
    if len(sub)<500:
        print("  [%s] 样本不足 %d"%(name,len(sub))); return
    s_new=[r[0] for r in sub]; s_old=[r[1] for r in sub]
    ent=[r[2] for r in sub]; edge=[r[3] for r in sub]
    r1=[r[4] for r in sub if r[4] is not None]; s1n=[r[0] for r in sub if r[4] is not None]
    r5=[r[5] for r in sub if r[5] is not None]; s5n=[r[0] for r in sub if r[5] is not None]
    s5o=[r[1] for r in sub if r[5] is not None]
    r10=[r[6] for r in sub if r[6] is not None]; s10n=[r[0] for r in sub if r[6] is not None]
    dd=[r[7] for r in sub if r[7] is not None]; sd5=[r[0] for r in sub if r[7] is not None]
    print("\n===== %s（样本 %d）====="%(name,len(sub)))
    print("IC  score_new : ret1=%.4f ret5=%.4f ret10=%.4f maxdd5=%.4f"%
          (pearson(s1n,r1), pearson(s5n,r5), pearson(s10n,r10), pearson(sd5,dd)))
    print("IC  score_old : ret5=%.4f ret10=%.4f"% (pearson(s5o,r5), pearson([r[1] for r in sub if r[5] is not None], r10)))
    print("IC  pattern_entropy : ret1=%.4f ret5=%.4f ret10=%.4f maxdd5=%.4f"%
          (pearson(ent,r1), pearson(ent,r5), pearson(ent,r10), pearson(ent,dd)))
    print("IC  micro_edge     : ret1=%.4f ret5=%.4f ret10=%.4f maxdd5=%.4f"%
          (pearson(edge,r1), pearson(edge,r5), pearson(edge,r10), pearson(edge,dd)))
    qn=quintile(s5n,r5); print("  ret5 五分位(新score): Q1=%+.3f%% Q3=%+.3f%% Q5=%+.3f%% | 多空Q5-Q1=%+.3f%%"%(
        qn[0][1]*100, qn[2][1]*100, qn[4][1]*100, (qn[4][1]-qn[0][1])*100))
    qo=quintile(s5o,r5); print("  ret5 五分位(旧score): Q1=%+.3f%% Q3=%+.3f%% Q5=%+.3f%% | 多空Q5-Q1=%+.3f%%"%(
        qo[0][1]*100, qo[2][1]*100, qo[4][1]*100, (qo[4][1]-qo[0][1])*100))
    qd=quintile(sd5,dd); print("  maxdd5 五分位(新score,越小越好): Q1=%+.3f%% Q5=%+.3f%% | Q5-Q1=%+.3f%% (负=高分票回撤更小=更结构化)"%(
        qd[0][1]*100, qd[4][1]*100, (qd[4][1]-qd[0][1])*100))

print("总样本:", len(rows_out))
report(lambda r: True, "全市场")
report(lambda r: r[8], "趋势股子集(近20日涨>10%且均线多头)")
