<script setup lang="ts">
/**
 * 1Panel 的 OpDialog（components/del-dialog）等价物：标题 + 被操作对象名单 +
 * 一句后果 + 取消/确认。acceptParams 签名与 1Panel 一致，页面里不再现写弹窗。
 */
import { ref } from 'vue';
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

function acceptParams(next: Params) {
  p.value = next;
  show.value = true;
}

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
  <NModal v-model:show="show" preset="card" :title="p?.title || ''" class="cp-opd" :style="{ width: '480px', maxWidth: '92vw' }">
    <NAlert type="warning" :bordered="false" show-icon>
      <p class="cp-opd-msg">{{ p?.msg }}</p>
      <ul v-if="p?.names?.length" class="cp-opd-names">
        <li v-for="n in p.names" :key="n">{{ n }}</li>
      </ul>
    </NAlert>
    <template #footer>
      <NSpace justify="end" :size="12">
        <NButton size="small" :disabled="loading" @click="show = false">取消</NButton>
        <NButton size="small" type="primary" :loading="loading" :disabled="loading" @click="run">{{ p?.confirmText || '确认' }}</NButton>
      </NSpace>
    </template>
  </NModal>
</template>

<style scoped>
/* 1Panel 的被操作对象名单就是 ul + li，长名字要能换行而不是撑破弹窗 */
.cp-opd-msg { margin: 0; font-size: var(--fs-sm); color: var(--cp-text); }
.cp-opd-names { margin: 6px 0 0; padding-left: 18px; font-size: var(--fs-sm); color: var(--cp-text-strong); word-wrap: break-word; }
</style>
