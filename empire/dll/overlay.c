/*
 * overlay.c - Feldherr-Overlay im Spielbild: die letzten Meldungen des Feldherrn unten links.
 *
 * Empire zeichnet mit Direct3D 9 (32 Bit). Ueber ein eigenes, unsichtbares Hilfsgeraet holen wir die Adresse der
 * Funktion Present und leiten die FUNKTION selbst um (Hot-Patch: Windows-DLLs haben vor jeder Funktion 5 Fuellbytes
 * und am Anfang "mov edi,edi" genau dafuer; ist dort schon ein anderer Hook, z. B. Steam-Overlay oder die
 * Windows-Kompatibilitaetsschicht, haengen wir uns davor). Die Methodentabellen liegen unter Windows 10/11 pro
 * Geraet im Speicher - sie umzubiegen erreicht das Geraet des Spiels nicht.
 *
 * KEINE dauerhaften Grafikobjekte: Schrift und Statusblock hielten Referenzen auf das Geraet; beim Wechsel Vollbild
 * <-> Fenster legt Empire ein neues Geraet an, das alte blieb dadurch am Leben bzw. die Kompatibilitaetsschicht
 * raeumte es weg, waehrend wir noch Referenzen hielten -> Freeze/Absturz (Schlacht 2026-11-20 12:45, im Test
 * nachgestellt). Deshalb: Text mit Windows-GDI in ein normales Speicherbild (nur wenn sich die Meldungen aendern),
 * je Bild daraus eine Textur und einen Statusblock anlegen, zeichnen, sofort wieder freigeben. Damit braucht es
 * weder Reset- noch CreateDevice-Umleitung und kein D3DX.
 *
 * Gezeichnet wird unmittelbar vor Present gezielt in den Bildpuffer (eigene BeginScene/EndScene).
 * Sicherheit: alles in __try/__except. Tritt eine Ausnahme auf, schaltet sich das Overlay ab und protokolliert das.
 * Abschaltbar ohne Neubau: in ai_bridge/config.lua "overlay = false" (wird alle 2 s gelesen).
 *
 * Aufgeklappt (F8): das Steuerpult zeichnet seine kompakte Oberflaeche selbst (python/overlay_panel.py) nach
 * ai_bridge/overlay_panel.bin; die DLL zeigt dieses Bild statt der Meldungsleiste. F8-Druecke zaehlt die DLL
 * (nur wenn Empire im Vordergrund ist) und meldet sie in ai_bridge/overlay_keys.txt. Schreibt das Steuerpult
 * 5 s lang kein neues Bild (geschlossen/abgestuerzt), faellt die Anzeige auf die Meldungsleiste zurueck.
 */
#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#include <windows.h>
#include <d3d9.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

void ov_log(const char *fmt, ...);   /* aus empire_bridge.c */

typedef IDirect3D9 *(WINAPI *PFN_Direct3DCreate9)(UINT);

/* ---------- Hook-Typen (vtable-Indizes von IDirect3DDevice9) ---------- */
#define VT_PRESENT   17
typedef HRESULT (WINAPI *PFN_Present)(IDirect3DDevice9 *, const RECT *, const RECT *, HWND, const RGNDATA *);

static PFN_Present g_orig_present;

static volatile LONG g_enabled = 1;        /* 0 = abgeschaltet (config oder Fehler) */
static LONG g_present_seen, g_draws;
static DWORD g_cfg_tick;

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
typedef struct { float x, y, z, rhw; DWORD c; float u, v; } TVtx;

/* ---------- Meldungen des Feldherrn (ai_bridge/overlay.txt, siehe python/empire_general/overlay_feed.py) ---------- */
#define FEED_MAX 14
#define FEED_LEN 200
static wchar_t g_lines[FEED_MAX][FEED_LEN];
static wchar_t g_kinds[FEED_MAX];
static int g_nlines;
static DWORD g_feed_tick;
static int g_text_dirty = 1;

static void read_feed(void)
{
	DWORD now = GetTickCount();
	if (now - g_feed_tick < 1000) return;
	g_feed_tick = now;
	FILE *f = fopen("ai_bridge\\overlay.txt", "rb");
	if (!f) return;                       /* fehlt oder wird gerade ersetzt - alte Zeilen bleiben stehen */
	static char raw[8192];
	static wchar_t wide[8192];
	size_t n = fread(raw, 1, sizeof raw - 1, f);
	fclose(f);
	raw[n] = 0;
	int wn = MultiByteToWideChar(CP_UTF8, 0, raw, (int)n, wide, (int)(sizeof wide / sizeof wide[0]) - 1);
	if (wn <= 0) return;
	wide[wn] = 0;
	int count = 0;
	wchar_t *p = wide;
	if (p[0] == 0xFEFF) p++;              /* BOM */
	while (*p && count < FEED_MAX) {
		wchar_t *e = wcschr(p, L'\n');
		size_t len = e ? (size_t)(e - p) : wcslen(p);
		if (len && p[len - 1] == L'\r') len--;
		if (len > 1) {
			g_kinds[count] = p[0];
			size_t m = len - 1 < FEED_LEN - 1 ? len - 1 : FEED_LEN - 1;
			wcsncpy(g_lines[count], p + 1, m);
			g_lines[count][m] = 0;
			count++;
		}
		if (!e) break;
		p = e + 1;
	}
	g_nlines = count;
	g_text_dirty = 1;
}

static D3DCOLOR kind_color(wchar_t k)
{
	switch (k) {
	case L'+': return D3DCOLOR_ARGB(255, 170, 225, 150);   /* aktueller Plan meiner Armee */
	case L'>': return D3DCOLOR_ARGB(255, 150, 200, 255);   /* eigener Befehl */
	case L'*': return D3DCOLOR_ARGB(255, 245, 238, 220);   /* Antwort des Feldherrn */
	case L'!': return D3DCOLOR_ARGB(255, 255, 170, 80);    /* Warnung */
	default:   return D3DCOLOR_ARGB(255, 175, 175, 175);   /* Hinweis */
	}
}

/* ---------- F8 und Bild des Steuerpults (aufgeklapptes Overlay) ---------- */
#define PANEL_MAX_W 1024
#define PANEL_MAX_H 768
static LONG g_f8_count;
static int g_f8_down;
static DWORD *g_panel_px;
static int g_panel_w, g_panel_h;
static DWORD g_panel_seq = 0xFFFFFFFF, g_panel_flags, g_panel_tick, g_panel_check;

static void poll_keys(void)
{
	HWND fg = GetForegroundWindow();
	DWORD pid = 0;
	if (fg) GetWindowThreadProcessId(fg, &pid);
	int down = (GetAsyncKeyState(VK_F8) & 0x8000) != 0;
	if (down && !g_f8_down && pid == GetCurrentProcessId()) {
		g_f8_count++;
		FILE *f = fopen("ai_bridge\\overlay_keys.txt", "w");
		if (f) { fprintf(f, "f8=%ld\n", g_f8_count); fclose(f); }
	}
	g_f8_down = down;
}

static void read_panel(void)
{
	DWORD now = GetTickCount();
	if (now - g_panel_check < 150) return;
	g_panel_check = now;
	FILE *f = fopen("ai_bridge\\overlay_panel.bin", "rb");
	if (!f) return;
	DWORD head[5];
	if (fread(head, 4, 5, f) == 5 && head[0] == 0x564F4245 /* 'EBOV' */ && head[3] != g_panel_seq) {
		DWORD w = head[1], h = head[2];
		if ((head[4] & 1) && w && h && w <= PANEL_MAX_W && h <= PANEL_MAX_H) {
			if (!g_panel_px) g_panel_px = (DWORD *)malloc((size_t)PANEL_MAX_W * PANEL_MAX_H * 4);
			if (g_panel_px && fread(g_panel_px, 4, (size_t)w * h, f) == (size_t)w * h) {
				g_panel_w = (int)w;
				g_panel_h = (int)h;
				g_panel_seq = head[3];
				g_panel_flags = head[4];
				g_panel_tick = now;
			}
		} else {
			g_panel_seq = head[3];
			g_panel_flags = head[4];
			g_panel_tick = now;
		}
	}
	fclose(f);
}

static int panel_open(void)
{
	return (g_panel_flags & 1) && g_panel_px && g_panel_w && GetTickCount() - g_panel_tick < 5000;
}

/* aufgeklapptes Overlay: Bild des Steuerpults zeichnen (eigene Deckkraft im Bild) */
static void draw_panel(IDirect3DDevice9 *dev)
{
	IDirect3DStateBlock9 *sb = NULL;
	IDirect3DTexture9 *tex = NULL;
	DWORD lockflags = D3DLOCK_DISCARD;
	if (FAILED(IDirect3DDevice9_CreateStateBlock(dev, D3DSBT_ALL, &sb)) || !sb) return;
	if (FAILED(IDirect3DDevice9_CreateTexture(dev, g_panel_w, g_panel_h, 1, D3DUSAGE_DYNAMIC, D3DFMT_A8R8G8B8,
	                                          D3DPOOL_DEFAULT, &tex, NULL)) || !tex) {
		tex = NULL;
		lockflags = 0;
		if (FAILED(IDirect3DDevice9_CreateTexture(dev, g_panel_w, g_panel_h, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED,
		                                          &tex, NULL)))
			tex = NULL;
	}
	if (!tex) { IDirect3DStateBlock9_Release(sb); return; }
	D3DLOCKED_RECT lr;
	if (FAILED(IDirect3DTexture9_LockRect(tex, 0, &lr, NULL, lockflags))) {
		IDirect3DTexture9_Release(tex);
		IDirect3DStateBlock9_Release(sb);
		return;
	}
	for (int y = 0; y < g_panel_h; y++)
		memcpy((BYTE *)lr.pBits + (size_t)y * lr.Pitch, g_panel_px + (size_t)y * g_panel_w, (size_t)g_panel_w * 4);
	IDirect3DTexture9_UnlockRect(tex, 0);

	D3DVIEWPORT9 vp;
	IDirect3DDevice9_GetViewport(dev, &vp);
	float w = (float)g_panel_w, h = (float)g_panel_h;
	float x0 = (float)vp.X + 14.0f, y0 = (float)(vp.Y + vp.Height) - h - 180.0f;
	if (y0 < (float)vp.Y + 10.0f) y0 = (float)vp.Y + 10.0f;

	IDirect3DStateBlock9_Capture(sb);
	IDirect3DDevice9_SetPixelShader(dev, NULL);
	IDirect3DDevice9_SetVertexShader(dev, NULL);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ZENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_LIGHTING, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_FOGENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_CULLMODE, D3DCULL_NONE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_STENCILENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SCISSORTESTENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ALPHATESTENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SRGBWRITEENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ALPHABLENDENABLE, TRUE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_BLENDOP, D3DBLENDOP_ADD);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SRCBLEND, D3DBLEND_SRCALPHA);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_COLORWRITEENABLE, 0xF);
	IDirect3DDevice9_SetTextureStageState(dev, 1, D3DTSS_COLOROP, D3DTOP_DISABLE);
	IDirect3DDevice9_SetTextureStageState(dev, 1, D3DTSS_ALPHAOP, D3DTOP_DISABLE);
	IDirect3DDevice9_SetTexture(dev, 0, (IDirect3DBaseTexture9 *)tex);
	IDirect3DDevice9_SetFVF(dev, D3DFVF_XYZRHW | D3DFVF_DIFFUSE | D3DFVF_TEX1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_TEXCOORDINDEX, 0);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_TEXTURETRANSFORMFLAGS, D3DTTFF_DISABLE);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLOROP, D3DTOP_SELECTARG1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG1, D3DTA_TEXTURE);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAOP, D3DTOP_SELECTARG1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAARG1, D3DTA_TEXTURE);
	IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MINFILTER, D3DTEXF_POINT);
	IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MAGFILTER, D3DTEXF_POINT);
	IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MIPFILTER, D3DTEXF_NONE);
	IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_ADDRESSU, D3DTADDRESS_CLAMP);
	IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_ADDRESSV, D3DTADDRESS_CLAMP);
	IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_SRGBTEXTURE, FALSE);
	DWORD c = 0xFFFFFFFF;
	float X = x0 - 0.5f, Y = y0 - 0.5f;
	TVtx v[4] = { {X, Y, 0, 1, c, 0, 0}, {X + w, Y, 0, 1, c, 1, 0}, {X, Y + h, 0, 1, c, 0, 1}, {X + w, Y + h, 0, 1, c, 1, 1} };
	IDirect3DDevice9_DrawPrimitiveUP(dev, D3DPT_TRIANGLESTRIP, 2, v, sizeof(TVtx));
	IDirect3DStateBlock9_Apply(sb);
	IDirect3DStateBlock9_Release(sb);
	IDirect3DTexture9_Release(tex);
}

/* ---------- Text als Bild (GDI, kein Grafikobjekt) ---------- */

#define TEX_H 512
#define BOX_W 640
#define TITLE_H 26
#define LINE_H 21
static HDC g_dc;
static DWORD *g_bits;            /* BOX_W x TEX_H, 32 Bit, oben zuerst */
static int g_text_h;

static void render_text(void)
{
	if (!g_dc) {
		g_dc = CreateCompatibleDC(NULL);
		BITMAPINFO bi = {0};
		bi.bmiHeader.biSize = sizeof bi.bmiHeader;
		bi.bmiHeader.biWidth = BOX_W;
		bi.bmiHeader.biHeight = -TEX_H;
		bi.bmiHeader.biPlanes = 1;
		bi.bmiHeader.biBitCount = 32;
		HBITMAP bmp = CreateDIBSection(g_dc, &bi, DIB_RGB_COLORS, (void **)&g_bits, NULL, 0);
		SelectObject(g_dc, bmp);
		HFONT f = CreateFontW(-17, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS,
		                      CLIP_DEFAULT_PRECIS, ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
		SelectObject(g_dc, f);
		SetBkMode(g_dc, TRANSPARENT);
		SetTextColor(g_dc, RGB(255, 255, 255));
	}
	if (!g_bits) return;
	int n = g_nlines ? g_nlines : 1;
	g_text_h = TITLE_H + n * LINE_H + 10;
	if (g_text_h > TEX_H) g_text_h = TEX_H;
	memset(g_bits, 0, (size_t)BOX_W * TEX_H * 4);
	TextOutW(g_dc, 14, 4, L"Feldherr", 8);
	if (!g_nlines) {
		const wchar_t *t = L"Overlay aktiv - noch keine Meldungen des Feldherrn";
		TextOutW(g_dc, 14, TITLE_H, t, (int)wcslen(t));
	}
	/* Folgezeilen einer Meldung beginnen mit Leerzeichen: buendig unter dem Text nach der Uhrzeit ausrichten
	 * (Proportionalschrift - Leerzeichen allein treffen die Kante nicht) */
	SIZE stamp = {0};
	GetTextExtentPoint32W(g_dc, L"00:00  ", 7, &stamp);
	for (int i = 0; i < g_nlines; i++) {
		const wchar_t *t = g_lines[i];
		int x = 14;
		if (t[0] == L' ') {
			while (*t == L' ') t++;
			x += stamp.cx;
		}
		TextOutW(g_dc, x, TITLE_H + i * LINE_H, t, (int)wcslen(t));
	}
	GdiFlush();
	g_text_dirty = 0;
}

static void quad(IDirect3DDevice9 *dev, float x, float y, float w, float h, DWORD c)
{
	Vtx v[4] = { {x, y, 0, 1, c}, {x + w, y, 0, 1, c}, {x, y + h, 0, 1, c}, {x + w, y + h, 0, 1, c} };
	IDirect3DDevice9_DrawPrimitiveUP(dev, D3DPT_TRIANGLESTRIP, 2, v, sizeof(Vtx));
}

/* Zeilen row0..row0+rows des Textbilds an (x, y) zeichnen, eingefaerbt mit c */
static void text_rows(IDirect3DDevice9 *dev, float x, float y, int row0, int rows, DWORD c)
{
	float u1 = 1.0f, v0 = (float)row0 / g_text_h, v1 = (float)(row0 + rows) / g_text_h;   /* Textur = BOX_W x g_text_h */
	float X = x - 0.5f, Y = y - 0.5f, W = (float)BOX_W, H = (float)rows;
	TVtx v[4] = { {X, Y, 0, 1, c, 0, v0}, {X + W, Y, 0, 1, c, u1, v0}, {X, Y + H, 0, 1, c, 0, v1},
	              {X + W, Y + H, 0, 1, c, u1, v1} };
	IDirect3DDevice9_DrawPrimitiveUP(dev, D3DPT_TRIANGLESTRIP, 2, v, sizeof(TVtx));
}

static void draw(IDirect3DDevice9 *dev)
{
	poll_keys();
	read_panel();
	if (panel_open()) {             /* aufgeklappt: Bedienoberflaeche des Steuerpults */
		draw_panel(dev);
		if (InterlockedIncrement(&g_draws) == 1) ov_log("Overlay: erstes Bild gezeichnet (Steuerpult-Ansicht)");
		return;
	}
	read_feed();
	if (g_text_dirty) render_text();
	if (!g_bits || !g_text_h) return;

	/* je Bild anlegen und am Ende freigeben - nichts haelt das Geraet ueber das Bild hinaus fest */
	IDirect3DStateBlock9 *sb = NULL;
	IDirect3DTexture9 *tex = NULL;
	if (FAILED(IDirect3DDevice9_CreateStateBlock(dev, D3DSBT_ALL, &sb)) || !sb) return;
	/* nur so gross wie noetig; dynamisch direkt in den Grafikspeicher (schnell), sonst verwaltet (immer moeglich) */
	DWORD lockflags = D3DLOCK_DISCARD;
	if (FAILED(IDirect3DDevice9_CreateTexture(dev, BOX_W, g_text_h, 1, D3DUSAGE_DYNAMIC, D3DFMT_A8R8G8B8,
	                                          D3DPOOL_DEFAULT, &tex, NULL)) || !tex) {
		tex = NULL;
		lockflags = 0;
		if (FAILED(IDirect3DDevice9_CreateTexture(dev, BOX_W, g_text_h, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED,
		                                          &tex, NULL)))
			tex = NULL;
	}
	if (tex) {
		D3DLOCKED_RECT lr;
		if (SUCCEEDED(IDirect3DTexture9_LockRect(tex, 0, &lr, NULL, lockflags))) {
			for (int y = 0; y < g_text_h; y++) {
				DWORD *dst = (DWORD *)((BYTE *)lr.pBits + (size_t)y * lr.Pitch);
				const DWORD *src = g_bits + (size_t)y * BOX_W;
				for (int x = 0; x < BOX_W; x++)
					dst[x] = ((src[x] & 0xFF) << 24) | 0x00FFFFFF;   /* weiss auf schwarz: Helligkeit = Deckkraft */
			}
			IDirect3DTexture9_UnlockRect(tex, 0);
		} else {
			IDirect3DTexture9_Release(tex);
			tex = NULL;
		}
	}

	D3DVIEWPORT9 vp;
	IDirect3DDevice9_GetViewport(dev, &vp);
	float w = (float)BOX_W, h = (float)g_text_h;
	if (w > (float)vp.Width - 28.0f) w = (float)vp.Width - 28.0f;
	float x0 = (float)vp.X + 14.0f, y0 = (float)(vp.Y + vp.Height) - h - 180.0f;   /* ueber der Einheitenleiste */

	IDirect3DStateBlock9_Capture(sb);
	IDirect3DDevice9_SetPixelShader(dev, NULL);
	IDirect3DDevice9_SetVertexShader(dev, NULL);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ZENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_LIGHTING, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_FOGENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_CULLMODE, D3DCULL_NONE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_STENCILENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SCISSORTESTENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ALPHATESTENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SRGBWRITEENABLE, FALSE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_ALPHABLENDENABLE, TRUE);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_BLENDOP, D3DBLENDOP_ADD);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_SRCBLEND, D3DBLEND_SRCALPHA);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA);
	IDirect3DDevice9_SetRenderState(dev, D3DRS_COLORWRITEENABLE, 0xF);
	IDirect3DDevice9_SetTextureStageState(dev, 1, D3DTSS_COLOROP, D3DTOP_DISABLE);
	IDirect3DDevice9_SetTextureStageState(dev, 1, D3DTSS_ALPHAOP, D3DTOP_DISABLE);

	/* Feld und goldene Leiste (ohne Textur) */
	IDirect3DDevice9_SetTexture(dev, 0, NULL);
	IDirect3DDevice9_SetFVF(dev, D3DFVF_XYZRHW | D3DFVF_DIFFUSE);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLOROP, D3DTOP_SELECTARG1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG1, D3DTA_DIFFUSE);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAOP, D3DTOP_SELECTARG1);
	IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAARG1, D3DTA_DIFFUSE);
	quad(dev, x0, y0, w, h, D3DCOLOR_ARGB(170, 20, 22, 28));
	quad(dev, x0, y0, 4, h, D3DCOLOR_ARGB(220, 196, 160, 80));

	/* Text: Textur (Deckkraft) x Farbe je Zeile */
	if (tex) {
		IDirect3DDevice9_SetTexture(dev, 0, (IDirect3DBaseTexture9 *)tex);
		IDirect3DDevice9_SetFVF(dev, D3DFVF_XYZRHW | D3DFVF_DIFFUSE | D3DFVF_TEX1);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_TEXCOORDINDEX, 0);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_TEXTURETRANSFORMFLAGS, D3DTTFF_DISABLE);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLOROP, D3DTOP_MODULATE);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG1, D3DTA_TEXTURE);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG2, D3DTA_DIFFUSE);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAOP, D3DTOP_MODULATE);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAARG1, D3DTA_TEXTURE);
		IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_ALPHAARG2, D3DTA_DIFFUSE);
		IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MINFILTER, D3DTEXF_POINT);
		IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MAGFILTER, D3DTEXF_POINT);
		IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MIPFILTER, D3DTEXF_NONE);
		IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_ADDRESSU, D3DTADDRESS_CLAMP);
		IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_ADDRESSV, D3DTADDRESS_CLAMP);
		IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_SRGBTEXTURE, FALSE);
		text_rows(dev, x0, y0, 0, TITLE_H, D3DCOLOR_ARGB(255, 214, 178, 96));
		int n = g_nlines ? g_nlines : 1;
		for (int i = 0; i < n; i++) {
			int r0 = TITLE_H + i * LINE_H, rows = (i == n - 1) ? g_text_h - r0 : LINE_H;
			if (rows <= 0) break;
			text_rows(dev, x0, y0 + r0, r0, rows,
			          g_nlines ? kind_color(g_kinds[i]) : D3DCOLOR_ARGB(255, 175, 175, 175));
		}
	}
	IDirect3DStateBlock9_Apply(sb);
	IDirect3DStateBlock9_Release(sb);
	if (tex) IDirect3DTexture9_Release(tex);
	if (InterlockedIncrement(&g_draws) == 1)
		ov_log("Overlay: erstes Bild gezeichnet (Viewport %lux%lu, Text %s)", vp.Width, vp.Height, tex ? "ok" : "FEHLT");
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

static BYTE *jmp_target(BYTE *at) { return at + 5 + *(LONG *)(at + 1); }

/* einen vorhandenen 5-Byte-Sprung (E9) auf unseren Hook umsetzen; unser "Original" ist das bisherige Sprungziel */
static void *chain_jump(BYTE *at, void *hook, const char *name, const char *who)
{
	BYTE *prev = jmp_target(at);
	if (prev == (BYTE *)hook) { ov_log("Overlay: %s ist bereits umgeleitet", name); return NULL; }
	DWORD old;
	if (!VirtualProtect(at, 5, PAGE_EXECUTE_READWRITE, &old)) {
		ov_log("Overlay: VirtualProtect fehlgeschlagen (%lu)", GetLastError());
		return NULL;
	}
	*(LONG *)(at + 1) = (LONG)((BYTE *)hook - (at + 5));     /* nur das Sprungziel aendern - E9 bleibt stehen */
	VirtualProtect(at, 5, old, &old);
	FlushInstructionCache(GetCurrentProcess(), at, 5);
	ov_log("Overlay: %s war schon umgeleitet (%s, Ziel 0x%08lx) - unser Overlay haengt sich davor", name, who, (DWORD)prev);
	return prev;
}

/* Umleitung von fn auf hook; liefert die Funktion, die der Hook zum Weiterreichen aufruft (oder NULL):
 *  - unberuehrt (Hot-Patch-Form): 5 Fuellbytes vor fn -> "jmp hook", "mov edi,edi" -> "jmp short -7"; weiter: fn + 2
 *  - schon von einem anderen Programm umgeleitet (z. B. Steam-Overlay), Sprung E9 am Anfang oder Hot-Patch davor:
 *    wir setzen nur das Sprungziel auf uns und reichen an das bisherige Ziel weiter. Kette: Spiel -> wir -> Steam -> d3d9 */
static void *hotpatch(BYTE *fn, void *hook, const char *name)
{
	static const BYTE cc5[5] = { 0xCC, 0xCC, 0xCC, 0xCC, 0xCC }, nop5[5] = { 0x90, 0x90, 0x90, 0x90, 0x90 };
	if (fn[0] == 0xE9)
		return chain_jump(fn, hook, name, "Sprung am Funktionsanfang");
	if (fn[0] == 0xEB && fn[1] == 0xF9 && fn[-5] == 0xE9)
		return chain_jump(fn - 5, hook, name, "Hot-Patch");
	if (!(fn[0] == 0x8B && fn[1] == 0xFF && (memcmp(fn - 5, cc5, 5) == 0 || memcmp(fn - 5, nop5, 5) == 0))) {
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
	void *present_fn = NULL;
	__try {
		HMODULE d3d9 = GetModuleHandleW(L"d3d9.dll");
		if (!d3d9) d3d9 = LoadLibraryW(L"d3d9.dll");
		PFN_Direct3DCreate9 create = d3d9 ? (PFN_Direct3DCreate9)GetProcAddress(d3d9, "Direct3DCreate9") : NULL;
		if (!create) { ov_log("Overlay: d3d9.dll/Direct3DCreate9 nicht gefunden"); return 0; }

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
			if (!present_fn) present_fn = vt[VT_PRESENT];
			IDirect3DDevice9_Release(dev);
			if (present_fn) break;
		}
		IDirect3D9_Release(d3d);
		DestroyWindow(hwnd);
		UnregisterClassW(wc.lpszClassName, wc.hInstance);
		if (present_fn) {
			g_orig_present = (PFN_Present)hotpatch((BYTE *)present_fn, (void *)hk_Present, "Present");
			ok = g_orig_present != NULL;
		}
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		ov_log("Overlay: Ausnahme 0x%08lx bei der Einrichtung - kein Overlay", GetExceptionCode());
		return 0;
	}
	ov_log(ok ? "Overlay eingerichtet" : "Overlay NICHT eingerichtet");
	return ok;
}
