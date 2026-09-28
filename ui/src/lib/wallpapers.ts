/**
 * Wallpaper assets + the one place that writes them onto the document.
 *
 * Kept out of `appSettings.ts` on purpose: that module is pure logic and is
 * imported by node tests, which cannot import a `.jpg`.
 */
import auroraUrl from '../assets/wallpapers/aurora.jpg';
import deepseaUrl from '../assets/wallpapers/deepsea.jpg';
import snowUrl from '../assets/wallpapers/snow.jpg';
import type { WallpaperId } from './appSettings';

export const WALLPAPER_URLS: Record<WallpaperId, string> = {
  halo: '',
  aurora: auroraUrl,
  deepsea: deepseaUrl,
  snow: snowUrl
};

/** Reflects the chosen wallpaper onto a root element (css var + data attr). */
export function applyWallpaper(id: WallpaperId, root: HTMLElement): void {
  const url = WALLPAPER_URLS[id] ?? '';
  if (url) {
    root.style.setProperty('--wallpaper', `url("${url}")`);
  } else {
    root.style.removeProperty('--wallpaper');
  }
  root.dataset.wallpaper = id;
}
