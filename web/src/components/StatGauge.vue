<script setup lang="ts">
/**
 * 圆环仪表（对标 1Panel 首页「状态」条）。
 * 1Panel 用的是 echarts 极坐标 bar（polar.radius ['71%','80%']、roundCap），
 * 这里用等效的 SVG：几何完全照抄（环中心线半径 37.75/100、线宽 4.5/100，
 * 渲染在 160px 盒子里就是 ⌀128 环 + 7.2px 厚），但省掉每个仪表一个 canvas。
 * 颜色分档（CPU>85 转红）是我们加的——1Panel 所有环同一种蓝，它没有阈值语义。
 *
 * 这一版把阈值语义做完：档位不只改颜色，还在环外侧画出那条线（红环要指得出
 * 「越过的是哪一格」，否则颜色只是吓人的装饰），环与圆心读数用同一个 rAF 补间
 * 一起走（CSS transition 补不了 textContent，两条速度会让环和数字各说各话）。
 */
import { computed, getCurrentInstance } from 'vue';
import { useTweenNumber } from '../composables/useTween';

type Tone = 'brand' | 'warn' | 'err';

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
    /** 与 color 同一处的判档（Dashboard 的 toneOf），这里只用来选光晕与刻度强调 */
    tone?: Tone;
    /** 警戒线位置（0-100），null/≤0/≥100 表示这一档没给，不画 */
    warn?: number | null;
    crit?: number | null;
    /** 悬停说明：这块盘是从哪个目录量到的。标签只报盘名，出处放这里 */
    note?: string;
  }>(),
  { unit: '', color: 'var(--cp-brand)', detail: '', size: 160, tone: 'brand', warn: null, crit: null, note: '' },
);

// viewBox 固定 100×100，实际尺寸靠 width/height 缩放，线宽随之等比
const RING = 37.75;
const SW = 4.5;
const C = 2 * Math.PI * RING;

// SVG 的 gradient id 是文档级全局的，同页四个仪表必须各有各的 id，
// 否则后挂载的那个会被前一个的渐变染色（阈值色全丢）。instance uid 天然唯一。
const gid = `cpg${getCurrentInstance()?.uid ?? 0}`;

const clamped = computed(() => {
  const v = props.percent;
  if (v == null || !Number.isFinite(v)) return null;
  return Math.max(0, Math.min(100, v));
});
const empty = computed(() => clamped.value == null);
// 环由补间值驱动，所以 stroke-dashoffset 上不再挂 CSS transition：
// 两边一起动会在每一帧互相超前后一个，看着像环在数字背后拖。
const shownPct = useTweenNumber(clamped, 900);
const offset = computed(() => C * (1 - (shownPct.value ?? 0) / 100));

// 圆心读数：只有本来就是数字的 text 才补间（负载那一格写的是「运行流畅」，
// 给一个词补间只能逐帧插出「运〜流」这种鬼东西，或者直接 NaN）。
const numeric = computed(() => {
  const t = String(props.text ?? '').trim();
  if (!t) return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
});
const decimals = computed(() => {
  const m = /\.(\d+)/.exec(String(props.text ?? '').trim());
  return m ? m[1].length : 0;
});
const shownText = useTweenNumber(numeric, 900);
const display = computed(() =>
  numeric.value == null || shownText.value == null
    ? props.text
    : shownText.value.toFixed(decimals.value),
);

/** 环外侧的一格短刻度：把 warn/crit 这两条线画在环上，位置与百分比同一口径。 */
function tickAt(v: number | null | undefined) {
  if (v == null || !Number.isFinite(v) || v <= 0 || v >= 100) return null;
  const a = ((v / 100) * 360 - 90) * (Math.PI / 180);
  const r1 = RING + SW / 2 + 1.4;
  const r2 = r1 + 2.4;
  return {
    x1: 50 + r1 * Math.cos(a), y1: 50 + r1 * Math.sin(a),
    x2: 50 + r2 * Math.cos(a), y2: 50 + r2 * Math.sin(a),
  };
}
// 两条线挨得比刻度本身还近时只留要紧的那条：并排两道会读成「环上有个粗边」，
// 而读不出「85 和 95 是两档」的刻度比不画更容易骗人。
const ticks = computed(() => {
  const w = tickAt(props.warn);
  const c = tickAt(props.crit);
  if (w && c && Math.abs(Number(props.crit) - Number(props.warn)) < 4) return [{ g: c, cls: 'crit' }];
  const out: { g: NonNullable<typeof w>; cls: string }[] = [];
  if (w) out.push({ g: w, cls: 'warn' });
  if (c) out.push({ g: c, cls: 'crit' });
  return out;
});

const ariaText = computed(
  () => `${props.label} ${props.text}${props.unit}${props.detail ? `，${props.detail}` : ''}${props.note ? `，${props.note}` : ''}`,
);
</script>

<template>
  <div class="gauge" :class="`t-${tone}`" role="img" :aria-label="ariaText" :title="note || undefined">
    <span class="gauge-halo" :style="`--halo: ${color}`" aria-hidden="true"></span>
    <svg :width="size" :height="size" viewBox="0 0 100 100" aria-hidden="true">
      <defs>
        <!-- 1Panel 的进度色是从浅到深的一条渐变（#4c8ef1 → #005eeb），不是纯色 -->
        <linearGradient :id="gid" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0" :style="{ stopColor: `color-mix(in srgb, ${color} 55%, #ffffff)` }" />
          <stop offset="1" :style="{ stopColor: color }" />
        </linearGradient>
      </defs>
      <circle class="track" :cx="50" :cy="50" :r="RING" :stroke-width="SW" fill="none" />
      <circle
        class="prog" :class="{ dim: empty }"
        :cx="50" :cy="50" :r="RING" :stroke-width="SW" fill="none" :stroke="`url(#${gid})`"
        stroke-linecap="round" :stroke-dasharray="C" :stroke-dashoffset="offset"
        transform="rotate(-90 50 50)"
      />
      <line
        v-for="t in ticks" :key="t.cls" class="mark" :class="t.cls"
        :x1="t.g.x1" :y1="t.g.y1" :x2="t.g.x2" :y2="t.g.y2" stroke-width="1.1" stroke-linecap="round"
      />
    </svg>
    <div class="gauge-mid">
      <div class="gauge-num">{{ display }}<span v-if="unit" class="gauge-unit">{{ unit }}</span></div>
      <div class="gauge-label">{{ label }}</div>
    </div>
    <div class="gauge-detail">{{ detail || ' ' }}</div>
  </div>
</template>

<style scoped>
/* 圆心叠字：svg 与文字放在 grid 的同一格（'ring'），比 absolute + translate 少一处魔数 */
.gauge { position: relative; display: grid; place-items: center; grid-template-areas: 'ring' 'detail'; min-width: 0; }
.gauge > svg { grid-area: ring; flex-shrink: 0; max-width: 100%; height: auto; position: relative; z-index: 1; }
.track { stroke: var(--cp-ring-track); }
/* stroke 换色（越线那一刻）补 0.2s；dashoffset 由 rAF 逐帧给，不再叠一层 transition */
svg circle.prog { transition: stroke var(--dur) var(--ease); }
svg circle.dim { opacity: 0.35; }
svg line.mark { stroke: var(--cp-text-mute); opacity: 0.55; transition: opacity var(--dur) var(--ease), stroke var(--dur) var(--ease); }
svg line.mark.crit { opacity: 0.8; }
.gauge.t-warn svg line.mark.warn, .gauge.t-err svg line.mark.warn { stroke: var(--cp-warn); opacity: 0.9; }
.gauge.t-err svg line.mark.crit { stroke: var(--cp-err); opacity: 1; }
/* 越线的那一档给一层同色底光：一眼扫过去先看到"谁在发光"，再读到数字。
   要紧档缓慢呼吸（只动 opacity，不碰尺寸，不触发重排）。 */
/* 底光和 svg、圆心文字同占 'ring' 这一格（grid 同一格天然叠放），省掉 absolute + translate 那组魔数 */
.gauge-halo {
  grid-area: ring; width: 74%; aspect-ratio: 1; border-radius: 50%; z-index: 0;
  background: radial-gradient(circle, color-mix(in srgb, var(--halo) 16%, transparent), transparent 68%);
  opacity: 0; pointer-events: none; transition: opacity var(--dur) var(--ease);
}
.gauge.t-warn .gauge-halo { opacity: 0.9; }
.gauge.t-err .gauge-halo { opacity: 0.9; animation: cp-breathe 2.6s var(--ease) infinite; }
@keyframes cp-breathe { 0%, 100% { opacity: 0.45; } 50% { opacity: 1; } }
.gauge-mid { grid-area: ring; text-align: center; pointer-events: none; position: relative; z-index: 1; }
/* 1Panel 的圆心读数：整数 22px/500，小数与 % 14px，指标名 13px */
.gauge-num { font-size: 22px; font-weight: 500; line-height: 25px; color: var(--cp-text-strong); letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.gauge-unit { font-size: 14px; color: var(--cp-text-strong); margin-left: 1px; }
.gauge-label { font-size: 13px; color: var(--cp-text-dim); margin-top: 2px; transition: color var(--dur) var(--ease); }
/* 悬停只做颜色与底光，不做缩放：缩放会把同一个 grid 格里的 detail 一起推走，
   四个环一排时其中两个动、两个不动，比不动更难看。 */
.gauge:hover .gauge-label { color: var(--cp-text); }
.gauge:hover .gauge-halo { opacity: 1; }
.gauge-detail {
  grid-area: detail; text-align: center; font-size: var(--fs-xs); color: var(--cp-text-mute);
  width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
