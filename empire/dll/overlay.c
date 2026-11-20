/*
 * overlay.c - Feldherr-Overlay im Spielbild (Stufe 1: nur Anzeige "Overlay aktiv").
 *
 * Empire zeichnet mit Direct3D 9 (32 Bit). Ueber ein eigenes, unsichtbares Hilfsgeraet holen wir die Adressen der
 * Funktionen Present und Reset in d3d9.dll und leiten die FUNKTIONEN selbst um (Hot-Patch: Windows-DLLs haben vor
 * jeder Funktion 5 Fuellbytes und am Anfang "mov edi,edi" genau dafuer). Die Methodentabellen liegen unter
 * Windows 10/11 pro Geraet im Speicher - sie umzubiegen erreicht das Geraet des Spiels nicht; die Funktionen sind
 * dagegen fuer alle Geraete dieselben.
 *
 * Gezeichnet wird unmittelbar vor Present gezielt in den Bildpuffer (eigene BeginScene/EndScene; in EndScene zu
 * zeichnen koennte in einem Zwischendurchgang wie einer Schattenkarte landen): ein halbtransparentes Feld unten
 * links und Text mit D3DXCreateFontW aus d3dx9_40.dll (bringt das Spiel mit). Die D3DX-Header gibt es nicht im Windows
 * SDK - die paar benoetigten Methoden von ID3DXFont sind unten von Hand beschrieben.
 *
 * Sicherheit: alles in __try/__except. Tritt eine Ausnahme auf, schaltet sich das Overlay ab und protokolliert das;
 * das Spiel laeuft weiter. Abschaltbar ohne Neubau: in ai_bridge/config.lua "overlay = false" (wird alle 2 s
 * gelesen).
 */
#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#include <windows.h>
#include <d3d9.h>
#include <stdio.h>
#include <string.h>

void ov_log(const char *fmt, ...);   /* aus empire_bridge.c */

/* ---------- D3DX: ID3DXFont von Hand (Reihenfolge wie d3dx9core.h) ---------- */
typedef struct XFont XFont;
typedef struct XFontVtbl {
	void *QueryInterface, *AddRef;
	ULONG (WINAPI *Release)(XFont *);
	void *GetDevice, *GetDescA, *GetDescW, *GetTextMetricsA, *GetTextMetricsW, *GetDC, *GetGlyphData,
	     *PreloadCharacters, *PreloadGlyphs, *PreloadTextA, *PreloadTextW, *DrawTextA;
	INT (WINAPI *DrawTextW)(XFont *, void *sprite, LPCWSTR s, INT n, LPRECT r, DWORD fmt, D3DCOLOR c);
	HRESULT (WINAPI *OnLostDevice)(XFont *);
	HRESULT (WINAPI *OnResetDevice)(XFont *);
} XFontVtbl;
struct XFont { const XFontVtbl *v; };
typedef HRESULT (WINAPI *PFN_CreateFontW)(IDirect3DDevice9 *, INT, UINT, UINT, UINT, BOOL, DWORD, DWORD, DWORD,
                                          DWORD, LPCWSTR, XFont **);
typedef IDirect3D9 *(WINAPI *PFN_Direct3DCreate9)(UINT);

/* ---------- Hook-Typen (vtable-Indizes von IDirect3DDevice9) ---------- */
#define VT_RESET     16
#define VT_PRESENT   17
typedef HRESULT (WINAPI *PFN_Reset)(IDirect3DDevice9 *, D3DPRESENT_PARAMETERS *);
typedef HRESULT (WINAPI *PFN_Present)(IDirect3DDevice9 *, const RECT *, const RECT *, HWND, const RGNDATA *);

static PFN_Reset g_orig_reset;       /* = Funktion + 2 (hinter "mov edi,edi") */
static PFN_Present g_orig_present;

static volatile LONG g_enabled = 1;        /* 0 = abgeschaltet (config oder Fehler) */
static LONG g_present_seen, g_draws;
static IDirect3DDevice9 *g_dev;            /* Geraet, fuer das Schrift/Statusblock angelegt sind */
static XFont *g_font;
static IDirect3DStateBlock9 *g_sb;
static PFN_CreateFontW g_createfont;
static DWORD g_cfg_tick;

static void release_resources(void)
{
	if (g_font) { g_font->v->Release(g_font); g_font = NULL; }
	if (g_sb) { IDirect3DStateBlock9_Release(g_sb); g_sb = NULL; }
	g_dev = NULL;
}

static void read_config(void)
{
	DWORD now = GetTickCount();
	if (now - g_cfg_tick < 2000) return;
	g_cfg_tick = now;
	FILE *f = fopen("ai_bridge\\config.lua", "r");
	if (!f) return;
	char buf[1024] = {0};
	fread(buf, 1, sizeof buf - 1, f);
	fclose(f);
	LONG want = strstr(buf, "overlay = false") ? 0 : 1;
	if (want != g_enabled) {
		ov_log("Overlay per config.lua %s", want ? "eingeschaltet" : "ausgeschaltet");
		InterlockedExchange(&g_enabled, want);
	}
}

typedef struct { float x, y, z, rhw; DWORD c; } Vtx;

static void draw(IDirect3DDevice9 *dev)
{
	if (dev != g_dev) {                    /* erstes Bild oder anderes Geraet */
		release_resources();
		g_dev = dev;
	}
	if (!g_font && g_createfont)
		g_createfont(dev, 18, 0, FW_SEMIBOLD, 1, FALSE, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, ANTIALIASED_QUALITY,
		             DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI", &g_font);
	if (!g_sb) IDirect3DDevice9_CreateStateBlock(dev, D3DSBT_ALL, &g_sb);
	if (!g_sb) return;

	D3DVIEWPORT9 vp;
	IDirect3DDevice9_GetViewport(dev, &vp);
	float w = 360.0f, h = 46.0f;
	float x0 = (float)vp.X + 14.0f, y0 = (float)(vp.Y + vp.Height) - h - 180.0f;   /* ueber der Einheitenleiste */

	IDirect3DStateBlock9_Capture(g_sb);
	IDirect3DDevice9_SetPixelShader(dev, NULL);
	IDirect3DDevice9_SetVertexShader(dev, NULL);
	IDirect3DDevice9_SetTexture(dev, 0, NULL);
	IDirect3DDevice9_SetFVF(dev, D3DFVF_XYZRHW | D3DFVF_DIFFUSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ZENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_LIGHTING, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_FOGENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_CULLMODE, D3DCULL_NONE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_STENCILENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SCISSORTESTENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ALPHATESTENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ALPHABLENDENABLE, TRUE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SRCBLEND, D3DBLEND_SRCALPHA);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_COLORWRITEENABLE, 0xF);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLOROP, D3DTOP_SELECTARG1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG1, D3DTA_DIFFUSE);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAOP, D3DTOP_SELECTARG1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAARG1, D3DTA_DIFFUSE);

	DWORD bg = D3DCOLOR_ARGB(170, 20, 22, 28), border = D3DCOLOR_ARGB(220, 196, 160, 80);
	Vtx box[4] = { {x0, y0, 0, 1, bg}, {x0 + w, y0, 0, 1, bg}, {x0, y0 + h, 0, 1, bg}, {x0 + w, y0 + h, 0, 1, bg} };
	IDirect3DDevice9_DrawPrimitiveUP(dev, D3DPT_TRIANGLESTRIP, 2, box, sizeof(Vtx));
	Vtx bar[4] = { {x0, y0, 0, 1, border}, {x0 + 4, y0, 0, 1, border}, {x0, y0 + h, 0, 1, border}, {x0 + 4, y0 + h, 0, 1, border} };
	IDirect3DDevice9_DrawPrimitiveUP(dev, D3DPT_TRIANGLESTRIP, 2, bar, sizeof(Vtx));

	if (g_font) {
		RECT r = { (LONG)x0 + 14, (LONG)y0 + 4, (LONG)(x0 + w) - 8, (LONG)(y0 + h) - 4 };
		g_font->v->DrawTextW(g_font, NULL, L"Empire Battle AI \x2013 Overlay aktiv", -1, &r,
		                     DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOCLIP, D3DCOLOR_ARGB(255, 235, 225, 200));
	}
	IDirect3DStateBlock9_Apply(g_sb);
	if (InterlockedIncrement(&g_draws) == 1)
		ov_log("Overlay: erstes Bild gezeichnet (Viewport %lux%lu, Schrift %s)", vp.Width, vp.Height, g_font ? "ok" : "FEHLT");
}

/* Vor Present: in den Bildpuffer zeichnen, Renderziel und Ausschnitt des Spiels danach wiederherstellen. */
static void draw_frame(IDirect3DDevice9 *dev)
{
	IDirect3DSurface9 *bb = NULL, *old = NULL;
	D3DVIEWPORT9 vp_old;
	if (FAILED(IDirect3DDevice9_GetBackBuffer(dev, 0, 0, D3DBACKBUFFER_TYPE_MONO, &bb)) || !bb) return;
	IDirect3DDevice9_GetViewport(dev, &vp_old);
	IDirect3DDevice9_GetRenderTarget(dev, 0, &old);
	IDirect3DDevice9_SetRenderTarget(dev, 0, bb);
	if (SUCCEEDED(IDirect3DDevice9_BeginScene(dev))) {
		draw(dev);
		IDirect3DDevice9_EndScene(dev);
	}
	if (old) { IDirect3DDevice9_SetRenderTarget(dev, 0, old); IDirect3DSurface9_Release(old); }
	IDirect3DDevice9_SetViewport(dev, &vp_old);
	IDirect3DSurface9_Release(bb);
}

static HRESULT WINAPI hk_Present(IDirect3DDevice9 *dev, const RECT *a, const RECT *b, HWND w, const RGNDATA *r)
{
	if (!g_present_seen) { g_present_seen = 1; ov_log("Overlay: Present wird aufgerufen"); }
	read_config();
	if (g_enabled) {
		__try {
			draw_frame(dev);
		} __except (EXCEPTION_EXECUTE_HANDLER) {
			InterlockedExchange(&g_enabled, 0);
			ov_log("Overlay: Ausnahme 0x%08lx beim Zeichnen - Overlay abgeschaltet", GetExceptionCode());
		}
	}
	return g_orig_present(dev, a, b, w, r);
}

static HRESULT WINAPI hk_Reset(IDirect3DDevice9 *dev, D3DPRESENT_PARAMETERS *pp)
{
	__try {
		if (g_font) g_font->v->OnLostDevice(g_font);
		if (g_sb) { IDirect3DStateBlock9_Release(g_sb); g_sb = NULL; }
	} __except (EXCEPTION_EXECUTE_HANDLER) {}
	HRESULT hr = g_orig_reset(dev, pp);
	__try {
		if (SUCCEEDED(hr) && g_font) g_font->v->OnResetDevice(g_font);
	} __except (EXCEPTION_EXECUTE_HANDLER) {}
	ov_log("Overlay: Reset (%s)", SUCCEEDED(hr) ? "ok" : "fehlgeschlagen");
	return hr;
}

/* Hot-Patch: 5 Fuellbytes vor fn -> "jmp hook", "mov edi,edi" am Anfang -> "jmp short -7". Original = fn + 2. */
static void *hotpatch(BYTE *fn, void *hook, const char *name)
{
	static const BYTE cc5[5] = { 0xCC, 0xCC, 0xCC, 0xCC, 0xCC }, nop5[5] = { 0x90, 0x90, 0x90, 0x90, 0x90 };
	if (fn[0] == 0xEB && fn[1] == 0xF9 && fn[-5] == 0xE9) {
		/* schon umgeleitet (zweite DLL-Instanz o. ae.): Ziel neu setzen */
	} else if (!(fn[0] == 0x8B && fn[1] == 0xFF && (memcmp(fn - 5, cc5, 5) == 0 || memcmp(fn - 5, nop5, 5) == 0))) {
		ov_log("Overlay: %s bei 0x%08lx nicht hot-patchbar (%02x %02x | %02x %02x %02x %02x %02x)", name, (DWORD)fn,
		       fn[0], fn[1], fn[-5], fn[-4], fn[-3], fn[-2], fn[-1]);
		return NULL;
	}
	DWORD old;
	if (!VirtualProtect(fn - 5, 7, PAGE_EXECUTE_READWRITE, &old)) {
		ov_log("Overlay: VirtualProtect fehlgeschlagen (%lu)", GetLastError());
		return NULL;
	}
	fn[-5] = 0xE9;
	*(DWORD *)(fn - 4) = (DWORD)hook - (DWORD)fn;
	*(volatile WORD *)fn = 0xF9EB;                  /* EB F9 in einem Schreibvorgang */
	VirtualProtect(fn - 5, 7, old, &old);
	FlushInstructionCache(GetCurrentProcess(), fn - 5, 7);
	ov_log("Overlay: %s bei 0x%08lx umgeleitet", name, (DWORD)fn);
	return fn + 2;
}

static LRESULT CALLBACK dummy_wndproc(HWND h, UINT m, WPARAM w, LPARAM l) { return DefWindowProcW(h, m, w, l); }

/* Einmal je Prozess aufrufen (aus luaopen_empire_bridge). */
int overlay_install(void)
{
	int ok = 0;
	void *present_fn = NULL, *reset_fn = NULL;
	__try {
		HMODULE d3d9 = GetModuleHandleW(L"d3d9.dll");
		if (!d3d9) d3d9 = LoadLibraryW(L"d3d9.dll");
		HMODULE d3dx = GetModuleHandleW(L"d3dx9_40.dll");
		if (!d3dx) d3dx = LoadLibraryW(L"d3dx9_40.dll");
		PFN_Direct3DCreate9 create = d3d9 ? (PFN_Direct3DCreate9)GetProcAddress(d3d9, "Direct3DCreate9") : NULL;
		g_createfont = d3dx ? (PFN_CreateFontW)GetProcAddress(d3dx, "D3DXCreateFontW") : NULL;
		if (!create) { ov_log("Overlay: d3d9.dll/Direct3DCreate9 nicht gefunden"); return 0; }
		if (!g_createfont) ov_log("Overlay: D3DXCreateFontW nicht gefunden - nur Feld ohne Text");

		WNDCLASSW wc = {0};
		wc.lpfnWndProc = dummy_wndproc;
		wc.hInstance = GetModuleHandleW(NULL);
		wc.lpszClassName = L"EmpireBattleAIOverlayDummy";
		RegisterClassW(&wc);
		HWND hwnd = CreateWindowExW(0, wc.lpszClassName, L"", WS_OVERLAPPEDWINDOW, 0, 0, 8, 8, NULL, NULL, wc.hInstance, NULL);
		IDirect3D9 *d3d = create(D3D_SDK_VERSION);
		if (!d3d || !hwnd) { ov_log("Overlay: Hilfsfenster/Direct3D nicht erzeugt"); return 0; }

		static const DWORD flags[2] = { D3DCREATE_SOFTWARE_VERTEXPROCESSING,
		                                D3DCREATE_HARDWARE_VERTEXPROCESSING | D3DCREATE_PUREDEVICE };
		for (int i = 0; i < 2; i++) {
			D3DPRESENT_PARAMETERS pp = {0};
			pp.Windowed = TRUE;
			pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
			pp.BackBufferFormat = D3DFMT_UNKNOWN;
			pp.BackBufferWidth = pp.BackBufferHeight = 8;
			pp.hDeviceWindow = hwnd;
			IDirect3DDevice9 *dev = NULL;
			HRESULT hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, hwnd, flags[i] | D3DCREATE_NOWINDOWCHANGES, &pp, &dev);
			if (FAILED(hr) || !dev) {
				ov_log("Overlay: Hilfsgeraet %d nicht erzeugt (0x%08lx)", i, hr);
				continue;
			}
			void **vt = *(void ***)dev;
			if (!present_fn) { present_fn = vt[VT_PRESENT]; reset_fn = vt[VT_RESET]; }
			IDirect3DDevice9_Release(dev);
			if (present_fn) break;
		}
		IDirect3D9_Release(d3d);
		DestroyWindow(hwnd);
		UnregisterClassW(wc.lpszClassName, wc.hInstance);
		if (present_fn && reset_fn) {
			/* Reset zuerst: ohne Reset-Umleitung darf Present nicht zeichnen (Schrift muesste beim Geraeteverlust
			 * freigegeben werden) */
			g_orig_reset = (PFN_Reset)hotpatch((BYTE *)reset_fn, (void *)hk_Reset, "Reset");
			if (g_orig_reset) {
				g_orig_present = (PFN_Present)hotpatch((BYTE *)present_fn, (void *)hk_Present, "Present");
				ok = g_orig_present != NULL;
			}
		}
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		ov_log("Overlay: Ausnahme 0x%08lx bei der Einrichtung - kein Overlay", GetExceptionCode());
		return 0;
	}
	ov_log(ok ? "Overlay eingerichtet" : "Overlay NICHT eingerichtet");
	return ok;
}
