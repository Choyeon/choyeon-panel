<script setup lang="ts">
/**
 * 通用 echarts 折线/面积封装：调用方只给 xs 与 series，颜色走 --cp-* 变量、
 * 主题切换自动重绘。每实例恰好一个 ResizeObserver，卸载时 dispose + disconnect。
 * 动画恒关（等价 prefers-reduced-motion：3s 轮询刷新时不产生持续补间）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { NSkeleton } from 'naive-ui';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { cssVar } from '../theme';

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

interface ChartSeries {
  name: string;
  /** CSS 变量名（'--cp-brand'）或字面色值 */
  color: string;
  data: (number | null)[];
  area?: boolean;
  /** 绑定的 y 轴下标，0 主轴 / 1 副轴（load1+进程数这类双量纲图用） */
  axis?: number;
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
  }>(),
  {
    xs: () => [],
    series: () => [],
    height: '200px',
    yFmt: undefined,
    y2Fmt: undefined,
    tipFmt: undefined,
    loading: false,
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

function buildOption() {
  const label = cssVar('--cp-text-dim', '#8a90a0');
  const axisLine = cssVar('--cp-border-strong', '#2c2c33');
  const split = cssVar('--cp-line', '#eef1f7');
  const hasSecond = props.series.some((s) => (s.axis ?? 0) !== 0);
  const yAxis: any[] = [
    {
      type: 'value',
      splitLine: { lineStyle: { color: split } },
      axisLabel: { color: label, fontSize: 11.5, formatter: fmtFn(props.yFmt) },
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
    // 双轴图再留 8px，否则副轴刻度贴边。
    grid: { left: 8, right: hasSecond ? 8 : 26, top: 14, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category', data: props.xs, boundaryGap: false,
      axisLine: { lineStyle: { color: axisLine } },
      axisLabel: { color: label, fontSize: 11.5, showMaxLabel: true, hideOverlap: true },
    },
    yAxis,
    series: props.series.map((s) => {
      const col = resolveColor(s.color);
      return {
        name: s.name, type: 'line', smooth: 0.4, showSymbol: false,
        yAxisIndex: s.axis ?? 0, data: s.data,
        lineStyle: { width: 2, color: col },
        areaStyle: s.area ? { color: areaGradient(s.color) } : undefined,
      };
    }),
    tooltip: {
      trigger: 'axis',
      ...(props.tipFmt ? { valueFormatter: (v: unknown) => props.tipFmt!(Number(v)) } : {}),
    },
    animation: false,
  };
}

function render() {
  chart?.setOption(buildOption(), { notMerge: true });
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
