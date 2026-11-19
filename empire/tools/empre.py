import struct,sys,re
from capstone import Cs,CS_ARCH_X86,CS_MODE_32
P=r"D:/SteamLibrary/steamapps/common/Empire Total War/Empire.exe"
d=open(P,'rb').read()
pe=struct.unpack_from('<I',d,0x3c)[0]; nsec=struct.unpack_from('<H',d,pe+6)[0]
opt=pe+24; IB=struct.unpack_from('<I',d,opt+28)[0]; optsz=struct.unpack_from('<H',d,pe+20)[0]
secs=[]
for i in range(nsec):
    n,vs,va,rs,ra=struct.unpack_from('<8sIIII',d,opt+optsz+40*i); secs.append((n.rstrip(b'\0').decode(),vs,va,rs,ra))
def o2va(o):
    for n,vs,va,rs,ra in secs:
        if ra<=o<ra+rs: return IB+va+o-ra
def va2o(v):
    r=v-IB
    for n,vs,va,rs,ra in secs:
        if va<=r<va+rs: return r-va+ra
text=[s for s in secs if s[0]=='.text'][0]
T0=text[4]; T1=T0+text[3]
md=Cs(CS_ARCH_X86,CS_MODE_32); md.detail=False
def xrefs(va):
    pat=struct.pack('<I',va); out=[]
    for m in re.finditer(re.escape(pat),d[T0:T1]): out.append(o2va(T0+m.start()))
    return out
def dis(va,before=0x40,after=0x80):
    o=va2o(va-before); code=d[o:o+before+after]
    for ins in md.disasm(code,va-before):
        mark='>>' if ins.address<=va<ins.address+ins.size else '  '
        extra=''
        for v in re.findall(r'0x[0-9a-f]{6,8}',ins.op_str):
            vv=int(v,16); oo=va2o(vv) if IB<=vv<IB+0x1400000 else None
            if oo:
                s=re.match(rb'[\x20-\x7e]{3,}',d[oo:oo+80])
                if s: extra='  ; "'+s.group().decode()+'"'
                else:
                    s=d[oo:oo+80]
                    if s[1:2]==b'\0':
                        w=s.decode('utf-16le','ignore').split('\0')[0]
                        if len(w)>2 and w.isprintable(): extra='  ; L"'+w+'"'
        print(mark,hex(ins.address),ins.mnemonic,ins.op_str,extra)
def strva(s,utf16=False):
    b=s.encode('utf-16le') if utf16 else s.encode()
    return [o2va(m.start()) for m in re.finditer(re.escape(b+(b'\0\0' if utf16 else b'\0')),d) if (m.start()==0 or d[m.start()-1:m.start()]==b'\0')]
if __name__=='__main__':
    cmd=sys.argv[1]
    if cmd=='x':
        for s in sys.argv[2:]:
            for u in (False,True):
                for v in strva(s,u):
                    print('STR',('L' if u else ''),repr(s),hex(v),'xrefs:',[hex(x) for x in xrefs(v)])
    elif cmd=='d':
        va=int(sys.argv[2],16); b=int(sys.argv[3],16) if len(sys.argv)>3 else 0x40; a=int(sys.argv[4],16) if len(sys.argv)>4 else 0x80
        dis(va,b,a)
