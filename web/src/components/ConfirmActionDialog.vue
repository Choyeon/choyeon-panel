<script setup lang="ts">
/**
 * 1Panel 的 OpDialog（components/del-dialog）等价物：标题 + 被操作对象名单 +
 * 一句后果 + 取消/确认。acceptParams 签名与 1Panel 一致，页面里不再现写弹窗。
 */
import { nextTick, ref, watch } from 'vue';
import { NModal, NAlert, NSpace, useMessage } from 'naive-ui';
import { NButton } from '../ui';

interface Params {
  title: string;
  names: string[];
  msg: string;
  api: () => Promise<any>;
  successMsg?: string;
  confirmText?: string;
}

const show = ref(false);
const loading = ref(false);
const p = ref<Params | null>(null);
const msg = useMessage();
const cancelBtn = ref<any>(null);
let returnFocus: HTMLElement | null = null;

function acceptParams(next: Params) {
  // 弹窗是 acceptParams() 程序化打开的，没有 trigger 元素：关掉后焦点会掉回 body，
  // 键盘用户得从页首重新 Tab。记下打开时的焦点，关闭时还回去。
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  p.value = next;
  show.value = true;
  // 破坏性操作默认停在「取消」上：误按 Enter 不会直接把东西删掉。
  nextTick(() => cancelBtn.value?.$el?.focus?.());
}

watch(show, (visible) => {
  if (!visible && returnFocus) {
    returnFocus.focus();
    returnFocus = null;
  }
});

async function run() {
  if (!p.value || loading.value) return;
  loading.value = true;
  try {
    await p.value.api();
    msg.success(p.value.successMsg || '操作已完成');
    show.value = false;
  } catch (e: any) {
    msg.error(String(e?.message || e || '操作失败'));
  } finally {
    loading.value = false;
  }
}

defineExpose({ acceptParams });
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :title="p?.title || ''"
    class="cp-opd"
    :style="{ width: '480px', maxWidth: '94vw' }"
    :aria-label="p?.title || '确认操作'"
    aria-describedby="cp-opd-desc"
  >
    <NAlert id="cp-opd-desc" type="warning" :bordered="false" show-icon role="alert">
      <p class="cp-opd-msg">{{ p?.msg }}</p>
      <ul v-if="p?.names?.length" class="cp-opd-names">
        <li v-for="n in p.names" :key="n">{{ n }}</li>
      </ul>
    </NAlert>
    <template #footer>
      <NSpace justify="end" :size="12">
        <NButton ref="cancelBtn" size="small" :disabled="loading" @click="show = false">取消</NButton>
        <!-- 破坏性确认统一用 error 色：以前和普通「保存」一样是 primary，眼睛分不出轻重。 -->
        <NButton size="small" type="error" :loading="loading" :disabled="loading" @click="run">{{ p?.confirmText || '确认' }}</NButton>
      </NSpace>
    </template>
  </NModal>
</template>

<style scoped>
/* 1Panel 的被操作对象名单就是 ul + li，长名字要能换行而不是撑破弹窗 */
.cp-opd-msg { margin: 0; font-size: var(--fs-sm); color: var(--cp-text); }
.cp-opd-names { margin: 6px 0 0; padding-left: 18px; font-size: var(--fs-sm); color: var(--cp-text-strong); word-wrap: break-word; }
</style>
