/* Injected by vite (`define`) — see ui/vite.config.ts. */
declare const __APP_VERSION__: string;

/* 内置壁纸资源（Vite 会把它们 emit 到 dist/assets 并给出 URL） */
declare module '*.jpg' {
  const src: string;
  export default src;
}
