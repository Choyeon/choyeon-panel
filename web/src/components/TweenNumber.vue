<script setup lang="ts">
/**
 * 读数补间的最小载体：给一个数就逐帧爬过去。
 * 概览那一排计数（网站/应用/定时任务/未确认告警）每 30s 与后端对一次账，
 * 硬切的数字读起来像「页面自己抖了一下」，补间之后才是「涨了 3 个」。
 * v-for 里不能逐个调 composable（每次渲染都会新起一个 rAF），所以做成组件。
 */
import { computed } from 'vue';
import { useTweenNumber } from '../composables/useTween';

const props = withDefaults(defineProps<{ value: number | null | undefined; decimals?: number }>(), { decimals: 0 });
const shown = useTweenNumber(() => props.value, 700);
const text = computed(() => (shown.value == null ? '' : shown.value.toFixed(props.decimals)));
</script>

<template><span class="twn">{{ text }}</span></template>

<style scoped>
/* 补间过程中位数会变（9 → 10 → 100），不锁等宽数字就会左右抖。 */
.twn { font-variant-numeric: tabular-nums; }
</style>
