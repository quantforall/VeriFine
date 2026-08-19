"""Test del orquestador: backfill reanudable, incremental y canario."""
import os, shutil, logging, q4_sync as S, q4_ingest as Q
logging.basicConfig(level=logging.INFO, format="  %(message)s")
shutil.rmtree("./raw_sync", ignore_errors=True); os.makedirs("./raw_sync")
SP="./raw_sync/state.json"

class Sess:
    def __init__(s,fail_on=None): s.n=0; s.fail_on=fail_on
    def get(s,url,params=None,timeout=None):
        class R:
            def __init__(s2,t): s2.text=t; s2.url="u"
            def raise_for_status(s2): pass
        if "SendRequest" in url:
            s.n+=1
            if s.fail_on and s.n==s.fail_on:
                return R("<FlexStatementResponse><Status>Fail</Status><ErrorCode>1020</ErrorCode>"
                         "<ErrorMessage>boom</ErrorMessage></FlexStatementResponse>")
            return R("<FlexStatementResponse><Status>Success</Status><ReferenceCode>1</ReferenceCode>"
                     "<Url>https://x/GetStatement</Url></FlexStatementResponse>")
        return R("<FlexQueryResponse/>")

def client(fail_on=None):
    c=Q.FlexClient(token="T",query_id="123456",raw_dir="./raw_sync",session=Sess(fail_on))
    c._pacer.wait=lambda: None; return c

print("1) Backfill que falla en la 4ª ventana")
st=S.SyncState.load(SP,"123456")
try: S.backfill(client(fail_on=4),st,SP,"20210106","20260813")
except Q.FlexError as e: print("   falló:",e)
print("   ventanas completadas y persistidas:",len(st.windows_done))

print("\n2) Reanudación: sólo pide las que faltan")
st=S.SyncState.load(SP,"123456")
S.backfill(client(),st,SP,"20210106","20260813")
print("   total ventanas:",len(st.windows_done))

print("\n3) Watermark desde los datos, no desde la petición")
S.update_watermark(st,SP,["20260810","20260811","20260812"])
print("   watermark:",st.watermark,"(se pidió hasta 20260813)")

print("\n4) Incremental: solapa 10 días hacia atrás")
r=S.incremental(client(),st,SP,today="20260814")
print("   rango pedido:",r["requested"],"->",r["status"])

print("\n5) Canario de golden values")
gold={"2023":7.497979,"2024":29.524351,"2025":2.879392}
print("   primera vez:",S.check_golden(st,SP,gold) or "sin desviación (línea base fijada)")
print("   sin cambios:",S.check_golden(st,SP,gold) or "sin desviación")
gold2=dict(gold,**{"2024":29.554351})
print("   con deriva: ",S.check_golden(st,SP,gold2))

print("\n6) Conflictos de NAV entre bloques solapados")
print("   iguales:",S.check_nav_conflicts({"20260810":100.0},{"20260810":100.0}) or "ninguno")
print("   distintos:",S.check_nav_conflicts({"20260810":100.0},{"20260810":100.5}))

print("\n7) Token caducado -> pausa, no reintento")
class Exp(Sess):
    def get(s,url,params=None,timeout=None):
        class R:
            text=("<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
                  "<ErrorMessage>Token has expired</ErrorMessage></FlexStatementResponse>")
            url="u"
            def raise_for_status(s2): pass
        return R()
c=Q.FlexClient(token="T",query_id="1",raw_dir="./raw_sync",session=Exp()); c._pacer.wait=lambda: None
print("  ",S.incremental(c,st,SP,today="20260815"))
print("   siguiente ejecución:",S.incremental(client(),S.SyncState.load(SP,"123456"),SP)["status"])
