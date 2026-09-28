import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles/global.css';
import './styles/highlight.css';
import { applyAppSettings, loadAppSettings } from './lib/appSettings';
import { applyWallpaper } from './lib/wallpapers';

/* Apply the saved appearance before the first paint: a light-theme user must
   never see a dark flash, and we do not depend on a React effect for it. */
const initialSettings = loadAppSettings();
applyWallpaper(initialSettings.wallpaper, document.documentElement);
applyAppSettings(
  initialSettings,
  document.documentElement,
  typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : true
);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);