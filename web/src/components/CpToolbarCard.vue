<script setup lang="ts">
/**
 * 1Panel 的 LayoutContent 主卡等价物：工具行与表格在**同一张卡**内，
 * 左组放主按钮（gap 8px）、右组放筛选/搜索/刷新（gap 12px），标题 18px/400。
 */
import { NCard } from 'naive-ui';

defineProps<{ title?: string }>();
</script>

<template>
  <NCard size="small" :bordered="true" :content-style="{ padding: 'var(--space-4) var(--space-5)' }" class="cp-tool-card">
    <div class="cp-tool-head">
      <div class="cp-tool-left">
        <h2 v-if="title" class="cp-tool-title">{{ title }}</h2>
        <slot name="left" />
      </div>
      <div class="cp-tool-right"><slot name="right" /></div>
    </div>
    <div class="cp-tool-body"><slot /></div>
  </NCard>
</template>

<style scoped>
/* 标题与按钮同行是 1Panel 的常态：.content-container__title 18px/400，工具行 gap 8/12px */
.cp-tool-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; min-width: 0; }
.cp-tool-left { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; min-width: 0; }
.cp-tool-right { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.cp-tool-title { margin: 0 6px 0 0; font-size: var(--fs-title); font-weight: 400; line-height: var(--lh-title); color: var(--cp-text-strong); }
.cp-tool-body { margin-top: 10px; min-width: 0; }
</style>
