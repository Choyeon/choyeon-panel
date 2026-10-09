<script setup lang="ts">
import { computed, ref } from 'vue';

/**
 * 递归 JSON 树：字典/数组按层级展开，而不是 JSON.stringify 挤成一行。
 * 表格里 180px 宽的单元格放不下，所以详细数据一律在行抽屉里用它渲染。
 */
defineOptions({ name: 'JsonView' });

const props = withDefaults(
  defineProps<{ value?: any; label?: string; depth?: number; startOpen?: boolean }>(),
  { label: undefined, depth: 0, startOpen: undefined },
);

const isBranch = computed(() => props.value !== null && typeof props.value === 'object');
const entries = computed<Array<[string, any]>>(() => {
  const v = props.value;
  if (!isBranch.value) return [];
  return Array.isArray(v) ? v.map((x, i) => [String(i), x] as [string, any]) : Object.entries(v);
});
// 只有根节点自动展开一层，深层默认收起：一张 20 字段的 jsonb 全展开会把抽屉撑爆
const expanded = ref(props.startOpen ?? props.depth < 1);

function kindOf(v: any) {
  if (v === null || v === undefined) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}
function textOf(v: any) {
  return typeof v === 'string' ? v : String(v);
}
const summary = computed(() =>
  Array.isArray(props.value) ? `Array(${entries.value.length})` : `{ ${entries.value.length} 项 }`,
);
const bracket = computed(() => (Array.isArray(props.value) ? '[' : '{'));
</script>

<template>
  <div class="jv">
    <div class="jv-line">
      <button
        v-if="isBranch" type="button" class="jv-caret"
        :aria-expanded="expanded" :aria-label="expanded ? '收起' : '展开'"
        @click="expanded = !expanded"
      >{{ expanded ? '▾' : '▸' }}</button>
      <span v-else class="jv-caret" aria-hidden="true">·</span>
      <span v-if="label !== undefined" class="jv-key">{{ label }}</span>
      <span v-if="label !== undefined && !isBranch" class="jv-sep">:</span>
      <template v-if="isBranch">
        <button
          v-if="!expanded" type="button" class="jv-sum" :aria-label="`展开 ${label ?? '内容'}`"
          @click="expanded = true"
        >{{ summary }}</button>
        <span v-else class="jv-punc">{{ bracket }}</span>
      </template>
      <span v-else class="jv-val" :class="'k-' + kindOf(value)">{{ textOf(value) }}</span>
    </div>
    <div v-if="isBranch && expanded" class="jv-kids">
      <JsonView
        v-for="[k, v] in entries" :key="k"
        :label="k" :value="v" :depth="depth + 1" :start-open="startOpen"
      />
      <div v-if="!entries.length" class="jv-empty">（空）</div>
    </div>
    <div v-if="isBranch && expanded" class="jv-punc">{{ Array.isArray(value) ? ']' : '}' }}</div>
  </div>
</template>

<style scoped>
.jv { font-family: var(--font-mono, ui-monospace, monospace); font-size: var(--fs-xs); line-height: 1.7; }
.jv-line { display: flex; align-items: baseline; gap: 4px; flex-wrap: wrap; }
.jv-caret {
  border: 0; background: none; padding: 0 2px; cursor: pointer; color: var(--cp-text-mute);
  font-size: var(--fs-2xs); line-height: 1;
}
.jv-key { color: var(--cp-brand-soft, #4f7cff); word-break: break-all; }
.jv-sep { color: var(--cp-text-mute); }
.jv-sum, .jv-punc { color: var(--cp-text-mute); }
.jv-sum { border: 0; background: none; padding: 0; cursor: pointer; font: inherit; }
.jv-punc { cursor: default; }
.jv-kids { padding-left: 14px; margin-left: 3px; border-left: 1px dashed var(--cp-border, rgba(128, 128, 128, 0.22)); }
.jv-empty { color: var(--cp-text-mute); font-size: var(--fs-2xs); padding-left: 18px; }
.jv-val { word-break: break-all; white-space: pre-wrap; color: var(--cp-text); }
.k-string { color: var(--cp-ok, #2f9e6b); }
.k-number { color: var(--cp-brand-soft, #4f7cff); }
.k-boolean { color: var(--cp-warn, #c98a00); }
.k-null { color: var(--cp-text-mute); font-style: italic; }
</style>
