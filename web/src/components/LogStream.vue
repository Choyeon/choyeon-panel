<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, nextTick, h } from 'vue';
import { NButton, NSpace, NInputNumber, NSwitch, NText, NIcon } from 'naive-ui';
import { logUrl } from '../api';
import { icons } from '../icons';

function ico(name: string, size = 13) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

const props = defineProps<{ urlPath: string }>();

const lines = ref<string[]>([]);
const linesCount = ref(200);
const follow = ref(true);
const autoScroll = ref(true);
const box = ref<HTMLElement | null>(null);
const streamLive = ref(true);
let es: EventSource | null = null;

function open() {
  close();
  es = new EventSource(logUrl(props.urlPath, linesCount.value));
  es.onopen = () => (streamLive.value = true);
  es.onmessage = async (e) => {
    lines.value.push(JSON.parse(e.data));
    if (lines.value.length > 3000) lines.value.splice(0, 1000);
    if (autoScroll.value) {
      await nextTick();
      if (box.value) box.value.scrollTop = box.value.scrollHeight;
    }
  };
  es.onerror = () => (streamLive.value = false); // EventSource 会自动重连
}
function close() {
  es?.close();
  es = null;
}
function refresh() {
  lines.value = [];
  open();
}

onMounted(open);
onBeforeUnmount(close);
</script>

<template>
  <div>
    <NSpace align="center" :size="10" style="margin-bottom: 8px">
      <NButton size="tiny" tertiary :icon="ico('RefreshOutline')" @click="refresh">刷新</NButton>
      <NButton size="tiny" :type="follow ? 'warning' : 'success'" tertiary :icon="ico(follow ? 'PauseOutline' : 'PlayOutline')" @click="follow ? close() : open()">
        {{ follow ? '暂停跟踪' : '继续跟踪' }}
      </NButton>
      <NText depth="3" style="font-size: 12px">初始行数</NText>
      <NInputNumber v-model:value="linesCount" size="tiny" :style="{ width: '92px' }" :min="10" :max="2000" @update:value="refresh" />
      <NText depth="3" style="font-size: 12px">自动滚动</NText>
      <NSwitch v-model:value="autoScroll" size="small" />
      <span class="st" style="font-size: 12px" :style="{ color: streamLive ? undefined : '#f5616c' }">
        <span class="dot" :class="streamLive ? 'ok' : 'err'"></span>{{ streamLive ? 'SSE 实时' : 'SSE 重连中…' }}
      </span>
    </NSpace>
    <div ref="box" class="log-view" style="height: 420px">{{ lines.join('\n') || '(无日志)' }}</div>
  </div>
</template>
