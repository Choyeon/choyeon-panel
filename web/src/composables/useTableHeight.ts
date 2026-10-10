import {
  computed, getCurrentScope, nextTick, onMounted, onScopeDispose, ref, watch,
  type ComputedRef, type Ref,
} from 'vue';

/**
 * 表格可视高度的唯一出处。仓库里原本散落着 8 个不同的 `calc(100vh - NNNpx)`
 * （268/308/322/330/368/400/430…），每个都靠手调，改一次页头高度就得全部重算。
 *
 * 做法：量出表格在滚动容器里的 top，得到真实可用高度，然后
 * 1) 把 px 数交给 NDataTable（虚拟滚动只吃数值，字符串不行），
 * 2) 同时把同一个值写成 CSS 变量 `--table-h` 挂在滚动容器上，
 *    这样 style.css 里的 `--header-h` / `--table-h` 机制（另一位同学在加）可以直接消费，
 *    两边共用一次测量，不会各算各的。
 */
export type TableHeightOptions = {
  /** 表格自身之外还要预留的像素（分页条、脚注、底部留白） */
  reserve?: number;
  /** 高度下限，窄窗口/小屏时不至于压成一条缝 */
  min?: number;
  /** 写出的 CSS 变量名，默认 --table-h */
  varName?: string;
};

const OBSERVERS = new Set<ResizeObserver>();

/** 探针用：卸载后必须是 0 */
export function liveResizeObservers(): number {
  return OBSERVERS.size;
}

export function useTableHeight(opts: TableHeightOptions = {}) {
  const reserve = opts.reserve ?? 12;
  const min = opts.min ?? 220;
  const varName = opts.varName ?? '--table-h';

  // 挂到 NDataTable（组件实例）或原生元素上都可以
  const anchor = ref<{ $el?: HTMLElement } | HTMLElement | null>(null);
  const heightPx = ref(0);

  let ro: ResizeObserver | null = null;
  let varHost: HTMLElement | null = null;

  function el(): HTMLElement | null {
    const raw = anchor.value as { $el?: HTMLElement } | HTMLElement | null;
    if (!raw) return null;
    const node = (raw as { $el?: HTMLElement }).$el ?? (raw as HTMLElement);
    return node instanceof HTMLElement ? node : null;
  }

  function measure() {
    const node = el();
    if (!node?.isConnected) return;
    const rect = node.getBoundingClientRect();
    if (!rect.width && !rect.height) return;
    const scroller = node.closest('.cp-scroll') as HTMLElement | null;
    const sRect = scroller?.getBoundingClientRect();
    const padBottom = scroller ? parseFloat(getComputedStyle(scroller).paddingBottom) || 0 : 0;
    const bottom = (sRect ? sRect.bottom : window.innerHeight) - padBottom;
    heightPx.value = Math.max(min, Math.floor(bottom - rect.top - reserve));
    paintVar();
  }

  // 变量写在滚动容器上（容器内所有元素都能继承），找不到就退回根节点
  function paintVar() {
    const node = el();
    const host = (node?.closest('.cp-scroll') as HTMLElement | null) ?? document.documentElement;
    if (host !== varHost) {
      varHost?.style.removeProperty(varName);
      varHost = host;
    }
    varHost.style.setProperty(varName, `${heightPx.value}px`);
  }

  /** 表格上方的内容高度变了（展开日志卡、出现提示条）时手动调用 */
  function remeasure() {
    nextTick(measure);
  }

  // 观测目标每次重新绑定，不一次性捕获：页面里的表格几乎都套在 `v-if="tab === …"` 的
  // 卡片里（Services / Databases / LogCenter / Sites / Apps），切走再切回来时 Vue 会销毁
  // 并重建表格元素。一次性捕获的话 RO 就永远挂在那个已经脱离文档的旧节点上——新表格的
  // 尺寸变化再也不会触发重测，旧节点还被观测器一直引用着。
  function observeTargets() {
    if (!ro) return;
    ro.disconnect();
    const node = el();
    const scroller = node?.closest('.cp-scroll') as HTMLElement | null;
    if (scroller) ro.observe(scroller);
    if (node?.parentElement) ro.observe(node.parentElement);
  }

  // 首屏先给一个保守估计，避免量到之前那一帧表格没高度上限
  if (typeof window !== 'undefined') heightPx.value = Math.max(min, Math.round(window.innerHeight * 0.6));

  if (getCurrentScope()) {
    const start = () => {
      if (typeof ResizeObserver === 'undefined') {
        window.addEventListener('resize', measure);
        measure();
        return;
      }
      ro = new ResizeObserver(measure);
      OBSERVERS.add(ro);
      observeTargets();
      window.addEventListener('resize', measure);
      measure();
    };
    const stopData = watch(() => el(), () => { observeTargets(); remeasure(); });
    onMounted(start);
    onScopeDispose(() => {
      stopData();
      window.removeEventListener('resize', measure);
      if (ro) {
        ro.disconnect();
        OBSERVERS.delete(ro);
        ro = null;
      }
      varHost?.style.removeProperty(varName);
      varHost = null;
      anchor.value = null;
    });
  }

  /** 给 :max-height 用的数值 */
  const maxHeight = computed(() => heightPx.value) as ComputedRef<number>;
  /** 给 CSS/其它组件用的字符串形式（非虚拟滚动的表格可以直接用它） */
  const maxHeightCss = ref('') as Ref<string>;
  watch(heightPx, (v) => { maxHeightCss.value = `${v}px`; }, { immediate: true });

  return { anchor, heightPx, maxHeight, maxHeightCss, measure: remeasure };
}
