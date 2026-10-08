/* ===========================================================
   naive-ui themeOverrides 单一来源：与 style.css 的 CSS 变量对齐。
   改品牌/语义色时只改这里 + style.css，不要在组件里再写字面量。
   =========================================================== */
import { type GlobalThemeOverrides } from 'naive-ui';

export const palette = {
  brand: '#4F7CFF',
  brandHover: '#6B92FF',
  brandPressed: '#3D66E8',
  brandAccent: '#7F5BFF',
  brandSoft: '#8FA8FF',
  ok: '#34C77B',
  warn: '#F5A623',
  err: '#F5616C',
  info: '#5B8DEF',
};

const surfaces = {
  dark: {
    bg: '#101014',
    bg1: '#141419',
    elevated: '#16161C',
    cardBorder: 'rgba(255,255,255,0.07)',
    border: 'rgba(255,255,255,0.06)',
    tableBorder: 'rgba(255,255,255,0.06)',
    thColor: 'rgba(255,255,255,0.035)',
    inputColor: 'rgba(255,255,255,0.045)',
    shadowCard: '0 1px 3px rgba(0,0,0,0.3)',
  },
  light: {
    bg: '#f5f6fa',
    bg1: '#ffffff',
    elevated: '#ffffff',
    cardBorder: '#e3e6ef',
    border: '#e3e6ef',
    tableBorder: '#eceef6',
    thColor: '#f7f8fc',
    inputColor: '#ffffff',
    shadowCard: '0 1px 2px rgba(15,23,42,0.06)',
  },
};

const common: GlobalThemeOverrides['common'] = {
  primaryColor: palette.brand,
  primaryColorHover: palette.brandHover,
  primaryColorPressed: palette.brandPressed,
  primaryColorSuppl: palette.brand,
  infoColor: palette.info,
  successColor: palette.ok,
  warningColor: palette.warn,
  errorColor: palette.err,
  borderRadius: '8px',
  borderRadiusSmall: '6px',
  fontFamily: `'Inter','PingFang SC','HarmonyOS Sans SC','Microsoft YaHei',system-ui,sans-serif`,
};

export const darkOverrides: GlobalThemeOverrides = {
  common,
  Card: { borderRadius: '12px', borderColor: surfaces.dark.cardBorder, color: surfaces.dark.elevated, boxShadow: surfaces.dark.shadowCard },
  DataTable: { thColor: surfaces.dark.thColor, thFontWeight: '600', borderColor: surfaces.dark.tableBorder, tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '5px' },
  Button: { fontWeight: '500' },
  Input: { color: surfaces.dark.inputColor },
  Layout: {
    color: surfaces.dark.bg,
    siderColor: surfaces.dark.elevated,
    siderBorderColor: surfaces.dark.border,
    headerColor: surfaces.dark.bg1,
    headerBorderColor: surfaces.dark.border,
  },
  Menu: { itemHeight: '40px', itemTextColor: 'inherit' },
};

export const lightOverrides: GlobalThemeOverrides = {
  common: { ...common, bodyColor: surfaces.light.bg },
  Card: { borderRadius: '12px', borderColor: surfaces.light.cardBorder, color: surfaces.light.elevated, boxShadow: surfaces.light.shadowCard },
  DataTable: { thColor: surfaces.light.thColor, thFontWeight: '600', borderColor: surfaces.light.tableBorder, tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '5px' },
  Button: { fontWeight: '500' },
  Input: { color: surfaces.light.inputColor },
  Layout: {
    color: surfaces.light.bg,
    siderColor: surfaces.light.elevated,
    siderBorderColor: surfaces.light.border,
    headerColor: surfaces.light.bg1,
    headerBorderColor: surfaces.light.border,
  },
  Menu: { itemHeight: '40px', itemTextColor: 'inherit' },
};

/** 运行时读取当前主题的 CSS 变量（供 canvas / xterm 等 JS 侧配色使用） */
export function cssVar(name: string, fallback = ''): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
