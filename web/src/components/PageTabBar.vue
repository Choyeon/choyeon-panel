<script setup lang="ts">
/**
 * 1Panel 的 RouterButton（el-card.router_card 里的 el-radio-group）等价物：
 * 二级导航在内容卡**之外**单独一张卡，页签带 2px 描边、选中时描边变品牌色。
 * 与 .cp-seg 的区别正是这条描边——1Panel 的 app/host/monitor 二级导航全是这个形态。
 */
import { NCard } from 'naive-ui';

defineProps<{
  tabs: { key: string; label: string; badge?: boolean }[];
  value: string;
}>();
const emit = defineEmits<{ update: [key: string] }>();
</script>

<template>
  <NCard size="small" :bordered="true" :content-style="{ padding: '0' }" class="ptb-card">
    <div class="ptb">
      <div class="ptb-tabs" role="tablist">
        <button
          v-for="t in tabs" :key="t.key" type="button" role="tab"
          class="ptb-tab cp-press" :class="{ on: t.key === value }"
          :aria-selected="t.key === value" @click="emit('update', t.key)"
        >
          <span class="ptb-dot" :class="{ show: t.badge }" aria-hidden="true"></span>{{ t.label }}
        </button>
      </div>
      <div class="ptb-extra"><slot name="extra" /></div>
    </div>
  </NCard>
</template>

<style scoped>
.ptb { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); min-width: 0; padding: var(--space-2) var(--space-3); }
.ptb-tabs { display: flex; flex-wrap: wrap; gap: var(--space-2); min-width: 0; }
.ptb-tab {
  min-width: 100px; border: 2px solid transparent; border-radius: var(--radius-lg);
  background: none; color: var(--cp-text-dim); font: inherit; font-size: var(--fs-sm);
  padding: var(--space-2) var(--space-4); cursor: pointer; text-align: center;
  transition: color var(--dur) var(--ease), border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.ptb-tab:hover { color: var(--cp-brand); }
.ptb-tab.on { color: var(--cp-text); border-color: var(--cp-brand); background: var(--cp-bg-soft); font-weight: 550; }
.ptb-extra { display: flex; align-items: center; gap: var(--space-2); flex-shrink: 0; }
.ptb-dot { display: none; width: 6px; height: 6px; border-radius: 50%; background: var(--cp-err); margin-right: 5px; }
.ptb-dot.show { display: inline-block; }
@media (max-width: 767px) {
  .ptb { flex-wrap: wrap; }
  .ptb-tab { min-width: 84px; padding: 6px 10px; font-size: var(--fs-xs); }
}
</style>
