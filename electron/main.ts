/**
 * Electron main entrypoint. Wires the SidecarClient (pure module) to the
 * BrowserWindow. This file is the ONLY place that imports `electron`.
 *
 * Everything else in `electron/` is pure and unit-tested headless.
 *
 * IPC surface (renderer side goes through preload's `window.agent`):
 *   invoke  agent:status      -> sidecar status snapshot
 *   invoke  agent:ping        -> { pong, ts }
 *   invoke  agent:info        -> sidecar info
 *   invoke  agent:tools       -> tools.list (schemas + permission levels)
 *   invoke  agent:skills     -> skills.list (installed skills + skipped)
 *   invoke  agent:toolToggle -> tools.setEnabled {name, enabled} -> tools.list
 *   invoke  agent:chat        -> { chatId } ; events stream back on agent:event
 *   invoke  agent:cancel      -> { cancelled }
 *   invoke  agent:approval    -> { accepted } (answers an approval.request)
 *   invoke  agent:openExternal -> open a link in the system browser (http/https only)
 *   invoke  agent:saveFile     -> save a data: URL via the native save dialog
 *   invoke  agent:pickImages  -> string[] of picked file paths
 *   invoke  snip:start       -> open the in-app screenshot overlay (bool)
 *   on      snip:attached    -> a finished screenshot's temp-file path
 *   invoke  agent:sessions         -> sessions.list (the sidebar's rows)
 *   invoke  agent:sessionCreate    -> { id, title } (caller may pass sessionId)
 *   invoke  agent:sessionRename    -> { sessionId, title }
 *   invoke  agent:sessionDelete    -> { sessionId }
 *   invoke  agent:sessionMessages  -> one session's messages, in order
 *   invoke  agent:messagesSearch   -> { query, sessionId? } FTS5 hits
 *   invoke  agent:usage            -> token/money aggregation (bar + usage page)
 *   on      agent:event       -> every sidecar evt frame, { chatId, event, data }
 *   on      agent:status      -> status transitions
 */
import { app, BrowserWindow, clipboard, desktopCapturer, dialog, globalShortcut, ipcMain, Menu, nativeTheme, screen, shell, Tray } from "electron";
import * as fs from "fs";
import * as path from "path";
import { randomUUID } from "crypto";
import { SidecarClient } from "./sidecarClient";
import { StdioTransport } from "./stdioTransport";
import { defaultDeps } from "./deps";

let mainWindow: BrowserWindow | null = null;
let client: SidecarClient | null = null;

/**
 * The project root: the directory that contains `python/` and `electron/`.
 *
 * Do NOT derive this by walking up from `__dirname` — the compiled main lives
 * in `dist/electron/`, so `__dirname/..` is `dist/`, and joining "python" onto
 * that yields the non-existent `dist/python/sidecar.py` (Windows exits 9009 =
 * file not found). `app.getAppPath()` is the app directory when launched with
 * `electron .` and the app bundle when packaged — correct in both cases.
 */
/** 只把 http(s) 外链交给系统浏览器，其余一律拒绝并返回 false。 */
async function openExternalSafe(url: unknown): Promise<boolean> {
  const target = typeof url === "string" ? url.trim() : "";
  if (!/^https?:\/\//i.test(target)) return false;
  try {
    await shell.openExternal(target);
    return true;
  } catch {
    return false;
  }
}

function projectRoot(): string {
  return app.getAppPath();
}

function getSidecarCommand(): { command: string; args: string[] } {
  const root = projectRoot();
  const script = path.join(root, "python", "sidecar.py");
  // Prefer a venv interpreter when present (uv-managed or stdlib venv).
  const candidates = process.platform === "win32"
    ? [path.join(root, "python", ".venv", "Scripts", "python.exe")]
    : [path.join(root, "python", ".venv", "bin", "python3")];
  for (const py of candidates) {
    if (fs.existsSync(py)) return { command: py, args: [script] };
  }
  return { command: process.platform === "win32" ? "python" : "python3", args: [script] };
}

/** Where audit.db + the attachment store live. Created on demand. */
function userDataSubdir(name: string): string {
  const p = path.join(app.getPath("userData"), name);
  fs.mkdirSync(p, { recursive: true });
  return p;
}

// ── Snip: the composer's built-in screenshot (2026-09-26) ───────────────────
/**
 * Flow: hotkey/button -> freeze the screen to a bitmap -> full-screen overlay on
 * the cursor's display (rendering that frozen bitmap) -> drag a region -> preview
 * window with rectangle / arrow / mosaic annotation -> confirm -> the flattened
 * PNG lands in the composer as an attachment.
 *
 * The shot is taken BEFORE the overlay is shown: the overlay's own dim veil can
 * never leak into the image, and what the user selects is exactly what was on
 * screen when they asked for a screenshot.
 *
 * The overlay page keeps the frozen bitmap and does its own coordinate work
 * (window CSS px -> native bitmap px) when it bakes the crop, so main only has
 * to know which window is live.
 */
let snipOverlay: BrowserWindow | null = null;
/** Desktop hub 浮层（Ctrl+Alt+H）。它只做随手问一件事，不接管会话/模型管理。 */
let hubWindow: BrowserWindow | null = null;
let hubSessionId = "";
let hubModelName = "";
let hubLastChatId = "";
/** 截图结果回哪个窗：主窗（Ctrl+Shift+S）还是 hub。 */
let snipTarget: "main" | "hub" = "main";
/** 截图收尾后的一小段窗口期：这期间 hub 即使收到 blur 也不自藏。
 *  原因：销毁截图浮层时系统会把焦点交给别的窗，那次 blur 在我们 show()+focus() 之后才到，
 *  而此刻 snipOverlay 已置空 —— 老逻辑会顺手把 hub 藏掉，用户看到「截完 hub 没了」。 */
let hubKeepAliveUntil = 0;

/** 主窗当前开着的会话。hub 默认接这条走：同一份上下文、同一个模型和设置。 */
let currentSessionId = "";
const HUB_WIDTH = 504;   // 420 -> 504：用户 2026-09-28 要求再长 20%
const HUB_KEY = "Control+Alt+H";
let snipBusy = false;

/**
 * The one place that turns a base64 image data URL into a temp file.
 * agent:saveTempImage and the snip flow share it, so the attachment pipeline
 * downstream (thumbnail, sidecar store, model input) stays single-track.
 */
function saveImageDataUrl(raw: unknown, name: unknown, fallbackName: string): string {
  const m = /^data:image\/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$/i.exec(String(raw ?? ""));
  if (!m) throw new Error("saveTempImage: expected a base64 image data URL");
  const ext = m[1].toLowerCase() === "jpeg" ? "jpg" : m[1].toLowerCase();
  const buf = Buffer.from(m[2].replace(/\s+/g, ""), "base64");
  if (buf.length === 0) throw new Error("saveTempImage: empty image");
  if (buf.length > 32 * 1024 * 1024) throw new Error("saveTempImage: image too large (32MB cap)");
  const dir = userDataSubdir("temp");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  // 名字里已经带了图片后缀就别再加一遍（否则出现 shot.png.png）
  const hint = String(name ?? "")
    .replace(/\.(png|jpe?g|webp|gif)$/i, "")
    .replace(/[^\w.-]+/g, "")
    .slice(0, 40);
  const file = path.join(dir, `${stamp}-${hint || fallbackName}.${ext}`);
  fs.writeFileSync(file, buf);
  return file;
}

function endSnip(): void {
  // 谁发起的截图，收尾就把谁放回来 —— 之前无条件 show+focus 主窗，
  // 于是 hub 发起的截图一确认，焦点被主窗抢走、hub 失焦自藏，用户看到「截完 hub 没了」。
  const fromHub = snipTarget === "hub";
  snipTarget = "main";
  if (fromHub) hubKeepAliveUntil = Date.now() + 1500;
  for (const w of [snipOverlay]) {
    if (w && !w.isDestroyed()) w.destroy();
  }
  snipOverlay = null;
  snipBusy = false;
  const back = fromHub && hubWindow && !hubWindow.isDestroyed() ? hubWindow : mainWindow;
  if (back && !back.isDestroyed()) {
    back.show();
    back.focus();
  }
}

async function startSnip(): Promise<void> {
  if (snipBusy) return; // one session at a time
  snipBusy = true;
  try {
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const scale = display.scaleFactor || 1;
    const sources = await desktopCapturer.getSources({
      types: ["screen"],
      thumbnailSize: {
        width: Math.max(1, Math.round(display.bounds.width * scale)),
        height: Math.max(1, Math.round(display.bounds.height * scale)),
      },
    });
    const wanted = String(display.id);
    const source = sources.find((s) => s.display_id === wanted) ?? sources[0];
    if (!source || source.thumbnail.isEmpty()) {
      console.error("[main] snip: no usable screen source");
      endSnip();
      return;
    }
    const fullPath = saveImageDataUrl(
      "data:image/png;base64," + source.thumbnail.toPNG().toString("base64"),
      "snip-full",
      "snip-full"
    );
    const b = display.bounds;
    const size = source.thumbnail.getSize();
    snipOverlay = new BrowserWindow({
      x: b.x,
      y: b.y,
      width: b.width,
      height: b.height,
      frame: false,
      // Windows clamps a frameless window to the work area (1080 - 40 taskbar),
      // which used to squash the frozen frame. kiosk covers display.bounds whole.
      kiosk: true,
      backgroundColor: "#000000",
      alwaysOnTop: true,
      skipTaskbar: true,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      hasShadow: false,
      show: false,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
        additionalArguments: [`--snip-image=${fullPath}`, `--snip-size=${size.width}x${size.height}`],
        preload: path.join(__dirname, "snip-preload.js"),
      },
    });
    snipOverlay.setAlwaysOnTop(true, "screen-saver");
    void snipOverlay.loadFile(path.join(projectRoot(), "electron", "snip-overlay.html"));
    snipOverlay.once("ready-to-show", () => {
      snipOverlay?.show();
      snipOverlay?.focus();
    });
    snipOverlay.on("closed", () => {
      snipOverlay = null;
    });
  } catch (e) {
    console.error("[main] snip: start failed:", e);
    endSnip();
  }
}

function registerSnipIpc(): void {
  ipcMain.handle("snip:start", async () => {
    snipTarget = "main";
    await startSnip();
    return true;
  });

  // Esc, right click or the toolbar's cancel button: close without attaching.
  ipcMain.on("snip:cancel", (e) => {
    if (!snipOverlay || snipOverlay.isDestroyed() || e.sender !== snipOverlay.webContents) return;
    endSnip();
  });

  // The overlay reports a single result: the annotated crop as a PNG data URL.
  // It is baked from the frozen bitmap in the page, so what the user framed is
  // exactly what lands in the composer.
  ipcMain.on("snip:confirm", (e, payload) => {
    if (!snipOverlay || snipOverlay.isDestroyed() || e.sender !== snipOverlay.webContents) return;
    const fromHub = snipTarget === "hub";
    if (typeof payload !== "string" || !payload.startsWith("data:image/png;base64,")) {
      endSnip();
      return;
    }
    let file = "";
    try {
      file = saveImageDataUrl(payload, "snip", "snip");
    } catch (err) {
      console.error("[main] snip: could not save the annotated image:", err);
    }
    endSnip();
    if (!file) return;
    // 谁发起的截图，结果就回谁那儿（hub 里的截图按钮 → 回 hub）。
    const target = fromHub && hubWindow && !hubWindow.isDestroyed() ? hubWindow : mainWindow;
    if (target && !target.isDestroyed()) {
      target.webContents.send("snip:attached", file);
      if (fromHub) {
        target.show();
        target.focus();
        if (hubWindow && !hubWindow.isDestroyed()) hubWindow.webContents.send("hub:shown");
      }
    }
  });
}

/**
 * Desktop hub（2026-09-28）：Ctrl+Alt+H 唤起的「随手问」浮层。
 * 它能做的事就一件 —— 把眼前这个界面/文件问出去：框选截图或挂文件，提问，就地看答案。
 * 刻意不做完整桌面才有的那些（会话列表/模型切换/历史），也不碰关闭逻辑。
 * 界面与行为口径见 vault：文件与截图两个图标键与输入框同行、仅图标。
 */
/**
 * 最小化到托盘：窗口 hide 掉（不是 minimize），托盘留个图标能点回来。
 * 图标按需建、回到桌面就销毁 —— 托盘里那个图标的意思就是「应用现在收在这里」。
 */
let tray: Tray | null = null;

function trayIconPath(): string {
  const ico = path.join(projectRoot(), "resources", "icon.ico");
  return fs.existsSync(ico) ? ico : path.join(projectRoot(), "resources", "icon.png");
}

function restoreMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  dropTray();
}

function dropTray(): void {
  if (tray && !tray.isDestroyed()) tray.destroy();
  tray = null;
}

function ensureTray(): void {
  if (tray && !tray.isDestroyed()) return;
  try {
    tray = new Tray(trayIconPath());
  } catch (err) {
    console.warn("[main] tray: icon could not be loaded, staying on the taskbar", err);
    return;
  }
  tray.setToolTip("CodeAgent");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开 CodeAgent", click: () => restoreMainWindow() },
    { label: "随手问（Ctrl+Alt+H）", click: () => toggleHub() },
    { type: "separator" },
    {
      label: "退出",
      click: () => {
        restoreMainWindow(); // 退出前先露个脸，运行中的任务确认框才有地方弹
        app.quit();
      },
    },
  ]));
  tray.on("click", () => restoreMainWindow());
}

/** 收到托盘后应用还在跑：快捷键、后台任务都不受影响，只是不进任务栏。 */
function hideToTray(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.hide();
  ensureTray();
}

function hubPosition(): { x: number; y: number } {
  const wa = screen.getPrimaryDisplay().workArea;
  return {
    x: wa.x + Math.round((wa.width - HUB_WIDTH) / 2),
    y: wa.y + Math.round(wa.height * 0.18),
  };
}

function createHubWindow(): BrowserWindow {
  const { x, y } = hubPosition();
  const win = new BrowserWindow({
    x, y, width: HUB_WIDTH, height: 62,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    hasShadow: false,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "hub-preload.js"),
    },
  });
  win.setAlwaysOnTop(true, "screen-saver");
  void win.loadFile(path.join(projectRoot(), "electron", "hub.html"));
  // 失焦即藏，像 Spotlight 一样点别处就走开。截图浮层开着的时候不藏
  // —— 那会儿焦点在浮层上，藏了用户框完就找不着 hub 了。
  win.on("blur", () => {
    if (snipOverlay && !snipOverlay.isDestroyed()) return;
    if (Date.now() < hubKeepAliveUntil) {
      if (!win.isVisible()) win.show();
      win.focus();
      return;
    }
    if (win.isVisible()) win.hide();
  });
  win.on("closed", () => { hubWindow = null; });
  return win;
}

function toggleHub(): void {
  if (!hubWindow || hubWindow.isDestroyed()) hubWindow = createHubWindow();
  const win = hubWindow;
  if (win.isVisible()) { win.hide(); return; }
  const { x, y } = hubPosition();
  win.setPosition(x, y, false);
  // 应用在后台（或已收进托盘）时，光 show() 拿不到前台：前台锁只认当前活动进程，
  // 用户按了 Ctrl+Alt+H 会看着像没反应。先偷一次焦点，再把窗抬到最前。
  app.focus({ steal: true });
  win.show();
  win.moveTop();
  win.focus();
  win.webContents.send("hub:shown");
}

/** hub 的提问落一条专用会话「随手问」，不打断正在聊的那个。找不到就建。 */
async function ensureHubSession(): Promise<string> {
  // hub 默认接着当前对话走 —— 上下文、模型、所有设置都跟主窗一致，
  // 答完落在同一条会话里，回完整端直接能看见。只有还没有当前对话时，
  // 才退回原来那条「随手问」。
  if (currentSessionId) return currentSessionId;
  if (hubSessionId) return hubSessionId;
  const c = requireClient();
  try {
    const list = (await c.request("sessions.list", {})) as { sessions?: Array<Record<string, unknown>> };
    const hit = (list?.sessions ?? []).find((r) => String(r.title ?? "") === "随手问");
    if (hit && typeof hit.id === "string") { hubSessionId = hit.id; return hubSessionId; }
  } catch { /* 列表拿不到就直接新建 */ }
  try {
    const created = (await c.request("sessions.create", { title: "随手问" })) as { id?: string };
    hubSessionId = String(created?.id ?? "");
  } catch (e) {
    console.error("[main] hub: could not ensure the session:", e);
  }
  return hubSessionId || "default";
}

async function hubModelLabel(): Promise<string> {
  if (hubModelName) return hubModelName;
  try {
    const list = (await requireClient().request("models.list", {})) as { models?: Array<Record<string, unknown>> };
    const rows = list?.models ?? [];
    const pick = rows.find((m) => m.isDefault === true || m.is_default === true) ?? rows[0];
    hubModelName = pick ? String(pick.name ?? pick.id ?? "") : "";
  } catch { hubModelName = ""; }
  return hubModelName;
}

function registerHubIpc(): void {
  ipcMain.handle("hub:ask", async (_e, payload?: { text?: string; images?: string[] }) => {
    const text = String(payload?.text ?? "");
    const images = Array.isArray(payload?.images)
      ? payload.images.filter((p: unknown): p is string => typeof p === "string" && !!p)
      : [];
    if (!text && images.length === 0) throw new Error("hub: nothing to ask");
    const sessionId = await ensureHubSession();
    const chatId = randomUUID();
    hubLastChatId = chatId;
    const ack = await requireClient().request("chat", {
      sessionId,
      messages: [{ role: "user", content: text }],
      images,
      // 挂上来的 xlsx/txt 得靠工具读；光看图看不进表格里的数。
      useTools: true,
      workspaceRoot: userDataSubdir("workspace"),
    }, { id: chatId });
    return { ...(ack as Record<string, unknown>), chatId, sessionId, model: await hubModelLabel() };
  });

  ipcMain.handle("hub:cancel", async () => {
    if (!hubLastChatId) return { cancelled: false };
    return requireClient().request("chat.cancel", { id: hubLastChatId });
  });

  ipcMain.handle("hub:snip", async () => {
    snipTarget = "hub";
    await startSnip();
    return true;
  });

  // 渲染层每次切会话都报一声，hub 就用这条。
  ipcMain.on("session:active", (_e, id?: unknown) => {
    currentSessionId = typeof id === "string" ? id : "";
  });

  ipcMain.on("hub:hide", () => {
    if (hubWindow && !hubWindow.isDestroyed()) hubWindow.hide();
  });

  // 内容自己量高度发上来：收起态一行、出答案就往下长，宽不变。
  ipcMain.on("hub:resize", (_e, height?: unknown) => {
    if (!hubWindow || hubWindow.isDestroyed()) return;
    const h = Math.max(58, Math.min(420, Math.round(Number(height) || 62)));
    hubWindow.setContentSize(HUB_WIDTH, h, false);
  });

  ipcMain.on("hub:copy", (_e, text?: unknown) => {
    if (typeof text === "string" && text) clipboard.writeText(text);
  });

  ipcMain.on("hub:openInMain", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.show();
    mainWindow.focus();
    const sid = currentSessionId || hubSessionId;
    if (sid) {
      mainWindow.webContents.send("hub:openSession", sid);
      mainWindow.webContents.send("session:refresh", sid);
    }
  });

  // 主题先跟系统深浅走。跟 app 里选的主题完全一致要等渲染层把变量递出来，那是下一步。
  ipcMain.handle("hub:theme", () => (nativeTheme.shouldUseDarkColors ? "dark" : "light"));
}

function registerHubShortcut(): void {
  // 占不到就只记一条日志：这个键是用户点的名，偷偷换成别的他更找不着。
  const ok = globalShortcut.register(HUB_KEY, () => toggleHub());
  if (!ok) console.warn(`[main] hub: ${HUB_KEY} 被别的程序占用，hub 唤起不可用`);
}

function createWindow(): void {
  // Windows 任务栏按 AppUserModelId 分组并按它取图标；不显式设置的话
  // dev 模式会沿用 electron.exe 的标识，自定义图标只在一部分位置生效。
  if (process.platform === "win32") app.setAppUserModelId("com.codeagent.desktop");
  // No File/Edit/View bar. A null application menu on win32, paired with
  // autoHideMenuBar below, ships a chrome-free window; F12 / Ctrl+Shift+I
  // still reach devtools.
  if (process.platform !== "darwin") Menu.setApplicationMenu(null);
  // The UI is dark only, so let the OS paint dark chrome too: on Windows 10 the
  // immersive dark title bar follows this, which beats a white strip on top of
  // a #0f0f10 app (the usual choice for dark-only Electron apps).
  nativeTheme.themeSource = "dark";
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 880,
    minHeight: 560,
    autoHideMenuBar: true, // belt and braces: Alt must not reveal a bar either
    // Frameless on Windows. The native title bar sat *above* the app's own
    // TopBar, so the window showed two stacked bars. The usual fix is
    // titleBarStyle "hidden" + titleBarOverlay — but that overlay is a
    // Windows 11 feature and this target is Windows 10, so we drop the frame
    // entirely and let the TopBar draw its own window buttons.
    // thickFrame keeps the native edge/corner resize behaviour.
    frame: process.platform !== "win32",
    thickFrame: true,
    backgroundColor: "#0f0f10", // no white flash before the page paints
    webPreferences: {
      contextIsolation: true, // R6
      nodeIntegration: false, // R6
      sandbox: true, // R6
      preload: path.join(__dirname, "preload.js"),
    },
    title: "CodeAgent",
    // 托盘/任务栏图标。Win32 用 .ico（多尺寸，托盘尺寸由系统挑），其他平台用 PNG。
    // projectRoot() 而不是 __dirname —— 见上面那段注释（编译产物在 dist/electron/）。
    icon: path.join(projectRoot(), "resources", process.platform === "win32" ? "icon.ico" : "icon.png"),
  });
  // Keyboard debug hatch, so removing the menu does not remove devtools.
  mainWindow.webContents.on("before-input-event", (_e, input) => {
    if (input.type !== "keyDown") return;
    const key = String(input.key || "").toLowerCase();
    if (key === "f12" || (input.control && input.shift && key === "i")) {
      mainWindow?.webContents.toggleDevTools();
    }
    // In-app screenshot hotkey. App-scoped on purpose: before-input-event only
    // fires for the focused window, and Feishu already owns Ctrl+Shift+A as a
    // *global* hotkey (its own docs), so that combination would be stolen.
    if (input.control && input.shift && key === "s") {
      void startSnip();
    }
  });
  // 消息里的链接（含引用 [1]）只让系统默认浏览器打开。窗口是 kiosk 且常驻，
  // 一旦在应用内打开，用户就回不到对话了；同时挡掉 window.open 走内的老路。
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void openExternalSafe(url);
    return { action: "deny" };
  });
  mainWindow.loadFile(path.join(projectRoot(), "electron", "ui", "index.html"));
  // Frameless window: the renderer's TopBar is the only title bar and owns the
  // buttons, so it needs to know when the maximised state flips (glyph swap).
  const pushMaximized = (): void => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("win:maximized", mainWindow.isMaximized());
    }
  };
  mainWindow.on("maximize", pushMaximized);
  mainWindow.on("unmaximize", pushMaximized);
  // 点标题栏 ✕（或 win.close()）时，窗口会自己关掉，before-quit 那会儿已经晚了 ——
  // 所以关卡要卡在窗口这一层：有任务在跑就先拦下，问用户怎么办。
  mainWindow.on("close", (event) => {
    if (quittingAllowed()) return;
    event.preventDefault();
    void handleCloseIntent(mainWindow);
  });
  // 最小化 = 收进托盘。不能用 minimize 事件里的 preventDefault：窗口已经最小化了，
  // 我们只需要再 hide 一次，restoreMainWindow 里 isMinimized() 那条会把它捞回来。
  mainWindow.on("minimize", () => hideToTray());
  mainWindow.on("closed", () => { mainWindow = null; });
}

function pushStatus(status: unknown): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send("agent:status", status);
}

function pushEvent(payload: unknown): void {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("agent:event", payload);
  // hub 也收：它按自己的 chatId 过滤，主窗那一堆事件与它无关。
  if (hubWindow && !hubWindow.isDestroyed()) hubWindow.webContents.send("agent:event", payload);
}

function requireClient(): SidecarClient {
  if (!client) throw new Error("client not initialized");
  return client;
}

async function bootstrap(): Promise<void> {
  const { command, args } = getSidecarCommand();
  client = new SidecarClient({
    transportFactory: () => new StdioTransport({
      command,
      args,
      cwd: projectRoot(),
      // The sidecar keeps its sqlite audit db + attachment store under this dir.
      env: { ...process.env, DESK_AGENT_DATA_DIR: userDataSubdir("data") },
      windowsHide: true, // R2
      spawner: defaultDeps().spawner,
    }),
    deps: defaultDeps(),
    pidFilePath: path.join(app.getPath("userData"), "sidecar.pid"),
  });

  client.onStatus(pushStatus);
  client.onEvent(({ event, data }) => {
    const chatId = typeof data.id === "string" ? data.id : undefined;
    pushEvent({ event, chatId, data });
    // hub 问完的那轮落在当前对话里：主窗正开着这条就重取一次，
    // 否则用户切回来看着像「对话没进去」。
    if (event === "chat.done" && chatId && chatId === hubLastChatId) {
      const sid = currentSessionId || hubSessionId;
      if (sid && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("session:refresh", sid);
      }
    }
  });

  registerSnipIpc();
  registerHubIpc();
  registerHubShortcut();

  ipcMain.handle("agent:status", async () => requireClient().getStatus());
  ipcMain.handle("agent:ping", async () => requireClient().request("ping"));
  ipcMain.handle("agent:info", async () => {
    // The renderer needs the workspace root to resolve relative image refs the
    // agent writes (a tool reports `gen-ok.png`, not the full path).
    const info = (await requireClient().request("info")) as Record<string, unknown>;
    return { ...info, workspaceRoot: userDataSubdir("workspace") };
  });
  ipcMain.handle("agent:tools", async () => requireClient().request("tools.list"));
  // Skills + the tool on/off switches (settings page). toolToggle writes the
  // sidecar-side config (capabilities.toolsDisabled) and resolves with the
  // refreshed tools payload, so the UI can update its list in one round-trip.
  ipcMain.handle("agent:skills", async () => requireClient().request("skills.list"));
  ipcMain.handle("agent:toolToggle", async (_e, input?: Record<string, unknown>) =>
    requireClient().request("tools.setEnabled", input ?? {}));

  // Model registry lives in the sidecar (config.json + env keys). The renderer
  // never sees a key value, only whether the env var is set.
  /**
   * 剪贴板里的截图没有磁盘路径（Electron 的 webUtils.getPathForFile 对剪贴板位图返回空），
   * 而附件管线只认「路径」这一种形态。所以渲染层把位图发过来，这里落成临时文件再把路径还回去
   * —— 之后缩略图、sidecar 存储、喂给模型全走原来那条路，不需要第二套逻辑。
   * 只接受 base64 图片 data URL，只写进 userData/temp，带大小上限，不接受任意路径。
   */
  ipcMain.handle("agent:saveTempImage", async (_e, payload?: { dataUrl?: string; name?: string }) =>
    saveImageDataUrl(payload?.dataUrl, payload?.name, "clipboard")
  );

  /**
   * 用系统默认浏览器打开外链。只放行 http(s)：别把 file://、自定义协议递给系统，
   * 那是本机任意程序启动面的入口。渲染层拿到的返回值只表示「已交给系统」。
   */
  ipcMain.handle("agent:openExternal", async (_e, url?: unknown) => openExternalSafe(url));

  /**
   * 打开对话里附件的本地文件 / 在资源管理器里定位它（2026-09-27）。
   * 只做「用系统默认程序打开」和「定位文件」，渲染进程依旧拿不到任意写文件的能力。
   */
  ipcMain.handle("agent:openPath", async (_e, target?: unknown) => {
    if (typeof target !== "string" || !target.trim()) return { ok: false, reason: "bad-path" };
    const err = await shell.openPath(target);
    return err ? { ok: false, reason: err } : { ok: true };
  });
  ipcMain.handle("agent:showItemInFolder", async (_e, target?: unknown) => {
    if (typeof target !== "string" || !target.trim()) return { ok: false, reason: "bad-path" };
    shell.showItemInFolder(target);
    return { ok: true };
  });

  /**
   * 图表另存为：只接受 data: URL，弹系统保存框之后才写盘。
   * 渲染进程因此永远拿不到「往任意路径写文件」的能力。
   */
  ipcMain.handle("agent:saveFile", async (_event, name?: unknown, dataUrl?: unknown) => {
    const suggested = typeof name === "string" && name.trim() ? path.basename(name.trim()) : "download";
    const raw = typeof dataUrl === "string" ? dataUrl : "";
    const match = /^data:([^;,]*)(;base64)?,([\s\S]*)$/.exec(raw);
    if (!match) return { ok: false, reason: "bad-data-url" };
    const buf = Buffer.from(match[3], match[2] ? "base64" : "utf8");
    if (buf.byteLength > 32 * 1024 * 1024) return { ok: false, reason: "too-large" };
    const win = BrowserWindow.getFocusedWindow() ?? mainWindow;
    const res = win
      ? await dialog.showSaveDialog(win, { defaultPath: suggested })
      : await dialog.showSaveDialog({ defaultPath: suggested });
    if (res.canceled || !res.filePath) return { ok: false, reason: "canceled" };
    await fs.promises.writeFile(res.filePath, buf);
    return { ok: true, path: res.filePath };
  });

  ipcMain.handle("agent:models", async () => requireClient().request("models.list"));
  ipcMain.handle("agent:modelPresets", async () => requireClient().request("models.presets"));
  // Vendor model catalogue (GET /models). The user should pick a model, not
  // type one from memory; this is a network round-trip so it can take seconds.
  ipcMain.handle("agent:modelFetch", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("models.fetch", payload ?? {}));
  ipcMain.handle("agent:modelUpsert", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("models.upsert", payload));
  ipcMain.handle("agent:modelRemove", async (_e, id?: string) =>
    requireClient().request("models.remove", { id }));
  ipcMain.handle("agent:modelSetDefault", async (_e, id?: string) =>
    requireClient().request("models.setDefault", { id }));

  // 视觉线路（capabilities.vision）：会话模型不吃图时，图片先交给它读成文字再进
  // 上下文。2026-09-28 用户要求这件事能在设置里改，不再手改 config.json。
  ipcMain.handle("agent:vision", async () => requireClient().request("vision.get"));
  ipcMain.handle("agent:visionSet", async (_e, id?: string) =>
    requireClient().request("vision.set", { modelId: id }));

  // Session store (sessions.db, owned by the sidecar). The renderer keeps its
  // in-memory message list; these channels are how the list and the history
  // survive a restart, and messages.search is the FTS5 index over them.
  ipcMain.handle("agent:sessions", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("sessions.list", payload));
  ipcMain.handle("agent:sessionCreate", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("sessions.create", payload));
  ipcMain.handle("agent:sessionRename", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("sessions.rename", payload));
  ipcMain.handle("agent:sessionDelete", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("sessions.delete", payload));
  ipcMain.handle("agent:sessionMessages", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("sessions.messages", payload));
  ipcMain.handle("agent:messagesSearch", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("messages.search", payload));

  // Usage aggregation. Both the bottom status bar and the 用量统计 page read
  // this one method, so the two can never disagree about the same tokens.
  // Money is computed sidecar-side from the preset catalog (token plans get
  // no number on purpose).
  // 运行断点（2026-09-25）：会话里有没有可续的任务、现在有没有在跑、中断/忽略。
  ipcMain.handle("agent:runPending", async (_e, sessionId?: string) =>
    requireClient().request("run.pending", { sessionId }));
  ipcMain.handle("agent:runActive", async () => requireClient().request("run.active", {}));
  ipcMain.handle("agent:runInterrupt", async (
    _e, payload?: { runId?: string; reason?: string }) =>
    requireClient().request("run.interrupt", payload ?? {}));
  ipcMain.handle("agent:runDismiss", async (_e, runId?: string) =>
    requireClient().request("run.dismiss", { runId }));

  ipcMain.handle("agent:usage", async (_e, payload?: Record<string, unknown>) =>
    requireClient().request("usage.summary", payload));

  // Window buttons for the frameless TopBar (minimise / maximise / close).
  ipcMain.handle("win:action", (e, action?: string) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!w || w.isDestroyed()) return false;
    if (action === "minimize") { if (w === mainWindow) hideToTray(); else w.minimize(); }
    else if (action === "maximize") {
      if (w.isMaximized()) w.unmaximize();
      else w.maximize();
    } else if (action === "close") w.close();
    return w.isMaximized();
  });
  ipcMain.handle("win:is-maximized", (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    return !!w && !w.isDestroyed() && w.isMaximized();
  });

  ipcMain.handle("agent:chat", async (_e, params?: Record<string, unknown>) => {
    const c = requireClient();
    const chatId = randomUUID();
    const request = { ...(params ?? {}) } as Record<string, unknown>;
    if (!request.workspaceRoot) request.workspaceRoot = userDataSubdir("workspace");
    if (!request.sessionId) request.sessionId = "default";
    const ack = await c.request("chat", request, { id: chatId });
    return { ...ack, chatId };
  });

  ipcMain.handle("agent:cancel", async (_e, chatId?: string) => {
    if (typeof chatId !== "string" || !chatId) throw new Error("chatId is required");
    return requireClient().request("chat.cancel", { id: chatId });
  });

  ipcMain.handle("agent:approval", async (_e, payload?: { approvalId?: string; decision?: string }) => {
    const approvalId = payload?.approvalId;
    const decision = payload?.decision;
    if (typeof approvalId !== "string" || !approvalId) throw new Error("approvalId is required");
    return requireClient().request("approval.respond", { approvalId, decision });
  });

  ipcMain.handle("agent:pickImages", async () => {
    if (!mainWindow) return [];
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择图片或文档",
      properties: ["openFile", "multiSelections"],
      filters: [
        {
          name: "图片与文档",
          extensions: [
            "png", "jpg", "jpeg", "gif", "webp",
            "pdf", "doc", "docx", "docm", "ppt", "pptx", "pptm",
            "pps", "ppsx", "xls", "xlsx", "xlsm", "xlsb",
            "odt", "ods", "odp", "rtf", "epub", "ipynb",
          ],
        },
        { name: "全部文件", extensions: ["*"] },
      ],
    });
    return result.canceled ? [] : result.filePaths;
  });

  // Start sidecar in background; UI shows "starting" meanwhile.
  client.start().catch((e) => {
    console.error("[main] sidecar start failed:", e);
  });
}

// Single-instance lock (2026-09-26): a second launch must never create a
// window or bootstrap a second sidecar — two sidecars would race on the same
// sessions.db / breakpoint store under <userData>/data. When the lock is
// denied we quit immediately; the primary instance receives
// "second-instance" and surfaces its existing window instead.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    // Bring the existing window forward, even out of a minimized state.
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    createWindow();
    bootstrap();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// ── 关程序时的交互（2026-09-25 定稿）──────────────────────────────────────
// 有任务在跑就别闷声退：问用户「等它做完 / 中断并保存断点 / 取消」。
// 用户选择走应用内弹框（可见、可点、也能被自动化验收）；渲染进程不吭声
// （卡死/没界面）就按默认「中断并保存断点」退出 —— 绝不因为弹框没答上
// 就把用户卡在关不掉的程序里。
const CLOSE_ANSWER_TIMEOUT_MS = 4000;
let quitAllowed = false;
//: 同时在问的时候不要问两遍（窗口 close 与 before-quit 可能前后脚触发）。
let askingInFlight: Promise<void> | null = null;

function quittingAllowed(): boolean {
  return quitAllowed;
}

async function interruptAll(runs: Array<Record<string, unknown>>): Promise<void> {
  for (const run of runs) {
    try {
      await requireClient().request("run.interrupt",
        { runId: run.run_id, reason: "应用关闭" });
    } catch (err) {
      console.error("[main] interrupt on quit failed:", err);
    }
  }
}

/** 统一入口：有任务在跑就问一句，按用户的答复决定退/等/取消。 */
async function handleCloseIntent(win: BrowserWindow | null): Promise<void> {
  if (askingInFlight) return askingInFlight;
  const task = (async () => {
    const runs = await activeRuns();
    if (runs.length === 0) {
      quitAllowed = true;
      app.quit();
      return;
    }
    const choice = await askCloseChoice(runs, win);
    if (choice === "cancel") return;
    if (choice === "wait") {
      scheduleWaitThenQuit();
      return;
    }
    await interruptAll(runs);
    quitAllowed = true;
    if (win && !win.isDestroyed()) win.destroy();
    app.quit();
  })();
  askingInFlight = task.finally(() => { askingInFlight = null; }) as Promise<void>;
  return askingInFlight;
}
let quitAnswer: ((choice: string) => void) | null = null;
let waitTimer: NodeJS.Timeout | null = null;

function runList(payload: unknown): Array<Record<string, unknown>> {
  const runs = (payload as { runs?: unknown } | null)?.runs;
  return Array.isArray(runs) ? (runs as Array<Record<string, unknown>>) : [];
}

async function activeRuns(): Promise<Array<Record<string, unknown>>> {
  try {
    return runList(await requireClient().request("run.active", {}));
  } catch {
    return [];
  }
}

function askCloseChoice(
  runs: Array<Record<string, unknown>>,
  target?: BrowserWindow | null
): Promise<string> {
  const win = target ?? BrowserWindow.getAllWindows()[0];
  if (!win || win.isDestroyed()) return Promise.resolve("interrupt");
  return new Promise<string>((resolve) => {
    let done = false;
    const finish = (choice: string) => {
      if (done) return;
      done = true;
      quitAnswer = null;
      resolve(choice);
    };
    quitAnswer = finish;
    win.webContents.send("close:confirm", { runs: runs.map(r => ({
      run_id: r.run_id, session_id: r.session_id, status: r.status,
      rounds: r.rounds, reason: r.reason, updated_at: r.updated_at
    })) });
    setTimeout(() => finish("interrupt"), CLOSE_ANSWER_TIMEOUT_MS);
  });
}

function scheduleWaitThenQuit(): void {
  if (waitTimer) return;
  waitTimer = setInterval(async () => {
    const runs = await activeRuns();
    if (runs.length === 0) {
      if (waitTimer) clearInterval(waitTimer);
      waitTimer = null;
      quitAllowed = true;
      const win = BrowserWindow.getAllWindows()[0];
      if (win && !win.isDestroyed()) win.destroy();
      app.quit();
    }
  }, 3000);
}

ipcMain.on("close:choice", (_e, choice?: string) => {
  if (quitAnswer) quitAnswer(String(choice || "interrupt"));
});

app.on("will-quit", () => globalShortcut.unregisterAll());

app.on("before-quit", async (event) => {
  if (!quitAllowed) {
    // 非窗口触发的退出（菜单/命令行/其它代码 app.quit()）也要先问。
    event.preventDefault();
    await handleCloseIntent(mainWindow);
    return;
  }
  if (client) await client.stop();
});

process.on("uncaughtException", (err) => {
  console.error("[main] uncaught:", err);
});
process.on("unhandledRejection", (err) => {
  console.error("[main] unhandled rejection:", err);
});