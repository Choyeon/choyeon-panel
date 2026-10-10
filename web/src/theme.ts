/* ===========================================================
   naive-ui themeOverrides 单一来源：与 style.css 的 CSS 变量对齐。
   改品牌/语义色时只改这里 + style.css，不要在组件里再写字面量。
   =========================================================== */
import { type GlobalThemeOverrides } from 'naive-ui';

export const palette = {
  brand: '#005EEB',
  brandHover: '#4c8ef1',
  brandPressed: '#196eed',
  brandAccent: '#7F5BFF',
  brandSoft: '#7faef5',
  ok: '#34C77B',
  warn: '#F5A623',
  err: '#F5616C',
  info: '#5B8DEF',
};

/* naive 的按钮文字 / 标签文字 / 链接直接取这里的语义色，跟 style.css 的 --cp-*
   一样必须按主题分两套：暗色要够亮（深底 4.5:1），亮色要够深（白底 4.5:1）。
   1Panel 的两档主色是 #3d8eff（暗）/ #005eeb（亮），实测分别为 4.7:1、5.1:1，
   所以直接采用它的取值；ok/warn/err 保留我们按 AA 重取过的档位（它的
   #a8abb2 一类副文字在白底只有 2.6:1，不能照抄）。 */
const darkSemantic = {
  brand: '#3d8eff', brandHover: '#6eaaff', brandPressed: '#2f76e8',
  ok: '#6fdba4', warn: '#f5c26f', err: '#f58a92', info: '#6eaaff',
};
const lightSemantic = {
  brand: '#005eeb', brandHover: '#196eed', brandPressed: '#0047b8',
  ok: '#1b8f3c', warn: '#7a5006', err: '#c62f3a', info: '#005eeb',
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
    borderRadius: '5px',
    borderRadiusSmall: '4px',
    fontFamily: `'Inter','PingFang SC','HarmonyOS Sans SC','Microsoft YaHei',system-ui,sans-serif`,
  };
}

const darkText = {
  text1: '#e3e6f3', text2: '#c0c2cf', text3: '#adb0bc',
  placeholder: '#9597a4', placeholderDisabled: '#5f6270',
};
const lightText = {
  text1: '#1f2329', text2: '#4a5262', text3: '#5f6a7d',
  placeholder: '#5f6a7d', placeholderDisabled: '#8a92a4',
};

/* 1Panel 的层次是"卡片比页面更暗"（dark: 页面 #2e313d / 卡片 #242633），
   与我们原来的相反；照抄这一条，配合 4px 圆角 + 0 0 4px 暗影才是那套观感。 */
const surfaces = {
  dark: {
    bg: '#2e313d',
    bg1: '#242633',
    elevated: '#242633',
    cardBorder: '#434552',
    border: '#434552',
    tableBorder: '#434552',
    thColor: '#2e313d',
    inputColor: '#2e313d',
    menuBg: '#242633',
    /* item 画在元素自身（style.css 的 --cp-menu-item-bg），hover/active 画在 naive 的
       ::before 上层， opaque 直接覆盖，所以深色档三档都是实色。 */
    menu: { item: '#2e313d', hover: '#434552', active: '#434552' },
    shadowCard: '0 0 4px rgba(36,38,51,0.72)',
  },
  light: {
    bg: '#f4f4f4',
    bg1: '#ffffff',
    elevated: '#ffffff',
    cardBorder: '#f2f2f2',
    border: '#f2f2f2',
    tableBorder: '#f2f2f2',
    thColor: '#fafafa',
    inputColor: '#ffffff',
    menuBg: 'rgba(0,94,235,0.1)',
    /* 1Panel 浅色 hover 只换文字/图标颜色，底色不动；那样 hover 完全看不出来，
       所以给同一支白色提一档不透明度——仍是白胶囊，不引入 naive 的灰底。
       0.35 是叠在 0.3 常态白之上的那一层，合成后约 0.55。 */
    menu: { item: 'rgba(255,255,255,0.3)', hover: 'rgba(255,255,255,0.35)', active: '#ffffff' },
    shadowCard: '0 0 4px rgba(0,94,235,0.1)',
  },
};

/* 侧栏菜单：1Panel 的菜单项是一颗颗胶囊（42px 高、4px 圆角、自带底色），
   选中项再加 2px 内描边 + 竖条。描边与竖条 naive 表达不了，在 Shell.vue 用 :deep() 补。
   状态色（hover/选中）必须走这里而不是 CSS：naive 把它们画在
   .n-menu-item-content::before 上，其中 :hover/--hover 两档的选择器权重不低于我们带
   scoped 属性的写法，而它的 <style> 是运行时注入、排在构建 CSS 之后，平权重后者胜——
   用 CSS 覆盖会在 hover 到选中项时被 naive 的灰底抢走。
   但胶囊"常态"底色不能也放这里：naive 没有 item 常态色变量（实测 ::before 常态是
   transparent，hover/选中才有 background-color），它唯一的 color 变量是菜单容器整块
   背景——写成胶囊底色会把 7px 间距一起染掉，胶囊就糊成一片。所以这里把容器设透明，
   常态底色由 Shell.vue 画在元素自身上（::before 是它的上层，状态色正好覆盖）。 */
const menuFor = (p: typeof darkSemantic, m: { item: string; hover: string; active: string }): GlobalThemeOverrides['Menu'] => ({
  itemHeight: '42px',
  borderRadius: '4px',
  /* m.item 不在这里用：它是胶囊常态底色，走 --cp-menu-item-bg（见 Shell.vue）。 */
  color: 'rgba(0,0,0,0)',
  itemColorHover: m.hover,
  itemColorActive: m.active,
  itemColorActiveHover: m.active,
  itemColorActiveCollapsed: m.active,
  itemTextColorActive: p.brand,
  itemIconColorActive: p.brand,
  itemTextColorChildActive: p.brand,
  itemIconColorChildActive: p.brand,
  itemTextColorHover: p.brand,
  itemIconColorHover: p.brand,
  arrowColorActive: p.brand,
});

export const darkOverrides: GlobalThemeOverrides = {
  common: commonFor(darkSemantic, darkText),
  Card: { borderRadius: '4px', borderColor: surfaces.dark.cardBorder, color: surfaces.dark.elevated, boxShadow: surfaces.dark.shadowCard },
  DataTable: { thColor: surfaces.dark.thColor, thFontWeight: '600', borderColor: surfaces.dark.tableBorder, tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '4px' },
  Button: { fontWeight: '500' },
  Input: { color: surfaces.dark.inputColor, borderRadius: '5px' },
  Layout: {
    color: surfaces.dark.bg,
    siderColor: surfaces.dark.menuBg,
    siderBorderColor: surfaces.dark.border,
    headerColor: surfaces.dark.bg1,
    headerBorderColor: surfaces.dark.border,
  },
  Menu: menuFor(darkSemantic, surfaces.dark.menu),
};

export const lightOverrides: GlobalThemeOverrides = {
  common: { ...commonFor(lightSemantic, lightText), bodyColor: surfaces.light.bg },
  Card: { borderRadius: '4px', borderColor: surfaces.light.cardBorder, color: surfaces.light.elevated, boxShadow: surfaces.light.shadowCard },
  DataTable: { thColor: surfaces.light.thColor, thFontWeight: '600', borderColor: surfaces.light.tableBorder, tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '4px' },
  Button: { fontWeight: '500' },
  Input: { color: surfaces.light.inputColor, borderRadius: '5px' },
  Layout: {
    color: surfaces.light.bg,
    siderColor: surfaces.light.menuBg,
    siderBorderColor: surfaces.light.border,
    headerColor: surfaces.light.bg1,
    headerBorderColor: surfaces.light.border,
  },
  Menu: menuFor(lightSemantic, surfaces.light.menu),
};

/** 运行时读取当前主题的 CSS 变量（供 canvas / xterm 等 JS 侧配色使用） */
export function cssVar(name: string, fallback = ''): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
