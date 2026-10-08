<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, nextTick, h, computed } from 'vue';
import { NButton, NSpace, NInputNumber, NSwitch, NText, NIcon, NTooltip, useMessage } from 'naive-ui';
import { logUrl } from '../api';
import { icons } from '../icons';

function ico(name: string, size = 13) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

const props = defineProps<{ urlPath: string; height?: number }>();

const msg = useMessage();
const lines = ref<string[]>([]);
const linesCount = ref(200);
const following = ref(true);
const autoScroll = ref(true);
const box = ref<HTMLElement | null>(null);
const streamState = ref<'live' | 'retry' | 'closed'>('live');
let es: EventSource | null = null;

const text = computed(() => lines.value.join('\n') || '(无日志)');

async function copyAll() {
  try {
    await navigator.clipboard.writeText(text.value);
    msg.success('日志已复制');
  } catch {
    msg.warning('浏览器拒绝了剪贴板访问，请手动复制');
  }
}

function open() {
  close();
  es = new EventSource(logUrl(props.urlPath, linesCount.value));
  es.onopen = () => (streamState.value = 'live');
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
  es.onerror = () => {
    // 只有 CONNECTING/OPEN 时浏览器才会自动重连。服务端返回 401（登录过期）、
    // 403 或非 SSE 响应时 readyState 直接变 CLOSED，再也不会重试；
    // 旧写法一律显示"重连中"，用户只能对着一个永远不动的提示干等。
    streamState.value = es && es.readyState === EventSource.CLOSED ? 'closed' : 'retry';
  };
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
    <NSpace align="center" :size="8" wrap style="margin-bottom: var(--space-2)">
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
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="tiny" circle quaternary
            aria-label="复制全部日志" title="复制全部日志"
            :icon="ico('CopyOutline')"
            @click="copyAll"
          />
        </template>
        复制全部日志
      </NTooltip>
      <NText depth="3" style="font-size: var(--fs-xs)">初始行数</NText>
      <NInputNumber v-model:value="linesCount" size="tiny" style="width: 92px" :min="10" :max="2000" :input-props="{ 'aria-label': '初始加载日志行数' }" @update:value="refresh" />
      <NText depth="3" id="ls-autoscroll-label" style="font-size: var(--fs-xs)">自动滚动</NText>
      <NSwitch v-model:value="autoScroll" size="small" aria-labelledby="ls-autoscroll-label" />
      <span class="st" style="font-size: var(--fs-xs)" :class="streamState === 'live' ? 'ok' : streamState === 'retry' ? 'warn' : 'err'">
        <span class="dot" :class="streamState === 'live' ? 'ok' : streamState === 'retry' ? 'warn' : 'err'"></span>
        {{ streamState === 'live' ? 'SSE 实时' : streamState === 'retry' ? 'SSE 重连中…' : 'SSE 已断开，点刷新重试' }}
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
