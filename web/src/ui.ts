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

/**
 * systemd 的 ActiveState → 说人话。整个前端只有这一份。
 *
 * 服务列表原来自己写了一份（`failed: '异常'`、`activating: '启动中'`），应用列表与详情走
 * 下面的 UNIT_TAG（`failed: '启动失败'`、`activating: '正在启动'`），同一个 unit 在两个页面上
 * 挂着两种说法，用户会当成两件事。状态取值仍按 systemd 回什么来（列表那边是枚举里挑出来的），
 * 认不出的原样显示——把未知状态翻译成一个已知说法，比原样摆着更误导人。
 */
export const UNIT_STATE_LABELS: Record<string, string> = {
  active: '运行中',
  reloading: '重载中',
  activating: '正在启动',
  deactivating: '正在停止',
  failed: '启动失败',
  inactive: '已停止',
  maintenance: '维护中',
};

export function stateLabel(s: string): string {
  return UNIT_STATE_LABELS[s || ''] || s;
}

/**
 * systemd 的 unit 状态 → CpStatusTag 的 status、说人话的文字、以及小圆点的色调。
 *
 * 判词只由后端 systemd_ops 出一份（列表与详情都带 unitState / unitMissing），这里只负责说法。
 * 以前应用列表和详情页各自写 `running ? 运行中 : 已停止`，于是"正在启动""上次启动失败"
 * "这台机器上根本没有这个 unit"全被压成"已停止"——而"已停止"在界面上的意思是"点启动就行"，
 * 对一个不存在的 unit 点下去只会换来一句 not loaded。
 */
const UNIT_TAG: Record<string, { status: string; text: string; tone: string }> = {
  active: { status: 'running', text: UNIT_STATE_LABELS.active, tone: 'ok' },
  reloading: { status: 'running', text: UNIT_STATE_LABELS.reloading, tone: 'ok' },
  activating: { status: 'starting', text: UNIT_STATE_LABELS.activating, tone: 'warn' },
  deactivating: { status: 'waiting', text: UNIT_STATE_LABELS.deactivating, tone: 'warn' },
  failed: { status: 'failed', text: UNIT_STATE_LABELS.failed, tone: 'err' },
  inactive: { status: 'stopped', text: UNIT_STATE_LABELS.inactive, tone: 'idle' },
};

export function unitTag(r: { unitState?: string; unitMissing?: boolean; suggestUnit?: string }): { status: string; text: string; tone: string } {
  // 「单元不存在」原话只说了一半：本机 panel-<名称>.service 确实没有，但同名 unit 就在 systemd 里
  // 跑着（应用其实活着）。那种情况判成红色「不存在」会把人推向「立即部署」——而部署写的是第二个
  // unit，和正在服务的那个抢同一个端口。说法改成「未纳管」，指向配置页的「已有 unit」。
  if (r.unitMissing)
    return r.suggestUnit
      ? { status: 'conflict', text: '未纳管', tone: 'warn' }
      : { status: 'notfound', text: '单元不存在', tone: 'err' };
  return UNIT_TAG[r.unitState || ''] || { status: 'unknown', text: '状态未知', tone: 'idle' };
}
