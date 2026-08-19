import xml.etree.ElementTree as ET, math, statistics as st
from collections import defaultdict
P="/mnt/user-data/uploads/"
nav={};acc={};X=defaultdict(dict);A=defaultdict(lambda: defaultdict(float));FL=defaultdict(list);seen=set()
for y in (2023,2024,2025,2026):
    r=ET.parse(P+f"Quant4all_Posiciones_Historicas_{y}.xml").getroot()
    for e in r.iter('EquitySummaryByReportDateInBase'):
        nav[e.get('reportDate')]=float(e.get('total'))
        acc[e.get('reportDate')]=float(e.get('total'))-float(e.get('cash'))-float(e.get('stock'))
    for e in r.iter('ConversionRate'):
        if e.get('toCurrency')=='EUR': X[e.get('fromCurrency')][e.get('reportDate')]=float(e.get('rate'))
    for e in r.iter('OpenPosition'): A[e.get('reportDate')][e.get('currency')]+=float(e.get('positionValue'))
    for e in r.iter('FxPosition'):
        if e.get('levelOfDetail')=='SUMMARY': A[e.get('reportDate')][e.get('fxCurrency')]+=float(e.get('quantity'))
    s0=list(r.iter('FlexStatement'))[0]
    dp=sorted({e.get('reportDate') for e in s0.iter('EquitySummaryByReportDateInBase')})[0]
    if dp not in A:
        for e in s0.iter('ChangeInPositionValue'):
            if e.get('currency')!='BASE_SUMMARY': A[dp][e.get('currency')]+=float(e.get('priorPeriodValue'))
        for e in s0.iter('CashReportCurrency'):
            if e.get('levelOfDetail')=='Currency': A[dp][e.get('currency')]+=float(e.get('startingCash'))
    for e in r.iter('CashTransaction'):
        if e.get('type')=='Deposits/Withdrawals' and e.get('transactionID') not in seen:
            seen.add(e.get('transactionID')); FL[e.get('reportDate')].append((e.get('currency'),float(e.get('amount'))))
    for e in r.iter('Transfer'):
        if e.get('transactionID') in seen: continue
        seen.add(e.get('transactionID')); fx=float(e.get('fxRateToBase'))
        FL[e.get('reportDate')].append((e.get('currency'),float(e.get('cashTransfer'))+float(e.get('positionAmountInBase'))/fx))
X['EUR']=defaultdict(lambda:1.0); D=sorted(nav)
def rate(c,d):
    if c=='EUR': return 1.0
    x=X[c]
    p=[k for k in x if k<=d]
    if p: return x[max(p)]
    n=[k for k in x if k>d]; return x[min(n)]
for d in D:
    if d not in A: continue
    v=sum(a*rate(c,d) for c,a in A[d].items())+acc.get(d,0)
    if abs(v-nav[d])>1e-3:
        f=sum(a*rate(c,d) for c,a in A[d].items() if c!='EUR'); k=1+(nav[d]-v)/f
        for c in A[d]:
            if c!='EUR': A[d][c]*=k

d0G=D[0]
def series(navf,flowf,ds):
    out=[]
    for p,c in zip(ds,ds[1:]):
        V0,V1,fl=navf(p),navf(c),flowf(c); out.append((c,(V1-V0-fl)/(V0+fl)))
    return out
fe=lambda d: sum(a*rate(c,d) for c,a in FL.get(d,[]))
sEUR=series(lambda d:nav[d],fe,D)
sUSD=series(lambda d:nav[d]/rate('USD',d),lambda d: fe(d)/rate('USD',d),D)
def neutral(d0,ds):
    return series(lambda d: sum(a*rate(c,d0) for c,a in A[d].items())+acc.get(d,0),
                  lambda d: sum(a*rate(c,d0) for c,a in FL.get(d,[])), ds)
sSTR=neutral(d0G,D)

def metrics(s,rf=0.0):
    r=[x[1] for x in s]; n=len(r); ann=252
    cum=1.0; peak=1.0; mdd=0.0; idx=[1.0]; dd_end=None
    for x in r:
        cum*=1+x; idx.append(cum); peak=max(peak,cum)
        dd=cum/peak-1
        if dd<mdd: mdd=dd
    tot=cum-1; yrs=n/ann
    cagr=(1+tot)**(1/yrs)-1
    vol=st.stdev(r)*math.sqrt(ann)
    neg=[x for x in r if x<0]
    dvol=math.sqrt(sum(x*x for x in neg)/n)*math.sqrt(ann)
    sh=(cagr-rf)/vol; so=(cagr-rf)/dvol if dvol else float('nan')
    sr=sorted(r); var95=sr[int(0.05*n)]; cvar=sum(sr[:int(0.05*n)])/int(0.05*n)
    return dict(n=n,tot=tot*100,cagr=cagr*100,vol=vol*100,dvol=dvol*100,sharpe=sh,sortino=so,
                mdd=mdd*100,calmar=cagr/abs(mdd),pos=100*sum(1 for x in r if x>0)/n,
                best=max(r)*100,worst=min(r)*100,var95=var95*100,cvar95=cvar*100)

print("MÉTRICAS SOBRE TODO EL HISTÓRICO (30-12-2022 → 13-08-2026, %d días)\n"%len(sEUR))
rows=[("Total EUR",sEUR),("Total USD",sUSD),("Estrategia (FX-neutral)",sSTR)]
h=["Serie","Acum.","CAGR","Vol","VolDown","Sharpe","Sortino","MaxDD","Calmar","%días+","VaR95","CVaR95"]
print("%-24s"%h[0]+"".join("%9s"%x for x in h[1:]))
for lab,s in rows:
    m=metrics(s)
    print("%-24s%8.2f%%%8.2f%%%8.2f%%%8.2f%%%9.2f%9.2f%8.2f%%%9.2f%8.1f%%%8.2f%%%8.2f%%"%(
        lab,m['tot'],m['cagr'],m['vol'],m['dvol'],m['sharpe'],m['sortino'],m['mdd'],m['calmar'],m['pos'],m['var95'],m['cvar95']))

print("\nPOR AÑO — serie Estrategia (FX-neutral, congelado a 1-ene de cada año)")
PER=[("2023","20221230","20231229"),("2024","20231229","20241231"),("2025","20241231","20251231"),("2026 YTD","20251231","20260813")]
print("%-9s%9s%9s%9s%9s%9s%9s%9s"%("Año","Estrat.","Vol","Sharpe","Sortino","MaxDD","Calmar","%días+"))
for lab,a,b in PER:
    ds=[d for d in D if a<=d<=b]
    m=metrics(neutral(a,ds))
    print("%-9s%8.2f%%%8.2f%%%9.2f%9.2f%8.2f%%%9.2f%8.1f%%"%(lab,m['tot'],m['vol'],m['sharpe'],m['sortino'],m['mdd'],m['calmar'],m['pos']))

print("\nDESCOMPOSICIÓN DEL RIESGO (log-retornos diarios, histórico completo)")
import math
lt=[math.log(1+x[1]) for x in sEUR]; ls=[math.log(1+x[1]) for x in sSTR]
lf=[a-b for a,b in zip(lt,ls)]
vt,vs,vf=st.variance(lt),st.variance(ls),st.variance(lf)
cov=st.covariance(ls,lf)
ann=252
print("  Vol total EUR      %.2f %%"%(math.sqrt(vt*ann)*100))
print("  Vol estrategia     %.2f %%"%(math.sqrt(vs*ann)*100))
print("  Vol divisa         %.2f %%"%(math.sqrt(vf*ann)*100))
print("  Correlación estrategia/divisa  %.3f"%(cov/math.sqrt(vs*vf)))
print("  Descomposición varianza: estrategia %.1f%% + divisa %.1f%% + cruzado %.1f%%"%(
    100*vs/vt,100*vf/vt,100*2*cov/vt))
