/**
 * 数值补间：CSS transition 能补 stroke，补不了 textContent。
 * 概览的读数每 3s 跳一次，不补间就是「21 → 24」硬切——读到的是「数字跳坏了」
 * 而不是「涨了三」；圆环用 rAF 一起补，环和圆心那个数才是同一个时刻的同一个值。
 *
 * prefers-reduced-motion 必须在这里再判一次：style.css 那条 kill switch 只掐 CSS
 * 动画与过渡，管不到 requestAnimationFrame，只在 CSS 里写豁免等于漏掉这一层。
 */
import { onScopeDispose, ref, watch } from 'vue';
import type { Ref, WatchSource } from 'vue';

const easeOutCubic = (t: number) => 1 - (1 - t) ** 3;

export function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

/**
 * 目标值 → 逐帧逼近的显示值。
 * null 目标表示「还没到货」，此时直接给 null，不许从 0 爬：骨架阶段先画一条
 * 0% 的环再涨到真值，读起来像这台机器刚从空载开始忙。
 */
export function useTweenNumber(
  source: WatchSource<number | null | undefined>,
  dur = 600,
): Ref<number | null> {
  const shown = ref<number | null>(null);
  let raf = 0;

  const stop = watch(
    source,
    (to) => {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      if (to == null || !Number.isFinite(to)) {
        shown.value = null;
        return;
      }
      // 首帧从 0 爬就是入场那一下的「读数生成」，之后每轮只补变化量。
      const from = shown.value ?? 0;
      // 后台标签页一帧都不排（浏览器直接停掉 rAF），这里不夹平就会留下「读数已经到货、
      // 环还停在 0」的仪表——正是这个补间要消灭的那种自相矛盾。
      if (prefersReducedMotion() || document.hidden || dur <= 0 || Math.abs(to - from) < 0.02) {
        shown.value = to;
        return;
      }
      const t0 = performance.now();
      const step = (now: number) => {
        const k = Math.min(1, (now - t0) / dur);
        shown.value = from + (to - from) * easeOutCubic(k);
        if (k < 1) {
          raf = requestAnimationFrame(step);
        } else {
          raf = 0;
          shown.value = to;
        }
      };
      raf = requestAnimationFrame(step);
    },
    { immediate: true },
  );

  onScopeDispose(() => {
    stop();
    if (raf) cancelAnimationFrame(raf);
  });
  return shown;
}
