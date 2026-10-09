import { defineComponent, h, type PropType } from 'vue';
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
