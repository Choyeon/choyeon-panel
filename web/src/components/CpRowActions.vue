<script setup lang="ts">
/**
 * 1Panel 表格操作列（FuTableOperationActions）等价物：文字按钮 + 5px gap、
 * 前 ellipsis 个平铺、其余收进「更多」。它**不画竖向分隔线**，现版就是纯 gap。
 */
import { computed } from 'vue';
import { NDropdown } from 'naive-ui';
import { NButton } from '../ui';

export interface RowAction {
  label: string;
  run: () => void;
  danger?: boolean;
  disabled?: boolean;
}

const props = withDefaults(defineProps<{ buttons: RowAction[]; ellipsis?: number }>(), { ellipsis: 2 });

const fine = typeof window !== 'undefined'
  && !!window.matchMedia?.('(hover: hover) and (pointer: fine)').matches;
const inline = computed(() => props.buttons.slice(0, props.ellipsis));
const rest = computed(() => props.buttons.slice(props.ellipsis));
const restOptions = computed(() =>
  rest.value.map((b, i) => ({ label: b.label, key: i, disabled: b.disabled })),
);
</script>

<template>
  <div class="cp-ops">
    <NButton
      v-for="b in inline" :key="b.label" text size="small" class="cp-op"
      :type="b.danger ? 'error' : 'primary'" :disabled="b.disabled" @click="b.run"
    >{{ b.label }}</NButton>
    <!-- 动作只能挂在 NDropdown 的 select 上：naive-ui 会把选项对象上多写的
         onClick 静默丢掉（review_fixes 里那条回归就是它）。字段改名成 run，
         就是为了让"这形状不能直接喂给 NDropdown"这件事在类型上看得出来。 -->
    <NDropdown
      v-if="rest.length" :trigger="fine ? 'hover' : 'click'" :options="restOptions"
      :on-select="(i: number) => rest[i]?.run()"
    >
      <NButton text size="small" class="cp-op" :aria-label="`更多操作（${rest.length} 项）`">更多</NButton>
    </NDropdown>
  </div>
</template>

<style scoped>
.cp-ops { display: inline-flex; align-items: center; justify-content: center; gap: 5px; flex-wrap: wrap; }
.cp-op { padding-inline: 0; white-space: nowrap; }
</style>
