<script setup lang="ts">
/**
 * 圆环仪表（对标 1Panel 首页「状态」条）。
 * 1Panel 用的是 echarts 极坐标 bar（polar.radius ['71%','80%']、roundCap），
 * 这里用等效的 SVG：几何完全照抄（环中心线半径 37.75/100、线宽 4.5/100，
 * 渲染在 160px 盒子里就是 ⌀128 环 + 7.2px 厚），但省掉每个仪表一个 canvas。
 * 颜色分档（CPU>85 转红）是我们加的——1Panel 所有环同一种蓝，它没有阈值语义。
 */
import { computed, getCurrentInstance } from 'vue';

const props = withDefaults(
  defineProps<{
    /** 0-100，超出会夹住；null 表示还没有数据 */
    percent: number | null | undefined;
    /** 圆心的主读数，例如 37.45 */
    text: string;
    unit?: string;
    label: string;
    detail?: string;
    color?: string;
    size?: number;
  }>(),
  { unit: '', color: 'var(--cp-brand)', detail: '', size: 160 },
);

// viewBox 固定 100×100，实际尺寸靠 width/height 缩放，线宽随之等比
const RING = 37.75;
const SW = 4.5;
const C = 2 * Math.PI * RING;

// SVG 的 gradient id 是文档级全局的，同页四个仪表必须各有各的 id，
// 否则后挂载的那个会被前一个的渐变染色（阈值色全丢）。instance uid 天然唯一。
const gid = `cpg${getCurrentInstance()?.uid ?? 0}`;

const p = computed(() => {
  const v = props.percent;
  if (v == null || !Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(100, v));
});
const offset = computed(() => C * (1 - p.value / 100));
const empty = computed(() => props.percent == null || !Number.isFinite(Number(props.percent)));
// 渐变两端：亮端由传入色混白得到，暗端就是该色本身（1Panel 的 #4c8ef1 → #005eeb）。
// 走 style 而不是 stop-color 属性：color-mix() 只在 CSS 属性里被解析。
const stopLight = computed(() => `color-mix(in srgb, ${props.color} 55%, #ffffff)`);
</script>

<template>
  <div class="gauge" :aria-label="`${label} ${text}${unit}，${detail || '无明细'}`" role="img">
    <svg :width="size" :height="size" viewBox="0 0 100 100" aria-hidden="true">
      <defs>
        <!-- 1Panel 的进度色是从浅到深的一条渐变（#4c8ef1 → #005eeb），不是纯色 -->
        <linearGradient :id="gid" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0" :style="{ stopColor: stopLight }" />
          <stop offset="1" :style="{ stopColor: color }" />
        </linearGradient>
      </defs>
      <circle class="track" :cx="50" :cy="50" :r="RING" :stroke-width="SW" fill="none" />
      <circle
        :cx="50" :cy="50" :r="RING" :stroke-width="SW" fill="none" :stroke="`url(#${gid})`"
        stroke-linecap="round" :stroke-dasharray="C" :stroke-dashoffset="offset"
        :class="{ dim: empty }" transform="rotate(-90 50 50)"
      />
    </svg>
    <div class="gauge-mid">
      <div class="gauge-num">{{ text }}<span v-if="unit" class="gauge-unit">{{ unit }}</span></div>
      <div class="gauge-label">{{ label }}</div>
    </div>
    <div class="gauge-detail">{{ detail || ' ' }}</div>
  </div>
</template>

<style scoped>
/* 圆心叠字：svg 与文字放在 grid 的同一格（'ring'），比 absolute + translate 少一处魔数 */
.gauge { position: relative; display: grid; place-items: center; grid-template-areas: 'ring' 'detail'; min-width: 0; }
.gauge > svg { grid-area: ring; flex-shrink: 0; max-width: 100%; height: auto; }
.track { stroke: var(--cp-ring-track); }
svg circle:last-child { transition: stroke-dashoffset var(--dur-page) var(--ease), stroke var(--dur) var(--ease); }
svg circle.dim { opacity: 0.35; }
.gauge-mid { grid-area: ring; text-align: center; pointer-events: none; }
/* 1Panel 的圆心读数：整数 22px/500，小数与 % 14px，指标名 13px */
.gauge-num { font-size: 22px; font-weight: 500; line-height: 25px; color: var(--cp-text-strong); letter-spacing: -0.02em; }
.gauge-unit { font-size: 14px; color: var(--cp-text-strong); margin-left: 1px; }
.gauge-label { font-size: 13px; color: var(--cp-text-dim); margin-top: 2px; }
.gauge-detail {
  grid-area: detail; text-align: center; font-size: var(--fs-xs); color: var(--cp-text-mute);
  width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
