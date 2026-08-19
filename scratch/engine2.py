import xml.etree.ElementTree as ET
from collections import defaultdict

def load(path):
    r=ET.parse(path).getroot()
    nav={e.get('reportDate'):float(e.get('total')) for e in r.iter('EquitySummaryByReportDateInBase')}
    F={'rep':defaultdict(float),'set':defaultdict(float)}
    detail=[]
    for e in r.iter('CashTransaction'):
        if e.get('type')=='Deposits/Withdrawals':
            a=float(e.get('amount'))*float(e.get('fxRateToBase'))
            F['rep'][e.get('reportDate')]+=a; F['set'][e.get('settleDate')]+=a
            detail.append((e.get('reportDate'),e.get('settleDate'),a,'DEP'))
    for e in r.iter('Transfer'):
        a=float(e.get('cashTransfer'))*float(e.get('fxRateToBase'))+float(e.get('positionAmountInBase'))
        F['rep'][e.get('reportDate')]+=a; F['set'][e.get('settleDate')]+=a
        detail.append((e.get('reportDate'),e.get('settleDate'),a,'TRF-'+e.get('assetCategory')))
    return nav,F,detail,r.find('.//ChangeInNAV')

def twr(nav,flows,w,d0=None,d1=None):
    ds=sorted(d for d in nav if (not d0 or d>=d0) and (not d1 or d<=d1))
    f=1.0; rows=[]
    for p,c in zip(ds,ds[1:]):
        V0,V1,Fl=nav[p],nav[c],flows.get(c,0.0)
        r=(V1-V0-Fl)/(V0+w*Fl); f*=(1+r); rows.append((c,V0,V1,Fl,r))
    return (f-1)*100, rows

for path,label in [("/mnt/user-data/uploads/Quant4all_Audit_2023-2024_1_.xml","AUDIT 2023-01-05 → 2024-01-05"),
                   ("/mnt/user-data/uploads/Quant4all_Audit_2024-2025_2.xml","AUDIT 2024-08-13 → 2025-08-13")]:
    nav,F,detail,cin=load(path)
    ibkr=float(cin.get('twr'))
    tot=sum(F['rep'].values())
    dec=float(cin.get('depositsWithdrawals'))+float(cin.get('internalCashTransfers'))+float(cin.get('assetTransfers'))
    print("="*72); print(label)
    print("  Flujo total calculado %.6f | declarado por IBKR %.6f | Δ=%.2e"%(tot,dec,tot-dec))
    for key in ('rep','set'):
        for w in (1.0,0.5,0.0):
            t,_=twr(nav,F[key],w)
            print("   w=%.1f %-4s TWR=%.9f%%  IBKR=%.9f%%  Δ=%+9.5f pb"%(w,key,t,ibkr,(t-ibkr)*100))

print("\n"+"="*72)
nav,F,det,cin=load("/mnt/user-data/uploads/Quant4all_Audit_2023-2024_1_.xml")
for d0,d1,lab in [('20230105','20231229','2023 (desde 05-ene, faltan 2-4 ene)'),
                  ('20230105','20240105','período completo del audit')]:
    t,_=twr(nav,F['rep'],1.0,d0,d1)
    print("  %-40s TWR EUR = %.6f %%"%(lab,t))
print("  Doc de trabajo 2023                      = +7.498000 %")
