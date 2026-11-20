/*
 * empire_bridge.dll - macht jede Empire-Schlacht ohne eigenes battle_script zur Skript-Schlacht.
 *
 * Laden: aus data/battle_scripted.lua per
 *     package.loadlib("empire_bridge.dll", "luaopen_empire_bridge")()
 * Das passiert im BATTLE_ENV-Konstruktor (0x579ac0) bei 0x579de1, also VOR der Pruefung bei 0x57b8de.
 *
 * Patch (Empire.exe, Steam-Build 11084729, VAs bei ImageBase 0x400000):
 *   0x57b8de  cmp word [edi+0x280bc],0 ; jbe 0x57b947   (Laenge battle_script-Pfad == 0 -> kein Skript)
 * wird ersetzt durch jmp Stub. Der Stub setzt bei leerem Pfad unseren Pfad mit der Engine-Funktion
 * 0x4e4310 (WSTRING::assign(const wchar_t*, len)) und laeuft dann den Originalweg 0x57b8e8
 * (Battle-Script-Umgebung 0x655830 anlegen). Historische Schlachten behalten ihr eigenes Skript.
 *
 * Sicherheit: Vor dem Patchen werden die Original-Bytes geprueft. Passt etwas nicht, wird nichts veraendert.
 * Die DLL ruft keine Lua-API auf.
 *
 * Zusaetzlich: Overlay im Spielbild (overlay.c), abschaltbar mit "overlay = false" in ai_bridge/config.lua.
 *
 * Build: build.bat (MSVC x86).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define IMAGEBASE 0x400000u
#define RVA(va) ((DWORD)(va) - IMAGEBASE)

/* Patch-Stelle und Sprungziele */
#define VA_PATCH        0x57b8deu
#define VA_CREATE       0x57b8e8u   /* push 0x468 ; call new ; ... call 0x655830 */
#define VA_SKIP         0x57b947u   /* nach der Skript-Erzeugung */
#define VA_WSTR_ASSIGN  0x4e4310u   /* thiscall void assign(const wchar_t* s, unsigned len), len 0xffff = bis 0 */

#define ENV_SCRIPT_PATH 0x280b4     /* BATTLE_ENV: WSTRING battle_script (ptr, cap, len16 @+8, flag @+0xa) */
#define ENV_SCRIPT_LEN  0x280bc

static const BYTE ORIG_PATCH[10]  = { 0x66,0x83,0xbf,0xbc,0x80,0x02,0x00,0x00,0x76,0x5f };
static const BYTE ORIG_CREATE[5]  = { 0x68,0x68,0x04,0x00,0x00 };
static const BYTE ORIG_ASSIGN[10] = { 0x53,0x57,0x8b,0x7c,0x24,0x0c,0x8b,0xd9,0x85,0xff };

/* VFS-Pfad relativ zu data\ (wie scenarios\battle_of_rossbach.battle_script) */
static const wchar_t BRIDGE_SCRIPT[] = L"ai_bridge.battle_script";

/* thiscall (ecx=this, Stack-Args, Callee raeumt auf) == fastcall mit ungenutztem edx; in C ist __thiscall nicht erlaubt */
typedef void (__fastcall *WSTR_ASSIGN)(void *self, void *edx_unused, const wchar_t *s, unsigned len);

static DWORD       g_base;
static DWORD       g_create, g_skip;
static WSTR_ASSIGN g_assign;
static volatile LONG g_installed;
static LONG        g_battles;

static void logf(const char *fmt, ...)
{
	FILE *f = fopen("empire_bridge_log.txt", "a");
	if (!f) return;
	SYSTEMTIME t; GetLocalTime(&t);
	fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d  ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
	va_list ap; va_start(ap, fmt); vfprintf(f, fmt, ap); va_end(ap);
	fputc('\n', f);
	fclose(f);
}

/* fuer overlay.c */
void ov_log(const char *fmt, ...)
{
	char buf[512];
	va_list ap; va_start(ap, fmt); _vsnprintf(buf, sizeof buf - 1, fmt, ap); va_end(ap);
	buf[sizeof buf - 1] = 0;
	logf("%s", buf);
}

int overlay_install(void);   /* overlay.c */

static int bytes_match(DWORD va, const BYTE *expect, size_t n)
{
	const BYTE *p = (const BYTE *)(g_base + RVA(va));
	if (memcmp(p, expect, n) == 0) return 1;
	char got[64] = {0};
	for (size_t i = 0; i < n && i < 16; i++) sprintf(got + i * 3, "%02x ", p[i]);
	logf("Byte-Pruefung FEHLGESCHLAGEN bei 0x%08x: %s", va, got);
	return 0;
}

/* Wird vom Stub mit edi = BATTLE_ENV* aufgerufen. */
static void __cdecl on_battle_env(BYTE *env)
{
	LONG n = InterlockedIncrement(&g_battles);
	unsigned short len = *(unsigned short *)(env + ENV_SCRIPT_LEN);
	if (len != 0) {
		const wchar_t *p = *(const wchar_t **)(env + ENV_SCRIPT_PATH);
		logf("Schlacht #%ld: eigenes battle_script vorhanden (%.*ls), keine Aenderung", n, (int)len, p ? p : L"?");
		return;
	}
	__try {
		g_assign(env + ENV_SCRIPT_PATH, NULL, BRIDGE_SCRIPT, 0xffff);
		logf("Schlacht #%ld: kein battle_script -> '%ls' eingesetzt (len=%u)", n, BRIDGE_SCRIPT,
		     (unsigned)*(unsigned short *)(env + ENV_SCRIPT_LEN));
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		logf("Schlacht #%ld: Ausnahme 0x%08lx beim Setzen des Pfads", n, GetExceptionCode());
	}
}

/* Ersetzt: cmp word [edi+0x280bc],0 ; jbe skip */
__declspec(naked) static void stub(void)
{
	__asm {
		pushad
		pushfd
		push edi
		call on_battle_env
		add esp, 4
		popfd
		popad
		cmp word ptr [edi + ENV_SCRIPT_LEN], 0
		jbe no_script
		jmp dword ptr [g_create]
	no_script:
		jmp dword ptr [g_skip]
	}
}

static int install(void)
{
	g_base   = (DWORD)GetModuleHandleA(NULL);
	g_create = g_base + RVA(VA_CREATE);
	g_skip   = g_base + RVA(VA_SKIP);
	g_assign = (WSTR_ASSIGN)(g_base + RVA(VA_WSTR_ASSIGN));
	logf("empire_bridge: Empire.exe Basis 0x%08lx", g_base);
	{   /* nur in einem Abbild passender Groesse lesen (fremde EXE, z. B. Test) */
		IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)g_base;
		IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(g_base + dos->e_lfanew);
		if (nt->OptionalHeader.SizeOfImage < RVA(VA_PATCH) + 16) {
			logf("Kein passendes Empire.exe (Abbild %lu Bytes) - Patch NICHT angewendet", nt->OptionalHeader.SizeOfImage);
			return 0;
		}
	}

	BYTE *site = (BYTE *)(g_base + RVA(VA_PATCH));
	if (site[0] == 0xE9) {
		/* Bereits gepatcht (z. B. von einer frueheren Instanz): Sprung auf unseren aktuellen Stub neu ausrichten. */
		logf("Patch war bereits vorhanden (Ziel 0x%08lx), richte ihn auf 0x%08lx aus",
		     (DWORD)site + 5 + *(LONG *)(site + 1), (DWORD)stub);
	} else if (!bytes_match(VA_PATCH, ORIG_PATCH, sizeof ORIG_PATCH) ||
	    !bytes_match(VA_CREATE, ORIG_CREATE, sizeof ORIG_CREATE) ||
	    !bytes_match(VA_WSTR_ASSIGN, ORIG_ASSIGN, sizeof ORIG_ASSIGN)) {
		logf("Unbekannte Empire.exe-Version, Patch NICHT angewendet");
		return 0;
	}

	DWORD old;
	if (!VirtualProtect(site, 10, PAGE_EXECUTE_READWRITE, &old)) {
		logf("VirtualProtect fehlgeschlagen (%lu)", GetLastError());
		return 0;
	}
	BYTE patch[10];
	patch[0] = 0xE9;
	*(DWORD *)(patch + 1) = (DWORD)stub - ((DWORD)site + 5);
	memset(patch + 5, 0x90, 5);
	memcpy(site, patch, sizeof patch);
	VirtualProtect(site, 10, old, &old);
	FlushInstructionCache(GetCurrentProcess(), site, 10);
	logf("Patch bei 0x%08lx angewendet, Bridge-Skript: %ls", (DWORD)site, BRIDGE_SCRIPT);
	return 1;
}

/* Von package.loadlib aufgerufen: int f(lua_State*). Wir nutzen L nicht und geben 0 Werte zurueck. */
__declspec(dllexport) int __cdecl luaopen_empire_bridge(void *L)
{
	(void)L;
	if (InterlockedCompareExchange(&g_installed, 1, 0) == 0) {
		/* Lua 5.1 ruft beim Schliessen des Lua-Zustands FreeLibrary auf. Die DLL muss aber geladen bleiben,
		 * solange der Patch in Empire.exe auf den Stub zeigt -> Modul dauerhaft festhalten (PIN). */
		HMODULE self = NULL;
		if (GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
		                       (LPCWSTR)(void *)luaopen_empire_bridge, &self))
			logf("DLL im Speicher festgehalten (PIN)");
		else {
			logf("PIN fehlgeschlagen (%lu), Patch wird NICHT angewendet", GetLastError());
			InterlockedExchange(&g_installed, 2);
			return 0;
		}
		if (!install()) InterlockedExchange(&g_installed, 2);
		/* Overlay im Spielbild (unabhaengig vom Patch; schaltet sich bei Problemen selbst ab) */
		overlay_install();
	}
	return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID r)
{
	(void)r;
	if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(h);
	return TRUE;
}
