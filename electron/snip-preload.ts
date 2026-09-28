/**
 * Preload for the snip overlay: one full-screen window that frames the region,
 * annotates it in place and bakes the crop on confirm.
 *
 * `sandbox: false` is required here: the frozen bitmap arrives as a temp file
 * path (never on argv as a data URL -- a 32 MB argv entry is a bad idea), so
 * the preload reads that file and hands the page a data URL.
 */
import { contextBridge, ipcRenderer } from "electron";
import * as fs from "fs";

export interface SnipPayload {
  imageUrl: string;
  naturalWidth: number;
  naturalHeight: number;
}

/** `--snip-image=<path>` plus `--snip-size=<w>x<h>`; both optional. */
function readPayload(): SnipPayload {
  const argv = process.argv;
  const fileArg = argv.find(a => a.startsWith("--snip-image="));
  const sizeArg = argv.find(a => a.startsWith("--snip-size="));
  const file = fileArg ? fileArg.slice("--snip-image=".length) : "";
  let naturalWidth = 0;
  let naturalHeight = 0;
  if (sizeArg) {
    const m = /^(\d+)x(\d+)$/.exec(sizeArg.slice("--snip-size=".length));
    if (m) {
      naturalWidth = Number(m[1]);
      naturalHeight = Number(m[2]);
    }
  }
  let imageUrl = "";
  try {
    if (file && fs.existsSync(file)) {
      imageUrl = "data:image/png;base64," + fs.readFileSync(file).toString("base64");
    }
  } catch {
    imageUrl = ""; // the page still renders, just without the bitmap
  }
  return { imageUrl, naturalWidth, naturalHeight };
}

const snip = {
  /** The annotated crop, baked from the frozen bitmap, as a PNG data URL. */
  confirm(dataUrl: string): void {
    ipcRenderer.send("snip:confirm", dataUrl);
  },
  cancel(): void {
    ipcRenderer.send("snip:cancel");
  },
};

contextBridge.exposeInMainWorld("__snipIpc", snip);
contextBridge.exposeInMainWorld("__snipPayload", readPayload());