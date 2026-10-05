<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, nextTick, h, computed } from 'vue';
import { NButton, NSpace, NInputNumber, NSwitch, NText, NIcon } from 'naive-ui';
import { logUrl } from '../api';
import { icons } from '../icons';

function ico(name: string, size = 13) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

const props = defineProps<{ urlPath: string; height?: number }>();

const lines = ref<string[]>([]);
const linesCount = ref(200);
const following = ref(true);
const autoScroll = ref(true);
const box = ref<HTMLElement | null>(null);
const streamLive = ref(true);
let es: EventSource | null = null;

const text = computed(() => lines.value.join('\n') || '(无日志)');

async function copyAll() {
  try {
    await navigator.clipboard.writeText(text.value);
  } catch {
    /* 非安全上下文下剪贴板不可用，忽略 */
  }
}

function open() {
  close();
  es = new EventSource(logUrl(props.urlPath, linesCount.value));
  es.onopen = () => (streamLive.value = true);
  es.onmessage = async (e) => {
    try {
      lines.value.push(JSON.parse(e.data));
    } catch {
      lines.value.push(e.data);
    }
    if (lines.value.length > 3000) lines.value.splice(0, 1000);
    if (autoScroll.value) {
      await nextTick();
      if (box.value) box.value.scrollTop = box.value.scrollHeight;
    }
  };
  es.onerror = () => (streamLive.value = false); // EventSource 会自动重连
  following.value = true;
}
function close() {
  es?.close();
  es = null;
  following.value = false;
}
function refresh() {
  lines.value = [];
  open();
}
function toggleFollow() {
  following.value ? close() : open();
}

onMounted(open);
onBeforeUnmount(close);
</script>

<template>
  <div>
    <NSpace align="center" :size="10" wrap style="margin-bottom: 8px">
      <NButton size="tiny" tertiary :icon="ico('RefreshOutline')" @click="refresh">刷新</NButton>
      <NButton
        size="tiny"
        :type="following ? 'warning' : 'success'"
        tertiary
        :icon="ico(following ? 'PauseOutline' : 'PlayOutline')"
        @click="toggleFollow"
      >
        {{ following ? '暂停跟踪' : '继续跟踪' }}
      </NButton>
      <NButton size="tiny" quaternary :icon="ico('CopyOutline')" @click="copyAll">复制</NButton>
      <NText depth="3" style="font-size: 12px">初始行数</NText>
      <NInputNumber v-model:value="linesCount" size="tiny" style="width: 92px" :min="10" :max="2000" @update:value="refresh" />
      <NText depth="3" style="font-size: 12px">自动滚动</NText>
      <NSwitch v-model:value="autoScroll" size="small" />
      <span class="st" style="font-size: 12px" :class="streamLive ? 'ok' : 'err'">
        <span class="dot" :class="streamLive ? 'ok' : 'err'"></span>{{ streamLive ? 'SSE 实时' : 'SSE 重连中…' }}
      </span>
    </NSpace>
    <div
      ref="box"
      class="log-view"
      :style="{ height: `${props.height || 420}px` }"
      role="log"
      aria-live="polite"
      aria-label="实时日志"
    >{{ text }}</div>
  </div>
</template>
