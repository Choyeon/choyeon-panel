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

/* naive 的按钮文字 / 标签文字 / 链接直接取这里的语义色，跟 style.css 的 --cp-*
   一样必须按主题分两套：暗色要够亮（深底 4.5:1），亮色要够深（白底 4.5:1）。
   之前两套共用 palette，实测亮色最低 1.78:1、暗色最低 3.5:1，都不达 WCAG AA。 */
const darkSemantic = {
  brand: '#6f9bff', brandHover: '#8fa8ff', brandPressed: '#4f7cff',
  ok: '#6fdba4', warn: '#f5c26f', err: '#f58a92', info: '#8fa8ff',
};
const lightSemantic = {
  brand: '#2f52c0', brandHover: '#3d66e8', brandPressed: '#2644a6',
  ok: '#146c41', warn: '#7a5006', err: '#c62f3a', info: '#2f52c0',
};

function commonFor(p: typeof darkSemantic, t: typeof darkText): GlobalThemeOverrides['common'] {
  return {
    primaryColor: p.brand,
    primaryColorHover: p.brandHover,
    primaryColorPressed: p.brandPressed,
    primaryColorSuppl: p.brand,
    infoColor: p.info,
    successColor: p.ok,
    warningColor: p.warn,
    errorColor: p.err,
    textColor1: t.text1,
    textColor2: t.text2,
    textColor3: t.text3,
    placeholderColor: t.placeholder,
    placeholderColorDisabled: t.placeholderDisabled,
    borderRadius: '8px',
    borderRadiusSmall: '6px',
    fontFamily: `'Inter','PingFang SC','HarmonyOS Sans SC','Microsoft YaHei',system-ui,sans-serif`,
  };
}

const darkText = {
  text1: '#dcdce4', text2: '#9d9da8', text3: '#8b8b95',
  placeholder: '#8b8b95', placeholderDisabled: '#5f5f68',
};
const lightText = {
  text1: '#1f2430', text2: '#5a6478', text3: '#5f6a7d',
  placeholder: '#5f6a7d', placeholderDisabled: '#8a92a4',
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

export const darkOverrides: GlobalThemeOverrides = {
  common: commonFor(darkSemantic, darkText),
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
  common: { ...commonFor(lightSemantic, lightText), bodyColor: surfaces.light.bg },
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
