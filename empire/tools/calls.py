import sys,struct,re
sys.argv=[sys.argv[0]]+sys.argv[1:]
from empre import d,T0,T1,o2va,va2o,IB
def callers(tgt):
    out=[]
    t=d[T0:T1]
    for m in re.finditer(b'[\xe8\xe9]',t):
        o=m.start()
        if o+5>len(t): break
        rel=struct.unpack_from('<i',t,o+1)[0]
        src=o2va(T0+o)
        if src+5+rel==tgt: out.append((hex(src),'call' if t[o]==0xe8 else 'jmp'))
    # absolute refs (vtables / function pointer tables)
    for m in re.finditer(re.escape(struct.pack('<I',tgt)),d):
        out.append((hex(o2va(m.start())),'ptr'))
    return out
for a in sys.argv[1:]:
    print(a, callers(int(a,16)))
