import struct,sys,re
def index(p):
    f=open(p,'rb'); h=f.read(28)
    magic=h[:4]; typ,nref,refsz,nfiles,idxsz=struct.unpack_from('<IIIII',h,4)
    hdr=24
    if magic in (b'PFH2',b'PFH3'): hdr=32
    f.seek(hdr+refsz); idx=f.read(idxsz)
    o=0; pos=hdr+refsz+idxsz; out=[]
    for i in range(nfiles):
        sz=struct.unpack_from('<I',idx,o)[0]; o+=4
        if magic in (b'PFH2',b'PFH3'): o+=0
        e=idx.index(b'\0',o); name=idx[o:e].decode('latin1'); o=e+1
        out.append((name,pos,sz)); pos+=sz
    return magic,typ,out
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='ls':
        pat=re.compile(sys.argv[2])
        for p in sys.argv[3:]:
            m,t,ents=index(p)
            hits=[e for e in ents if pat.search(e[0])]
            print('##',p.split('/')[-1],m,t,len(ents),'files,',len(hits),'hits')
            for n,_,s in hits[:60]: print('  ',s,n)
    elif mode=='cat':
        p,name=sys.argv[2],sys.argv[3]
        m,t,ents=index(p)
        for n,pos,s in ents:
            if n==name:
                f=open(p,'rb'); f.seek(pos); sys.stdout.buffer.write(f.read(s))
