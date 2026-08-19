"""Test del cliente de ingesta con transporte simulado (sin red)."""
import logging, types, q4_ingest as Q
logging.basicConfig(level=logging.INFO, format="%(message)s")

# 1) planificación de ventanas
w=Q.plan_windows("20210106","20260813")
print("Ventanas para 2021-01-06 → 2026-08-13:",len(w))
for a,b in w: print("   ",a,"→",b)
assert all((int(b[:4])*10000+int(b[4:]))>=(int(a[:4])*10000+int(a[4:])) for a,b in w)
print("   Primer bloque adelantado 5 días:",w[0][0])

# 2) validación de la query sobre un XML real
xml=open("/mnt/user-data/uploads/Quant4all_Posiciones_Historicas_2025.xml").read()
v=Q.validate_query(xml)
print("\nvalidate_query sobre el fichero real de 2025:")
for k,val in v.items(): print("   %-18s %s"%(k,val))

# 3) protocolo con transporte falso
class FakeResp:
    def __init__(s,text): s.text=text; s.url="https://x?t=SECRET&q=1"
    def raise_for_status(s): pass
class FakeSession:
    def __init__(s): s.calls=0
    def get(s,url,params=None,timeout=None):
        s.calls+=1
        if "SendRequest" in url:
            return FakeResp("<FlexStatementResponse><Status>Success</Status>"
                            "<ReferenceCode>999</ReferenceCode><Url>https://x/GetStatement</Url>"
                            "</FlexStatementResponse>")
        if s.calls<=2:
            return FakeResp("<FlexStatementResponse><Status>Warn</Status>"
                            "<ErrorCode>1019</ErrorCode><ErrorMessage>generating</ErrorMessage>"
                            "</FlexStatementResponse>")
        return FakeResp("<FlexQueryResponse><FlexStatements count='1'/></FlexQueryResponse>")

c=Q.FlexClient(token="SECRET",query_id="123456",raw_dir="./raw_test",
               poll_wait=0.05,session=FakeSession())
c._pacer.wait=lambda: None
p=c.fetch("20250101","20251231")
print("\nDescarga simulada con dos 1019 antes del éxito -> guardado en:")
print("   ",p)
print("   URL redactada:",Q._redact("https://x/SendRequest?t=SECRET&q=1&v=3"))

# 4) errores fatales
class ErrSession(FakeSession):
    def get(s,url,params=None,timeout=None):
        return FakeResp("<FlexStatementResponse><Status>Fail</Status>"
                        "<ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired</ErrorMessage>"
                        "</FlexStatementResponse>")
c2=Q.FlexClient(token="X",query_id="1",session=ErrSession()); c2._pacer.wait=lambda: None
try: c2.fetch()
except Q.TokenExpired as e: print("\nToken caducado detectado:",e)

# 5) rango demasiado largo
try: c.request_statement("20240101","20260101")
except ValueError as e: print("Rango excesivo rechazado:",e)
