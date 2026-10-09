<script setup lang="ts">
/** 1Panel 的 copy-button：值后面跟一个 link 型小图标（ml-1.5 ≈ 6px），点击复制。 */
import { NButton } from '../ui';
import { icon } from '../icons';

const props = defineProps<{ text: string; label?: string }>();

async function go() {
  try {
    await navigator.clipboard.writeText(props.text);
  } catch {
    /* 非 https 下剪贴板不可用，值本身已显示在页面上，不额外打断用户 */
  }
}
</script>

<template>
  <NButton
    text size="tiny" class="cp-copy" :icon="icon('CopyOutline', 14)"
    :aria-label="props.label || '复制到剪贴板'" @click="go"
  />
</template>

<style scoped>
.cp-copy { margin-left: 6px; flex-shrink: 0; vertical-align: middle; }
</style>
