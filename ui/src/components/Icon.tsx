/**
 * Inline SVG icon library.
 * All icons share a 16x16 grid by default (set width/height via prop).
 * Strokes use currentColor so they inherit button/text color.
 */
import { type SVGProps } from 'react';

type Props = SVGProps<SVGSVGElement> & { size?: number };

const base = (size: number): SVGProps<SVGSVGElement> => ({
  width: size, height: size, viewBox: '0 0 16 16', fill: 'none',
  stroke: 'currentColor', strokeWidth: 1.5,
  strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const
});

export const Icon = {
  Plus: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M8 3v10M3 8h10" /></svg>
  ),
  Search: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}><circle cx="7" cy="7" r="4" /><path d="M11 11l3 3" /></svg>
  ),
  /** 文件（非图片附件的占位图标）：不能画缩略图的一律用它，避免 <img> 裂图。 */
  File: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M4.75 1.5h4L12.25 5V14.5a1 1 0 0 1-1 1H4.75a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1z" />
      <path d="M8.75 1.5v3.5h3.5" />
    </svg>
  ),
  ChevronLeft: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M10.5 3L5.5 8l5 5" /></svg>
  ),
  ChevronRight: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M5.5 3l5 5-5 5" /></svg>
  ),
  ChevronDown: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M3 5.5l5 5 5-5" /></svg>
  ),
  Pin: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M9.5 1.5l5 5-2 2-1-1-3 3v3l-2 2-1-1 2-2v-3l3-3-1-1z" />
    </svg>
  ),
  Settings: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <circle cx="8" cy="8" r="2" />
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3" />
    </svg>
  ),
  ChartBar: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 13h12" />
      <rect x="3" y="8" width="2" height="4" />
      <rect x="7" y="5" width="2" height="7" />
      <rect x="11" y="3" width="2" height="9" />
    </svg>
  ),
  /** 截图：四角取景框 + 中线（输入栏截图按钮用，两轴都落在 16 栅格中心）。 */
  Shot: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 5.5V3.5A1.5 1.5 0 0 1 3.5 2h2M10.5 2h2A1.5 1.5 0 0 1 14 3.5v2M14 10.5v2a1.5 1.5 0 0 1-1.5 1.5h-2M5.5 14h-2A1.5 1.5 0 0 1 2 12.5v-2" />
      <path d="M5.5 8h5" />
    </svg>
  ),
  Send: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      {/* 路径原本 y 2..16（中心 9，比栅格中心低 1 格）→ 上移 1 格后墨迹居中 */}
      <path d="M2 7l12-6-5 14-2-6z" />
    </svg>
  ),
  Stop: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}><rect x="3" y="3" width="10" height="10" rx="1.5" /></svg>
  ),
  Copy: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <rect x="5" y="5" width="9" height="9" rx="1.5" />
      <path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H11" />
    </svg>
  ),
  Check: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M2 8l4 4 8-9" /></svg>
  ),
  X: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M3 3l10 10M13 3L3 13" /></svg>
  ),
  Folder: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h5A1.5 1.5 0 0 1 14.5 6v6A1.5 1.5 0 0 1 13 13.5H3.5A1.5 1.5 0 0 1 2 12z" />
    </svg>
  ),
  Terminal: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <rect x="2" y="2.5" width="12" height="11" rx="1.5" />
      <path d="M4.5 6.5l2.5 2-2.5 2M9 11h3" />
    </svg>
  ),
  Pencil: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 13l1-3 8-8 2 2-8 8z" />
      <path d="M10 3l2 2" />
    </svg>
  ),
  Trash: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M3 4h10M5 4V2.5h6V4M4 4l1 9h6l1-9M7 7v4M9 7v4" />
    </svg>
  ),
  Eye: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M1.5 8s2-4.5 6.5-4.5S14.5 8 14.5 8s-2 4.5-6.5 4.5S1.5 8 1.5 8z" />
      <circle cx="8" cy="8" r="2" />
    </svg>
  ),
  EyeOff: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 8s2-4.5 6-4.5c1.5 0 2.7.5 3.7 1.2M14 8s-2 4.5-6 4.5c-1.5 0-2.7-.5-3.7-1.2" />
      <path d="M2 2l12 12" />
    </svg>
  ),
  Sparkles: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M8 2l1 3 3 1-3 1-1 3-1-3-3-1 3-1zM13 9l.6 1.4 1.4.6-1.4.6L13 13l-.6-1.4L11 11l1.4-.6z" />
    </svg>
  ),
  Shield: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M8 1.5l5.5 2v4c0 4-2.5 6.5-5.5 7.5-3-1-5.5-3.5-5.5-7.5v-4z" />
    </svg>
  ),
  Warning: ({ size = 16, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M8 2l6.5 11h-13z" />
      <path d="M8 7v3" />
      <circle cx="8" cy="11.5" r="0.4" fill="currentColor" />
    </svg>
  ),
  Cog: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.5v2M8 12.5v2M14 8h-2M4 8H2M12 4l-1.5 1.5M5.5 10.5L4 12M12 12l-1.5-1.5M5.5 5.5L4 4" />
    </svg>
  ),
  Bolt: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M9 1.5L3 9h4l-1 5.5L13 7H8z" /></svg>
  ),
  Lock: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <rect x="3" y="7" width="10" height="7" rx="1.5" />
      <path d="M5 7V5a3 3 0 0 1 6 0v2" />
    </svg>
  ),
  /** Brand mark: filled robot. Path comes from the user's own icon asset
   *  (resources/robot-config-selected.svg, an iconfont export). It is a filled
   *  glyph, so it does not use the shared stroke base; colour is inherited
   *  from currentColor and set to the brand blue in TopBar.css. */
  Robot: ({ size = 18, ...p }: Props) => (
    <svg width={size} height={size} viewBox="0 0 1024 1024" fill="currentColor"
         aria-hidden="true" {...p}>
      <path d="M844.416 896.554667c21.034667 0 42.026667 17.493333 42.026667 43.690666 0 24.32-14.506667 41.130667-36.693334 43.434667l-5.290666 0.256H163.882667c-25.173333 0-42.026667-17.493333-42.026667-43.690667 0-24.32 14.506667-41.173333 36.736-43.434666l5.290667-0.256h680.533333zM504.192 0.853333c58.837333 0 105.045333 48.042667 105.045333 109.226667 0 40.96-22.186667 78.08-56.064 96.938667l-6.954666 3.541333v74.24h84.053333c151.210667 0 273.066667 126.72 273.066667 284.032 0 153.856-116.608 278.442667-263.253334 283.818667l-9.813333 0.170666H378.112c-151.253333 0-273.066667-126.72-273.066667-283.989333 0-153.856 116.565333-278.485333 263.253334-283.818667l9.813333-0.170666h84.010667V210.602667c-37.802667-17.493333-63.018667-56.789333-63.018667-100.48 0-56.789333 46.208-109.226667 105.002667-109.226667zM42.069333 442.154667c23.381333 0 39.552 15.061333 41.728 38.229333l0.256 5.461333v174.762667c0 26.24-16.810667 43.690667-41.984 43.690667-23.424 0-39.594667-15.061333-41.770666-38.186667L0.042667 660.608v-174.762667c0-26.197333 16.810667-43.690667 42.026666-43.690666z m924.202667 0c23.424 0 39.594667 15.061333 41.770667 38.229333l0.256 5.461333v174.762667c0 26.24-16.810667 43.690667-42.026667 43.690667-23.381333 0-39.552-15.061333-41.770667-38.186667l-0.213333-5.504v-174.762667c0-26.197333 16.768-43.690667 41.984-43.690666z m-609.152 39.338666c-46.208 0-84.010667 39.296-84.010667 87.381334 0 48.042667 37.802667 87.381333 84.053334 87.381333 46.165333 0 83.968-34.986667 83.968-87.381333 0-48.085333-37.802667-87.381333-84.010667-87.381334z m294.101333 0c-46.250667 0-84.053333 39.296-84.053333 87.381334 0 48.042667 37.802667 87.381333 84.053333 87.381333 46.208 0 84.010667-34.986667 84.010667-87.381333 0-48.085333-37.802667-87.381333-84.053333-87.381334z" />
    </svg>
  ),
  Message: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 3h12v8H10l-3 3v-3H2z" />
    </svg>
  ),
  Edit: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M11 2l3 3-8 8H3v-3z" />
    </svg>
  ),
  ArrowDown: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}><path d="M8 3v10M4 9l4 4 4-4" /></svg>
  ),
  Refresh: ({ size = 14, ...p }: Props) => (
    <svg {...base(size)} {...p}>
      <path d="M2 8a6 6 0 0 1 11-4M14 8a6 6 0 0 1-11 4" />
      <path d="M11 1v3h3M5 15v-3H2" />
    </svg>
  )
};

export const ToolGlyph = {
  bash: Icon.Terminal,
  read_file: Icon.Folder,
  write_file: Icon.Pencil,
  edit_file: Icon.Edit,
  grep: Icon.Search,
  default: Icon.Bolt
} as const;

export type ToolName = keyof typeof ToolGlyph;