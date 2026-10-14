<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { NCard, NSelect, NIcon, NText, NSpace } from 'naive-ui';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import LogStream from '../components/LogStream.vue';

// LogStream 在 mount 时打开 EventSource、unmount 时关闭：
// 切换 unit 用 :key 重建组件，比在组件里加 urlPath 响应逻辑更可靠。
const unit = ref('choyeon-panel.service');
const logHeight = ref(460);
onMounted(() => {
  logHeight.value = Math.max(360, window.innerHeight - 320);
});
const unitOptions = [
  { label: 'choyeon-panel（面板自身）', value: 'choyeon-panel.service' },
  { label: 'nginx（反向代理访问/错误日志）', value: 'nginx.service' },
  { label: 'postgresql（数据库）', value: 'postgresql.service' },
  { label: 'redis-server（缓存）', value: 'redis-server.service' },
];
</script>

<template>
  <div>
    <PageHeader title="面板日志" sub="journalctl 实时流（SSE）：排查面板与服务问题第一现场">
      <template #actions>
        <NSpace align="center" :size="8">
          <NIcon :component="icons.CubeOutline" :size="15" color="var(--cp-text-mute)" />
          <NSelect v-model:value="unit" size="small" style="width: 260px; max-width: 100%" :options="unitOptions" aria-label="选择日志来源服务" />
        </NSpace>
      </template>
    </PageHeader>

    <NCard size="small">
      <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
        日志按时间正序滚动，「暂停跟踪」可回看历史；切换服务会重新拉取所选行数。
      </NText>
      <LogStream :key="unit" :url-path="`/system/services/${unit}/logs`" :height="logHeight" />
    </NCard>
  </div>
</template>
