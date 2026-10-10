<script setup lang="ts">
/**
 * 1Panel 的 <Status> 组件等价物：el-tag size=small round effect=light + 一张
 * 全站通用的状态→语义色映射（照抄 status/index.vue 的原文分集，不做删减）。
 */
import { computed } from 'vue';
import { NTag } from 'naive-ui';

const props = defineProps<{ status: string; text?: string; operate?: boolean }>();

const SUCCESS = ['running', 'free', 'success', 'enable', 'enabled', 'done', 'healthy', 'executing', 'new', 'active', 'up', 'ok'];
// 'stopped' 从 ERROR 挪到 INFO：应用列表/应用详情的「已停止」用的是它，服务器管理里同一个
// 文案走的是 systemd 的 'inactive'，两边一红一蓝，同一状态在站内是两种严重程度。
// 真正异常的表达另有 failed / crashed / exited / dead，红色留给它们。
const ERROR = ['exceptional', 'disable', 'disabled', 'unhealthy', 'failed', 'crashed', 'lost', 'exited', 'notfound', 'inaccessible', 'dead'];
const WARN = ['paused', 'blocked', 'shutdown', 'pmsuspended', 'removing', 'deleted', 'conflict', 'partial', 'degraded'];
const INFO = ['duplicate', 'unexecuted', 'canceled', 'inactive', 'stopped', 'unknown'];
const LOADING = ['installing', 'building', 'restarting', 'upgrading', 'rebuilding', 'recreating', 'creating', 'starting', 'applying', 'uninstalling', 'downloading', 'packing', 'sending', 'waiting', 'loading', 'deploying'];

const type = computed(() => {
  const s = (props.status || '').toLowerCase();
  if (SUCCESS.includes(s)) return 'success';
  if (ERROR.includes(s)) return 'error';
  if (WARN.includes(s)) return 'warning';
  if (INFO.includes(s)) return 'info';
  if (s.includes('error') || s.includes('err')) return 'error';
  // 1Panel 的 status/index.vue 兜底给 primary（不是灰），未知状态也要显眼。
  return 'primary';
});
const loading = computed(() => LOADING.includes((props.status || '').toLowerCase()));
const label = computed(() => props.text || props.status || '-');
</script>

<template>
  <span class="cp-status" :class="{ 'is-disabled': operate === false }">
    <NTag size="small" round :bordered="false" :type="type">
      <span class="cp-status-inner">
        <span v-if="loading" class="cp-status-spin" aria-hidden="true"></span>{{ label }}
      </span>
    </NTag>
  </span>
</template>

<style scoped>
.cp-status { display: inline-flex; align-items: center; }
.cp-status-inner { display: inline-flex; align-items: center; gap: 5px; }
.cp-status-spin {
  width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid currentColor;
  border-top-color: transparent; display: inline-block; animation: cp-status-rot 0.8s linear infinite;
}
@keyframes cp-status-rot { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .cp-status-spin { animation-duration: 2.4s; }
}
/* 1Panel 的禁用态：整块降透明度并去色，同时 cursor 明确不许点 */
.cp-status.is-disabled :deep(.n-tag) { opacity: 0.55; filter: grayscale(1); }
.cp-status.is-disabled { cursor: not-allowed; }
</style>
