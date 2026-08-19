import xml.etree.ElementTree as ET
from collections import defaultdict
P="/mnt/user-data/uploads/"
roots=[ET.parse(P+"Quant4all_Posiciones_Historicas_20_282_29_281_29_2.xml").getroot(),
       ET.parse(P+"Quant4all_Audit_2024-2025_2.xml").getroot()]
r=roots[0]
X={}
for rr in roots:
    for e in rr.iter('ConversionRate'):
        if e.get('fromCurrency')=='USD' and e.get('toCurrency')=='EUR':
            X[e.get('reportDate')]=float(e.get('rate'))
nav={e.get('reportDate'):float(e.get('total')) for e in r.iter('EquitySummaryByReportDateInBase')}
Feur=defaultdict(float); Fusd=defaultdict(float)
for e in r.iter('CashTransaction'):
    if e.get('type')=='Deposits/Withdrawals':
        d=e.get('reportDate'); a=float(e.get('amount'))*float(e.get('fxRateToBase'))
        Feur[d]+=a; Fusd[d]+=a/X[d]
seen=set()
for e in r.iter('Transfer'):
    if e.get('transactionID') in seen: continue
    seen.add(e.get('transactionID')); d=e.get('reportDate')
    a=float(e.get('cashTransfer'))*float(e.get('fxRateToBase'))+float(e.get('positionAmountInBase'))
    Feur[d]+=a; Fusd[d]+=a/X[d]
ds=sorted(d for d in nav if '20241231'<=d<='20251231')
print("Fechas sin FX:",[d for d in ds if d not in X])
navU={d:nav[d]/X[d] for d in ds}
def chain(N,F):
    f=1.0
    for p,c in zip(ds,ds[1:]): f*=1+(N[c]-N[p]-F.get(c,0.0))/(N[p]+F.get(c,0.0))
    return (f-1)*100
Re=chain(nav,Feur); Ru=chain(navU,Fusd)
closed=((1+Re/100)*X[ds[0]]/X[ds[-1]]-1)*100
print("EURUSD %s = %.6f | %s = %.6f"%(ds[0],1/X[ds[0]],ds[-1],1/X[ds[-1]]))
print("TWR EUR 2025 (cadena)        = %.6f %%"%Re)
print("TWR USD 2025 (cadena diaria) = %.6f %%   <- motor"%Ru)
print("TWR USD 2025 (forma cerrada) = %.6f %%   <- sólo exacta sin flujos"%closed)
print("Motor - forma cerrada        = %+.4f pb"%((Ru-closed)*100))
print("Doc de trabajo               = +16.711200 %")
