import { computed, defineComponent, h, onBeforeUnmount, ref, type ComputedRef, type PropType } from 'vue';
import { NButton as NaiveButton } from 'naive-ui';
import { fmtBytes } from './composables/useFormat';

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

/**
 * 截断要自己说出来。面板里有几处格子装不下原值（Redis 成员值、JSON 预览、安全通告号），
 * 过去各写各的截断法，其中两处连"被切过"都不提——半截文本看起来就像数据本来到此为止，
 * 只读账号又没有第二个入口，于是"值只有这么长"成了一个谎。
 * 上限由调用方给（受格子大小约束），说法只有这一份。
 */
export function cutChars(text: string, max: number, where: string): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…（已截断，共 ${text.length} 字符，${where}）`;
}

/**
 * 同上，但限的是条目数：只列前 max 条时，真实条数跟着一起走。
 * `realTotal` 给的是接口那头没被切过的总数——数组本身可能已经被服务端切过（补丁清单就有
 * 这一刀），这时 `items.length` 是传输上限而不是条数，拿它写「等 N 条」等于用面板自己的
 * 限额冒充测量结果。
 */
export function cutCounted<T>(items: T[], max: number, show: (item: T) => string, realTotal?: number): string {
  const shown = items.slice(0, max).map(show).join('、');
  const total = typeof realTotal === 'number' && realTotal > items.length ? realTotal : items.length;
  return items.length > max || total > items.length ? `${shown} 等 ${total} 条` : shown;
}

/**
 * 巡检/探测回一句「本轮有 N 节压根没查成」时该怎么说；没查成的节为 0 就回空串。
 *
 * 后端每个入口的成功回应原本只说「巡检完成，新增 N 条历史」。systemctl 不可用、系统指标
 * 读失败、应用列表查询失败这些情况一份历史都不新增，于是"全查完了都没事"和"什么都没读成"
 * 在界面上长得一模一样（批次101 定的规矩：后端说这轮没做，界面不许替它说做完了）。
 * 说法只有这一份——告警页和设置页点的是同一轮巡检，各写各的迟早只剩一处转述。
 */
export function uncheckedNote(list?: string[]): string {
  if (!Array.isArray(list) || list.length === 0) return '';
  const head = list.slice(0, 3).join('；');
  const more = list.length > 3 ? `；另有 ${list.length - 3} 节详见面板日志` : '';
  return `本轮有 ${list.length} 节没查成：${head}${more}`;
}

/**
 * 「这一项本轮读不到」的说法只有一个出处：定时任务页的 Timer 列、上次执行列和
 * 输出抽屉都要说它，三处各写一遍迟早只剩一处转述。
 *
 * 后端给的是原因（`systemctl 退出码 …：…`），不是结论——结论必须由这里说出来：
 * 读不到不等于「定时器没挂」，也不等于「从没执行过」。
 */
export function unknownNote(what: string, why?: string): string {
  return why ? `${what}本轮读不到：${why}` : `${what}本轮读不到`;
}

/** 上面那句用在 title/说明行；格子里放不下，第三态的短名也只有一个。 */
export const UNKNOWN_LABEL = '未读到';

/**
 * 日志来源清单被服务端扫描上限截断时的说法（尾读、分钟统计、按天回看三个页签共用）。
 *
 * 下拉里列出 60 个来源、而白名单里其实还有第 61 个时，界面看起来和「这台机器就 60 份日志」
 * 完全同形：序号照旧、选中照旧，只是「3 号」之外还有没有来源这件事没人说。
 * 服务端给不出真实总数（数它就得把为了封顶而省掉的那次走盘重做一遍），所以这里也只能
 * 如实说「不代表全部」，而不是编一个总数。
 */
export function sourcesCappedNote(limit: number): string {
  return `来源清单只列到本次扫描上限 ${limit} 个，排在其后的日志没有列出——这些序号不代表全部；`
    + '把常看的目录单独加进 CP_LOG_ROOTS 可以收窄范围';
}

/** 回看世代数量触顶：更早的世代还在盘上，只是这一次没翻。 */
export function seriesCappedNote(listed: number, total: number, limit: number): string {
  return `回看只统计了 ${listed}/${total} 份世代（数量上限 ${limit} 份），`
    + '更早的轮替件还在盘上但没进这一轮，环比因此比的是被削短的一段';
}

/**
 * 连世代清单都列不出来时的一句。
 *
 * 原来这里和「这个站点没人访问」同形：接口交出空列表，界面就画一张空表。
 * 列不出目录是权限或正好赶上 logrotate 改名，有没有历史是另一件事。
 */
export const SERIES_UNREADABLE_NOTE = '列不出这个来源的轮替世代（多半是所在目录没有读权限，'
  + '或正被 logrotate 改名）——这一轮没看到历史，不等于盘上没有历史';

/**
 * 速率（每秒字节）的「这一轮没有分母」：后端给 null，不是 0（批次108）。
 *
 * CPU 占用率与网速/IO 都是两次累计量的差除以时间，面板刚启动的第一帧、两次采样落在
 * 同一秒、或计数器回绕之后都算不出这个差。`x || 0` 那种写法会把"量不出"渲染成
 * 「0 B/s」——看着像一台闲得发慌的机器，而它只是刚起来。绝对量（总收发字节、容量）
 * 不走这里：那些是真读到了数。
 */
export function rateNum(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** 速率一格的读法：有分母写 `12.3 KB/s`，没有分母就明说未读到。 */
export function rateText(v: unknown): string {
  const n = rateNum(v);
  return n === null ? UNKNOWN_LABEL : `${fmtBytes(n)}/s`;
}
