import xml.etree.ElementTree as ET
from collections import defaultdict
P="/mnt/user-data/uploads/"
FILES=[P+f"Quant4all_Posiciones_Historicas_{y}.xml" for y in (2023,2024,2025,2026)]

nav={}; acc={}; X=defaultdict(dict); A=defaultdict(lambda: defaultdict(float))
FL=defaultdict(list)   # date -> [(currency, amount_local)]
seen=set()
for p in FILES:
    r=ET.parse(p).getroot()
    for e in r.iter('EquitySummaryByReportDateInBase'):
        nav[e.get('reportDate')]=float(e.get('total'))
        acc[e.get('reportDate')]=float(e.get('total'))-float(e.get('cash'))-float(e.get('stock'))
    for e in r.iter('ConversionRate'):
        if e.get('toCurrency')=='EUR': X[e.get('fromCurrency')][e.get('reportDate')]=float(e.get('rate'))
    for e in r.iter('OpenPosition'):
        A[e.get('reportDate')][e.get('currency')]+=float(e.get('positionValue'))
    for e in r.iter('FxPosition'):
        if e.get('levelOfDetail')=='SUMMARY':
            A[e.get('reportDate')][e.get('fxCurrency')]+=float(e.get('quantity'))
    # bootstrap del día previo al primer statement: buckets desde priorPeriodValue + startingCash
    st0=list(r.iter('FlexStatement'))[0]
    d_prev=sorted({e.get('reportDate') for e in st0.iter('EquitySummaryByReportDateInBase')})[0]
    if d_prev not in A:
        for e in st0.iter('ChangeInPositionValue'):
            if e.get('currency')!='BASE_SUMMARY':
                A[d_prev][e.get('currency')]+=float(e.get('priorPeriodValue'))
        for e in st0.iter('CashReportCurrency'):
            if e.get('levelOfDetail')=='Currency':
                A[d_prev][e.get('currency')]+=float(e.get('startingCash'))
    for e in r.iter('CashTransaction'):
        if e.get('type')=='Deposits/Withdrawals' and e.get('transactionID') not in seen:
            seen.add(e.get('transactionID')); FL[e.get('reportDate')].append((e.get('currency'),float(e.get('amount'))))
    for e in r.iter('Transfer'):
        if e.get('transactionID') in seen: continue
        seen.add(e.get('transactionID'))
        cur=e.get('currency'); fx=float(e.get('fxRateToBase'))
        amt=float(e.get('cashTransfer'))+ (float(e.get('positionAmountInBase'))/fx if fx else 0)
        FL[e.get('reportDate')].append((cur,amt))
X['EUR']=defaultdict(lambda:1.0)
D=sorted(nav)

def rate(c,d):
    if c=='EUR': return 1.0
    x=X[c]
    if d in x: return x[d]
    prev=[k for k in x if k<=d]
    if prev: return x[max(prev)]
    nxt=[k for k in x if k>d]
    return x[min(nxt)] if nxt else None

# corrección del FX del día bootstrap: escalar los buckets no-EUR para cuadrar el NAV
for d in D:
    if d not in A: continue
    v=sum(a*rate(c,d) for c,a in A[d].items())+acc.get(d,0)
    if abs(v-nav[d])>1e-3:
        fgn=sum(a*rate(c,d) for c,a in A[d].items() if c!='EUR')
        if abs(fgn)>1e-6:
            k=1+(nav[d]-v)/fgn
            for c in A[d]:
                if c!='EUR': A[d][c]*=k
            print("  bootstrap %s: FX no-EUR escalado x%.8f"%(d,k))

# control: reconstrucción de NAV desde buckets
worst=0; wd=None
for d in D:
    if d not in A: continue
    v=sum(a*rate(c,d) for c,a in A[d].items())+acc.get(d,0)
    e=abs(v-nav[d])
    if e>worst: worst,wd=e,d
print("Residuo máx. reconstrucción NAV desde buckets: %.6f EUR (%s, NAV=%.2f)"%(worst,wd,nav[wd]))

def chain(d0,d1,navf,flowf):
    ds=[d for d in D if d0<=d<=d1]; f=1.0
    for p,c in zip(ds,ds[1:]):
        V0,V1,fl=navf(p),navf(c),flowf(c); f*=1+(V1-V0-fl)/(V0+fl)
    return (f-1)*100

PER=[("2023","20221230","20231229"),("2024","20231229","20241231"),
     ("2025","20241231","20251231"),("2026 YTD","20251231","20260813")]
print("\n%-9s %11s %11s %11s %11s %11s %9s"%("Período","TWR EUR","TWR USD","cerrada USD","Estrategia","FX (EUR)","cierre"))
for lab,d0,d1 in PER:
    fe=lambda d: sum(a*rate(c,d) for c,a in FL.get(d,[]))
    Re=chain(d0,d1,lambda d:nav[d],fe)
    xu=lambda d: rate('USD',d)
    Ru=chain(d0,d1,lambda d:nav[d]/xu(d),lambda d: fe(d)/xu(d))
    closed=((1+Re/100)*rate('USD',d0)/rate('USD',d1)-1)*100
    navN=lambda d: sum(a*rate(c,d0) for c,a in A[d].items())+acc.get(d,0)
    flN =lambda d: sum(a*rate(c,d0) for c,a in FL.get(d,[]))
    Rs=chain(d0,d1,navN,flN)
    Rfx=((1+Re/100)/(1+Rs/100)-1)*100
    close=(1+Rs/100)*(1+Rfx/100)-1-Re/100
    print("%-9s %10.4f%% %10.4f%% %10.4f%% %10.4f%% %10.4f%% %9.1e"%(lab,Re,Ru,closed,Rs,Rfx,close))
    # estrategia en USD (debe ser idéntica)
    navNu=lambda d: (sum(a*rate(c,d0) for c,a in A[d].items())+acc.get(d,0))/rate('USD',d0)
    flNu =lambda d: sum(a*rate(c,d0) for c,a in FL.get(d,[]))/rate('USD',d0)
    Rsu=chain(d0,d1,navNu,flNu)
    print("           T4 estrategia USD = %.10f%%  Δ vs EUR = %.2e"%(Rsu,Rsu-Rs))
