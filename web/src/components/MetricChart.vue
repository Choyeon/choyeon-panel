<script setup lang="ts">
/**
 * 通用 echarts 折线/面积封装：调用方只给 xs 与 series，颜色走 --cp-* 变量、
 * 主题切换自动重绘。每实例恰好一个 ResizeObserver，卸载时 dispose + disconnect。
 * 动画只在「这一组数据第一次画出来」和「换了另一组数据」（animKey）各播一次：
 * 稳态是 3s 一次的增量 setOption(notMerge)，全程开着等于每 3s 把所有曲线重补间，
 * CPU 白烧且补间永远追不上刷新节奏；全关掉则是首屏四条线凭空出现，没有生成过程。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { NSkeleton } from 'naive-ui';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { cssVar } from '../theme';
import { prefersReducedMotion } from '../composables/useTween';

echarts.use([LineChart, GridComponent, TooltipComponent, DataZoomComponent, LegendComponent, CanvasRenderer]);

interface ChartSeries {
  name: string;
  /** CSS 变量名（'--cp-brand'）或字面色值 */
  color: string;
  data: (number | null)[];
  area?: boolean;
  /** 绑定的 y 轴下标，0 主轴 / 1 副轴（load1+进程数这类双量纲图用） */
  axis?: number;
  /** 只描边不填充的细线（1Panel 的"下行"就是这种次级曲线） */
  thin?: boolean;
  /** 1Panel 的监控曲线是折线（不平滑），直连更能看出尖峰；实时 CPU 图仍用平滑 */
  straight?: boolean;
}

const props = withDefaults(
  defineProps<{
    xs?: string[];
    series?: ChartSeries[];
    height?: string;
    yFmt?: (v: number) => string;
    y2Fmt?: (v: number) => string;
    tipFmt?: (v: number) => string;
    loading?: boolean;
    /** 底部拖动条：实时曲线也要能回看前几分钟，光靠 tooltip 做不到 */
    dataZoom?: boolean;
    legend?: boolean;
    /** y 轴单位标题，1Panel 写成 "( KB/s )" 这种带括号的弱色注记 */
    yName?: string;
    /** 换一组数据就换一个值（历史区间 1h→7d、监控「流量/磁盘 IO」）：
     *  变了才重播一次入场动画——那是另一批数据，从零生成是诚实的；
     *  同一个序列的 3s 增量刷新不播。 */
    animKey?: string | number;
  }>(),
  {
    xs: () => [],
    series: () => [],
    height: '200px',
    yFmt: undefined,
    y2Fmt: undefined,
    tipFmt: undefined,
    loading: false,
    dataZoom: false,
    legend: false,
    yName: undefined,
    animKey: undefined,
  },
);

const el = ref<HTMLElement | null>(null);
let chart: echarts.ECharts | null = null;
let ro: ResizeObserver | null = null;
let mo: MutationObserver | null = null;

function resolveColor(c: string): string {
  return c.startsWith('--') ? cssVar(c, '#4f7cff') : c;
}

function rgbaOf(c: string, alpha: number): string {
  const hex = resolveColor(c).replace('#', '');
  const full = hex.length === 3 ? hex.split('').map((x) => x + x).join('') : hex;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) return `rgba(79,124,255,${alpha})`;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function areaGradient(color: string) {
  return {
    type: 'linear' as const, x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [
      { offset: 0, color: rgbaOf(color, 0.32) },
      { offset: 1, color: rgbaOf(color, 0) },
    ],
  };
}

function fmtFn(fn?: (v: number) => string, fallback = '') {
  return (v: number) => (fn ? fn(Number(v)) : `${v}${fallback}`);
}

/** 用户拖动过的时间窗口。3s 一次的 setOption(notMerge) 会把没保存的窗口重置回全量，
 *  表现为"刚框选好区间就自己弹回去"，所以每次重建 option 前先把它抄下来。 */
let zoom = { start: 0, end: 100 };

/** legend 的显隐选择同理：notMerge 重建会把用户点掉的曲线塞回来，
 *  表现为"点掉『下行』之后 ≤3s 它自己又出现，永远关不掉"。 */
let legendSelected: Record<string, boolean> | null = null;

/** 入场动画是否已经播过。theme 切换、3s 增量刷新都会重建 option，只有第一次
 *  真正画出数据的那一帧（以及 animKey 换掉之后的那一帧）允许播。 */
let animated = false;

function buildOption(animate: boolean) {
  const label = cssVar('--cp-text-dim', '#8a90a0');
  const axisLine = cssVar('--cp-border-strong', '#2c2c33');
  const split = cssVar('--cp-line', '#eef1f7');
  const tipBg = cssVar('--cp-bg-1', '#ffffff');
  const tipText = cssVar('--cp-text', '#1f2329');
  const hasSecond = props.series.some((s) => (s.axis ?? 0) !== 0);
  const yAxis: any[] = [
    {
      type: 'value',
      // 1Panel 的分割线是虚线，比实线更退后，曲线是主角
      splitLine: { lineStyle: { type: 'dashed', color: split } },
      axisLabel: { color: label, fontSize: 11.5, formatter: fmtFn(props.yFmt) },
      ...(props.yName ? { name: props.yName, nameTextStyle: { color: label, fontSize: 11.5 } } : {}),
    },
  ];
  if (hasSecond) {
    yAxis.push({
      type: 'value',
      splitLine: { show: false },
      axisLabel: { color: label, fontSize: 11.5, formatter: fmtFn(props.y2Fmt) },
    });
  }
  return {
    // containLabel 只按轴标签的"半宽"预留，最右侧那个时间标签仍会被画布裁掉半个字；
    // 双轴图再留 8px，否则副轴刻度贴边。有 dataZoom 时底部要腾出滑块（18px + 8px 间距）。
    // 注意：1Panel 写的是 left/right:65，那是为了给**绝对定位悬浮在图上方**的读数芯片
    // 让位；我们的芯片在正常文档流里（.chip-row），照抄 65 只会白白吃掉两侧宽度。
    grid: {
      left: 8,
      right: hasSecond ? 8 : 26,
      top: props.legend ? 28 : 14,
      bottom: props.dataZoom ? 26 : 4,
      containLabel: true,
    },
    xAxis: {
      type: 'category', data: props.xs, boundaryGap: false,
      axisLine: { lineStyle: { color: axisLine } },
      axisTick: { lineStyle: { color: axisLine } },
      axisLabel: { color: label, fontSize: 11.5, showMaxLabel: true, hideOverlap: true },
    },
    yAxis,
    series: props.series.map((s) => {
      const col = resolveColor(s.color);
      return {
        name: s.name, type: 'line', smooth: s.straight ? false : 0.4, showSymbol: false,
        // 点数上千时（24h/7d 档）先做 LTTB 降采样，否则每 3s 重建 option 会明显卡顿
        sampling: 'lttb',
        yAxisIndex: s.axis ?? 0, data: s.data,
        // legend 的圆点取 itemStyle.color，只给 lineStyle 的话图例会掉回默认调色板色
        itemStyle: { color: col },
        lineStyle: { width: s.thin ? 1.2 : 2, color: col },
        areaStyle: s.area ? { color: areaGradient(s.color) } : undefined,
      };
    }),
    tooltip: {
      trigger: 'axis',
      backgroundColor: tipBg,
      borderColor: axisLine,
      textStyle: { color: tipText, fontSize: 12 },
      ...(props.tipFmt ? { valueFormatter: (v: unknown) => props.tipFmt!(Number(v)) } : {}),
    },
    ...(props.legend
      ? {
          legend: {
            show: true, top: 2, right: 4, itemWidth: 8, itemHeight: 8, icon: 'circle',
            textStyle: { color: label, fontSize: 12 },
            // 不回填 selected 时，notMerge 重建等价于每次刷新都把用户的显隐选择抹掉
            ...(legendSelected ? { selected: legendSelected } : {}),
          },
        }
      : {}),
    ...(props.dataZoom
      ? {
          dataZoom: [
            {
              type: 'slider', height: 18, bottom: 2, start: zoom.start, end: zoom.end,
              borderColor: 'transparent', backgroundColor: split,
              fillerColor: rgbaOf('--cp-brand', 0.12),
              dataBackground: { lineStyle: { width: 1, color: axisLine }, areaStyle: { color: axisLine } },
              selectedDataBackground: { lineStyle: { width: 1, color: cssVar('--cp-brand') }, areaStyle: { color: 'transparent' } },
              handleStyle: { color: cssVar('--cp-brand'), borderColor: cssVar('--cp-brand') },
              moveHandleStyle: { color: axisLine },
              showDetail: false,
            },
            // 滚轮缩放：滑块只给"拖"，看长历史时还得能缩到某个窗口
            { type: 'inside', start: zoom.start, end: zoom.end, zoomOnMouseWheel: false, moveOnMouseWheel: true },
          ],
        }
      : {}),
    animation: animate,
    animationDuration: 850,
    // as const：抽成函数后字面量会被推宽成 string，setOption 的 AnimationEasing 联合类型接不上
    animationEasing: 'cubicOut' as const,
  };
}

function render() {
  if (chart && props.dataZoom) {
    const cur: any = (chart.getOption() as any)?.dataZoom?.[0];
    if (cur && typeof cur.start === 'number' && typeof cur.end === 'number') {
      zoom = { start: cur.start, end: cur.end };
    }
  }
  if (chart && props.legend) {
    const sel: any = (chart.getOption() as any)?.legend?.[0]?.selected;
    if (sel && typeof sel === 'object') legendSelected = { ...sel };
  }
  // 空数据不播：给一条还不存在的线做入场动画，等到数据到货时动画早演完了。
  const animate = !animated && props.xs.length > 0 && !prefersReducedMotion();
  if (animate) animated = true;
  chart?.setOption(buildOption(animate), { notMerge: true });
}

onMounted(() => {
  if (!el.value) return;
  chart = echarts.init(el.value);
  render();
  if (window.ResizeObserver) {
    ro = new ResizeObserver(() => chart?.resize());
    ro.observe(el.value);
  }
  // 主题切换后坐标轴/曲线颜色必须跟着换，否则深色底上留着浅色的分割线
  mo = new MutationObserver(() => render());
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
});

watch(() => [props.xs, props.series], render);
// 区间/指标切换：换的是另一批数据，把入场标记放下，等下一次数据到货那一帧重播一次。
watch(() => props.animKey, () => { animated = false; });

onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  mo?.disconnect();
  mo = null;
  chart?.dispose();
  chart = null;
});
</script>

<template>
  <div class="mchart" :style="{ height }">
    <div ref="el" class="mchart-canvas"></div>
    <!-- 骨架屏覆盖而不是替换容器：loading 切换时高度不变，布局不跳动 -->
    <div v-if="loading" class="mchart-skeleton">
      <NSkeleton height="60px" :repeat="3" class="cp-shimmer" style="border-radius: var(--radius-sm)" />
    </div>
  </div>
</template>

<style scoped>
.mchart { position: relative; width: 100%; min-width: 0; }
.mchart-canvas { width: 100%; height: 100%; min-width: 0; }
.mchart-skeleton { position: absolute; inset: 0; pointer-events: none; }
</style>
