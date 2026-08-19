import xml.etree.ElementTree as ET
from collections import defaultdict
r=ET.parse("/mnt/user-data/uploads/Quant4all_Posiciones_Historicas_20_282_29_281_29_2.xml").getroot()

nav={}
for e in r.iter('EquitySummaryByReportDateInBase'):
    d=e.get('reportDate'); v=float(e.get('total'))
    if d in nav and abs(nav[d]-v)>1e-6: print("CONFLICTO NAV",d,nav[d],v)
    nav[d]=v
F=defaultdict(float); det=[]
for e in r.iter('CashTransaction'):
    if e.get('type')=='Deposits/Withdrawals':
        a=float(e.get('amount'))*float(e.get('fxRateToBase')); F[e.get('reportDate')]+=a
        det.append((e.get('reportDate'),a,'DEP'))
seen=set()
for e in r.iter('Transfer'):
    tid=e.get('transactionID')
    if tid in seen: continue
    seen.add(tid)
    a=float(e.get('cashTransfer'))*float(e.get('fxRateToBase'))+float(e.get('positionAmountInBase'))
    F[e.get('reportDate')]+=a; det.append((e.get('reportDate'),a,'TRF'))

# chain IBKR daily twr as cross-check
dtwr={}
for st in r.iter('FlexStatement'):
    c=st.find('.//ChangeInNAV')
    if c is not None and c.get('twr'): dtwr[st.get('toDate')]=float(c.get('twr'))

def twr(nav,F,w,d0,d1):
    ds=sorted(d for d in nav if d0<=d<=d1); f=1.0; rows=[]
    for p,c in zip(ds,ds[1:]):
        V0,V1,Fl=nav[p],nav[c],F.get(c,0.0)
        rr=(V1-V0-Fl)/(V0+w*Fl); f*=(1+rr); rows.append((c,V0,V1,Fl,rr))
    return (f-1)*100,rows

print("NAV serie:",min(nav),"->",max(nav),len(nav),"días")
t,rows=twr(nav,F,1.0,'20241231','20251231')
print("TWR EUR 2025 (w=1, reportDate) = %.6f %%"%t)
ch=1.0
for d in sorted(dtwr):
    if '20250101'<=d<='20251231': ch*=(1+dtwr[d]/100)
print("Cadena de TWR diarios de IBKR      = %.6f %%"%((ch-1)*100))
print("Doc de trabajo                     = +2.879392 %")
print("\nFlujos 2025:")
for d,a,k in sorted(det):
    if '20250101'<=d<='20251231': print("   %s %10.2f EUR %s"%(d,a,k))
print("\nTop 5 |r| diarios:")
for c,V0,V1,Fl,rr in sorted(rows,key=lambda x:-abs(x[4]))[:5]:
    print("   %s r=%+.4f%% NAV %.2f->%.2f flujo %.2f"%(c,rr*100,V0,V1,Fl))
