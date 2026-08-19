"""Test offline del módulo con una serie sintética + las fechas reales del motor."""
import numpy as np, pandas as pd, os, xml.etree.ElementTree as ET
os.environ["Q4_CACHE"]="./cache_test"
import q4_benchmark as B

# fechas reales del motor
P="/mnt/user-data/uploads/"
dates=set()
for y in (2023,2024,2025,2026):
    r=ET.parse(P+f"Quant4all_Posiciones_Historicas_{y}.xml").getroot()
    dates|={e.get('reportDate') for e in r.iter('EquitySummaryByReportDateInBase')}
D=sorted(dates)

# benchmark sintético con calendario US (quitamos ~8 días que el motor sí tiene)
rng=np.random.default_rng(7)
bdates=[d for i,d in enumerate(D) if i%31!=17]
px=pd.Series(100*np.cumprod(1+rng.normal(0.0005,0.011,len(bdates))),index=bdates)
os.makedirs("./cache_test",exist_ok=True)
px.to_frame("Close").to_csv("./cache_test/FAKE.csv")

rx=B.reindex_to_engine(px,D)
print("días arrastrados: %d (%.2f%%) vol fiable=%s"%(rx.n_forward_filled,rx.pct_forward_filled,rx.reliable_vol))
print("retornos exactamente 0:",int((rx.returns==0).sum()))

rp=pd.Series(rng.normal(0.0009,0.0153,len(D)-1),index=D[1:])
rt=pd.Series(rng.normal(0.0008,0.0155,len(D)-1),index=D[1:])
m=B.relative_metrics(rp,rx.returns,rf=0.03)
for k,v in m.items(): print("  %-20s %s"%(k,round(v,4) if isinstance(v,float) else v))

fx={d:0.90 for d in D}
df=B.compare(D,rt,rp,fx_to_analysis=fx,tickers=["FAKE"],rf=0.03,offline=True)
print()
print(df.to_string(index=False))
