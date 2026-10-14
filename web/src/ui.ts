import { computed, defineComponent, h, onBeforeUnmount, ref, type ComputedRef, type PropType } from 'vue';
import { NButton as NaiveButton } from 'naive-ui';

/**
 * NButton 的图标只认 `#icon` 插槽（naive-ui 没有 icon prop），而全站按钮写的是
 * `:icon="ico('RefreshOutline')"`：这个 prop 会被当成普通 attribute 丢掉，
 * 于是带文字的按钮只剩文字、纯图标按钮渲染成一块空白。
 * 这里把 prop 转成插槽，一处修复，避免逐个改写九十多处调用点。
 */
export const NButton = defineComponent({
  name: 'NButton',
  inheritAttrs: false,
  props: {
    icon: { type: Function as PropType<() => any>, default: undefined },
  },
  setup(props, { attrs, slots }) {
    return () =>
      h(NaiveButton, attrs, {
        ...slots,
        icon: slots.icon ?? props.icon,
      } as any);
  },
});

/**
 * 表格列的「被裁掉的全文怎么看见」。
 *
 * 触屏没有 hover，而 naive 的 `ellipsis: { tooltip: true }` 只在 hover 时浮出浮层，
 * 被裁的那半句就再也没有第二个入口（375px 实测：`.n-ellipsis` 连 `title` 都不带，
 * 最长一条告警内容被裁掉 155px）。这里把触发方式改成 click：桌面点一下、手机按一下就看得见。
 * 能折行的格子优先用 `.cp-prose` 折行（结论不该等人点一下才露脸），
 * 只有必须保持单行的表格才用它——审计那张开了 `virtual-scroll`，行高必须齐。
 */
export const tapEllipsis = { tooltip: { trigger: 'click' } } as const;

/**
 * 抽屉宽度的唯一出处。裸百分比在手机档是坏的：375px 下 `'50%'` 实测 188px、
 * `'30%'` 只剩 113px，装表单的那点空间全被 230px 的控件顶穿；而 naive 把宽度
 * 原样写成行内样式，CSS 里 min-width 又压过 max-width，浮层会直接宽出屏幕。
 * 规则：≤ 640px 一律全屏（移动端的抽屉就是整页弹层），更宽的口径取比例、
 * 下限 420（表单控件最少要能并排下 label+输入）、上限 860（大屏不把一行字拉散）。
 */
const viewportWidth = ref(window.innerWidth);
let vwSubs = 0;
const onVwResize = () => { viewportWidth.value = window.innerWidth; };

export function useDrawerWidth(fraction: number): ComputedRef<number> {
  if (!vwSubs) window.addEventListener('resize', onVwResize, { passive: true });
  vwSubs += 1;
  onBeforeUnmount(() => {
    vwSubs -= 1;
    if (!vwSubs) window.removeEventListener('resize', onVwResize);
  });
  return computed(() =>
    viewportWidth.value <= 640
      ? viewportWidth.value
      : Math.min(Math.max(Math.round(viewportWidth.value * fraction), 420), 860),
  );
}
