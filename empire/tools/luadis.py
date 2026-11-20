"""Kleiner Lua-5.1-Bytecode-Leser (Empire: little endian, int/size_t/Instruktion 4 Byte, lua_Number = float 4 Byte).
Rekonstruiert Aufrufe als Ausdruecke (Register-Verfolgung je Funktion, geradlinig) und gibt alle Aufrufe von
Funktionen eines Moduls (z. B. CampaignUI.X(...)) mit ihren Argumenten aus."""
import struct
import sys
from pathlib import Path

OPS = ["MOVE", "LOADK", "LOADBOOL", "LOADNIL", "GETUPVAL", "GETGLOBAL", "GETTABLE", "SETGLOBAL", "SETUPVAL",
       "SETTABLE", "NEWTABLE", "SELF", "ADD", "SUB", "MUL", "DIV", "MOD", "POW", "UNM", "NOT", "LEN", "CONCAT", "JMP",
       "EQ", "LT", "LE", "TEST", "TESTSET", "CALL", "TAILCALL", "RETURN", "FORLOOP", "FORPREP", "TFORLOOP", "SETLIST",
       "CLOSE", "CLOSURE", "VARARG"]


class Reader:
    def __init__(self, data: bytes):
        self.d, self.p = data, 0
        hdr = data[:12]
        if hdr[:5] != b"\x1bLuaQ":
            raise ValueError("kein Lua-5.1-Bytecode")
        self.nsize = hdr[10]
        self.p = 12

    def byte(self):
        v = self.d[self.p]; self.p += 1; return v

    def int(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v

    def string(self):
        n = struct.unpack_from("<I", self.d, self.p)[0]; self.p += 4
        if n == 0:
            return None
        s = self.d[self.p:self.p + n - 1].decode("latin-1"); self.p += n; return s

    def number(self):
        fmt = "<f" if self.nsize == 4 else "<d"
        v = struct.unpack_from(fmt, self.d, self.p)[0]; self.p += self.nsize; return v

    def function(self, parent="?"):
        f = {"source": self.string() or parent}
        f["line"], f["lastline"] = self.int(), self.int()
        f["nups"], f["nparams"], f["vararg"], f["maxstack"] = self.byte(), self.byte(), self.byte(), self.byte()
        f["code"] = [self.int() & 0xFFFFFFFF for _ in range(self.int())]
        consts = []
        for _ in range(self.int()):
            t = self.byte()
            consts.append(None if t == 0 else bool(self.byte()) if t == 1 else self.number() if t == 3
                          else self.string() if t == 4 else f"?{t}")
        f["consts"] = consts
        f["protos"] = [self.function(f["source"]) for _ in range(self.int())]
        f["lines"] = [self.int() for _ in range(self.int())]
        f["locals"] = []
        for _ in range(self.int()):
            f["locals"].append((self.string(), self.int(), self.int()))
        f["upvals"] = [self.string() for _ in range(self.int())]
        return f


def k(v):
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def local_name(f, reg, pc):
    """Name des Registers reg an Stelle pc (Debug-Info, falls vorhanden)."""
    active = [n for n, s, e in f["locals"] if s <= pc < e]
    return active[reg] if reg < len(active) else None


def calls(f, module: str, out: list, path: str = "main"):
    R: dict[int, str] = {}
    code, consts = f["code"], f["consts"]

    def reg(r, pc):
        if r in R:
            return R[r]
        n = local_name(f, r, pc)
        return n or (f"arg{r + 1}" if r < f["nparams"] else f"r{r}")

    def rk(x, pc):
        return k(consts[x & 0xFF]) if x & 0x100 else reg(x, pc)

    for pc, ins in enumerate(code):
        op = OPS[ins & 0x3F] if (ins & 0x3F) < len(OPS) else "?"
        a, c, b, bx = (ins >> 6) & 0xFF, (ins >> 14) & 0x1FF, (ins >> 23) & 0x1FF, ins >> 14
        if op == "MOVE":
            R[a] = reg(b, pc)
        elif op == "LOADK":
            R[a] = k(consts[bx])
        elif op == "LOADBOOL":
            R[a] = "true" if b else "false"
        elif op == "LOADNIL":
            for r in range(a, b + 1):
                R[r] = "nil"
        elif op == "GETUPVAL":
            R[a] = f["upvals"][b] if b < len(f["upvals"]) and f["upvals"][b] else f"upval{b}"
        elif op == "GETGLOBAL":
            R[a] = str(consts[bx])
        elif op == "GETTABLE":
            key = consts[c & 0xFF] if c & 0x100 else None
            base = reg(b, pc)
            R[a] = f"{base}.{key}" if isinstance(key, str) else f"{base}[{rk(c, pc)}]"
        elif op == "SELF":
            obj = reg(b, pc)
            key = consts[c & 0xFF] if c & 0x100 else rk(c, pc)
            R[a + 1] = obj
            R[a] = f"{obj}:{key}"
        elif op == "NEWTABLE":
            R[a] = "{}"
        elif op in ("ADD", "SUB", "MUL", "DIV", "MOD", "POW"):
            sym = {"ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/", "MOD": "%", "POW": "^"}[op]
            R[a] = f"({rk(b, pc)} {sym} {rk(c, pc)})"
        elif op in ("UNM", "NOT", "LEN"):
            R[a] = {"UNM": "-", "NOT": "not ", "LEN": "#"}[op] + reg(b, pc)
        elif op == "CONCAT":
            R[a] = " .. ".join(reg(r, pc) for r in range(b, c + 1))
        elif op in ("CALL", "TAILCALL"):
            fn = reg(a, pc)
            method = ":" in fn
            nargs = (b - 1) if b else None
            args = [reg(a + 1 + i, pc) for i in range(nargs)] if nargs is not None else ["..."]
            if method and args:
                args = args[1:]          # self
            expr = f"{fn}({', '.join(args)})"
            if fn.startswith(module + ".") or fn.startswith(module + ":"):
                line = f["lines"][pc] if pc < len(f["lines"]) else 0
                out.append((fn.split(".", 1)[-1].split(":", 1)[-1], expr, c - 1 if c else -1, path, line))
            R[a] = expr
            for r in range(a + 1, a + 1 + max(c - 1, 0)):
                R[r] = f"{expr}[{r - a + 1}]"
            for r in list(R):
                if r > a and (c == 0 or r >= a + max(c - 1, 1)):
                    R.pop(r, None)
        elif op == "CLOSURE":
            R[a] = f"function#{bx}"
        elif op == "VARARG":
            R[a] = "..."
        elif op in ("JMP", "FORLOOP", "FORPREP", "TFORLOOP"):
            pass                         # geradlinige Naeherung: Register behalten
        elif op == "SETGLOBAL" or op == "SETTABLE" or op == "SETUPVAL" or op == "SETLIST":
            pass
    for i, p in enumerate(f["protos"]):
        calls(p, module, out, f"{path}/{i}@{p['line']}")


def module_calls(data: bytes, module: str = "CampaignUI") -> list:
    f = Reader(data).function()
    out: list = []
    calls(f, module, out)
    return out


if __name__ == "__main__":
    for p in sys.argv[2:]:
        for name, expr, nret, where, line in module_calls(Path(p).read_bytes(), sys.argv[1]):
            print(f"{Path(p).name}:{line}  {expr}  -> {nret if nret >= 0 else 'mehrere'} Rueckgabe(n)")
