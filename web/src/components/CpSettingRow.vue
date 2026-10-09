<script setup lang="ts">
/**
 * 1Panel 设置页的核心范式：label 左置 150px，值用 **disabled 输入框**（一眼可读、
 * 防误改），右侧 #append 一个「设置」按钮打开抽屉；行下方 12px 说明（.cp-help）。
 * 能直接改的项（开关/单选）走 #control 插槽，change 即存，不再要「保存」大按钮。
 */
import { NFormItem, NInput } from 'naive-ui';
import { NButton } from '../ui';
import { icon } from '../icons';

const props = withDefaults(
  defineProps<{
    label: string;
    value?: string;
    placeholder?: string;
    help?: string;
    actionText?: string;
  }>(),
  { value: '', placeholder: '未设置', help: '', actionText: '设置' },
);
const emit = defineEmits<{ edit: [] }>();
</script>

<template>
  <NFormItem :label="props.label" class="cp-set-row">
    <div class="cp-set-main">
      <slot name="control">
        <!-- naive 的 NInput 没有 element-plus 的 #append 插槽，所以按 1Panel
             .append-button 的做法画：定宽 80px 的按钮贴在读框右边、压掉一条边框。 -->
        <div class="cp-set-ctl">
          <NInput :value="props.value" disabled :placeholder="props.placeholder" class="cp-set-read" />
          <NButton
            tertiary size="small" class="cp-set-btn" :icon="icon('SettingsOutline', 14)"
            :aria-label="`${props.actionText}：${props.label}`" @click="emit('edit')"
          >{{ props.actionText }}</NButton>
        </div>
      </slot>
      <span v-if="props.help" class="cp-help">{{ props.help }}</span>
    </div>
  </NFormItem>
</template>

<style scoped>
.cp-set-main { display: flex; flex-direction: column; min-width: 0; width: 100%; }
.cp-set-ctl { display: flex; align-items: stretch; width: 100%; }
.cp-set-ctl :deep(.cp-set-read) { flex: 1 1 auto; min-width: 0; }
.cp-set-btn {
  flex: 0 0 80px; width: 80px; margin-left: -1px;
  border-top-left-radius: 0; border-bottom-left-radius: 0; cursor: pointer;
}
.cp-set-read { border-top-right-radius: 0 !important; border-bottom-right-radius: 0 !important; }
</style>
