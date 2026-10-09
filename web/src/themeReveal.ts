export type ThemeOrigin = { x: number; y: number };

type VTDocument = Document & {
  startViewTransition?: (cb: () => void | Promise<void>) => { finished: Promise<void> };
};

const EASE = 'cubic-bezier(0.32, 0.72, 0, 1)';
const DUR = 480;

// 圆形扩散（切到深色）/ 圆形收缩（切回浅色）的主题切换动效。
// 依据 View Transitions 的两张 root 快照：扩散时移动的是 new 层，收缩时移动的是 old 层，
// 另一层必须整块可见且排在下面，所以用 z-index 指定谁盖住谁。

function css(dir: 'grow' | 'shrink', x: number, y: number, r: number): string {
  const at = `${Math.round(x)}px ${Math.round(y)}px`;
  const moving = dir === 'grow' ? 'new' : 'old';
  const still = dir === 'grow' ? 'old' : 'new';
  const [from, to] = dir === 'grow' ? [0, r] : [r, 0];
  return [
    `@keyframes cp-theme-reveal{from{clip-path:circle(${from}px at ${at})}to{clip-path:circle(${to}px at ${at})}}`,
    '::view-transition-group(root){animation:none}',
    '::view-transition-old(root),::view-transition-new(root){animation:none;mix-blend-mode:normal}',
    `::view-transition-${moving}(root){z-index:1;animation:cp-theme-reveal ${DUR}ms ${EASE} both}`,
    `::view-transition-${still}(root){z-index:auto}`,
  ].join('');
}

export async function revealThemeSwitch(opts: {
  origin?: ThemeOrigin;
  toDark: boolean;
  mutate: () => void | Promise<void>;
}): Promise<void> {
  const doc = document as VTDocument;
  const { origin } = opts;
  if (!origin || typeof doc.startViewTransition !== 'function' || typeof window === 'undefined'
    || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    await opts.mutate();
    return;
  }
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  // 半径要覆盖到最远的视口角，否则圆形动画结束时四角还没被新主题盖住
  const r = Math.ceil(Math.hypot(Math.max(origin.x, vw - origin.x), Math.max(origin.y, vh - origin.y)));

  const style = document.createElement('style');
  style.textContent = css(opts.toDark ? 'grow' : 'shrink', origin.x, origin.y, r);
  document.head.appendChild(style);
  document.documentElement.classList.add('cp-vt');
  // cp-vt 会关掉全站过渡，页面切到后台导致过渡不结束时不能把它留在那儿
  const bail = window.setTimeout(cleanup, DUR + 2000);
  let ran = false;
  function cleanup() {
    window.clearTimeout(bail);
    document.documentElement.classList.remove('cp-vt');
    style.remove();
  }
  try {
    await doc.startViewTransition(async () => {
      ran = true;
      await opts.mutate();
    })!.finished;
  } catch {
    // 页面不可见等情况会中断过渡，回调可能根本没跑；主题必须落地，不能只图动画好看
    if (!ran) await opts.mutate();
  } finally {
    cleanup();
  }
}
