<script setup lang="ts">
/**
 * 1Panel 的 el-descriptions(border, :column=1) 等价物：naive-ui 没有 NDescriptions，
 * 而它的观感关键就在「label 格 40% 宽 + 浅底 + 单元格描边」，用 InfoList 冒充会差一层。
 */
import { computed, ref } from 'vue';
import { NButton } from '../ui';
import { icon } from '../icons';
import { useCopy } from '../composables/useCopy';

export interface DescItem {
  label: string;
  value?: string;
  mono?: boolean;
  /** 传入即在该行右侧渲染复制按钮（1Panel 只对端口/仓库/路径这类白名单字段这么做） */
  copy?: string;
  /** 密码类：默认遮罩，点眼睛才显示原文 */
  secret?: boolean;
  /** 值里允许出现超链接文案（如 http://…） */
  slot?: string;
}

const props = withDefaults(
  defineProps<{
    items: DescItem[];
    labelWidth?: string;
    direction?: 'horizontal' | 'vertical';
  }>(),
  { labelWidth: '40%', direction: 'horizontal' },
);

const emit = defineEmits<{ openSlot: [name: string] }>();
const shown = ref<Record<string, boolean>>({});
const grid = computed(() => `grid-template-columns: ${props.labelWidth} minmax(0, 1fr)`);

const { copy } = useCopy();
</script>

<template>
  <div class="dsc" :class="`dsc-${direction}`">
    <div
      v-for="(it, i) in items" :key="it.label"
      class="dsc-row" :class="{ 'dsc-v': direction === 'vertical' }"
      :style="direction === 'vertical' ? undefined : grid"
    >
      <div class="dsc-k">{{ it.label }}</div>
      <div class="dsc-v-cell">
        <template v-if="it.secret">
          <span class="dsc-text mono-on">{{ shown[it.label] ? it.value : '********' }}</span>
          <NButton
            text size="tiny" class="dsc-eye" :aria-label="shown[it.label] ? `隐藏 ${it.label}` : `显示 ${it.label}`"
            :icon="icon('EyeOutline', 14)" @click="shown[it.label] = !shown[it.label]"
          />
        </template>
        <span v-else class="dsc-text" :class="{ mono: it.mono }">{{ it.value || '-' }}</span>
        <NButton
          v-if="it.copy" text size="tiny" class="dsc-eye" :aria-label="`复制 ${it.label}`"
          :icon="icon('CopyOutline', 14)" @click="copy(it.copy)"
        />
        <NButton v-if="it.slot" text size="tiny" type="primary" class="dsc-eye" @click="emit('openSlot', it.slot)">
          查看
        </NButton>
        <slot v-if="it.slot" :name="it.slot" />
      </div>
    </div>
    <div v-if="!items.length" class="dsc-row dsc-empty">-</div>
  </div>
</template>

<style scoped>
.dsc {
  border: 1px solid var(--cp-border); border-radius: var(--radius); overflow: hidden;
  font-size: var(--fs-sm); background: var(--cp-bg-1);
}
.dsc-row { display: grid; min-width: 0; }
.dsc-row + .dsc-row { border-top: 1px solid var(--cp-border); }
.dsc-k {
  background: var(--cp-bg-soft); color: var(--cp-text-dim); font-size: var(--fs-xs);
  padding: var(--space-2) var(--space-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  display: flex; align-items: center;
}
.dsc-v-cell {
  padding: var(--space-2) var(--space-3); min-width: 0; display: flex; align-items: center; gap: 2px;
  border-left: 1px solid var(--cp-border);
}
.dsc-text { min-width: 0; word-break: break-all; white-space: normal; color: var(--cp-text); }
.dsc-text.mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
.dsc-eye { margin-left: 6px; flex-shrink: 0; }
/* 竖排（商店详情那种）：label 在上、值在下，单列 */
.dsc-v .dsc-row { grid-template-columns: minmax(0, 1fr); }
.dsc-v .dsc-v-cell { border-left: none; border-top: 1px solid var(--cp-border); }
.dsc-v .dsc-k { border-bottom: 0; }
.dsc-empty { color: var(--cp-text-mute); }
</style>
