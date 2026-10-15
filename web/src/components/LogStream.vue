<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, nextTick, h, computed, watch } from 'vue';
import { NSpace, NInputNumber, NSwitch, NText, NIcon, NTooltip, useMessage } from 'naive-ui';
import { NButton } from '../ui';
import { logUrl, req } from '../api';
import { icon } from '../icons';
import { useCachedFetch } from '../composables/useCachedFetch';

const ico = (name: string, size = 13) => icon(name, size);

const props = defineProps<{ urlPath: string; height?: number }>();

const msg = useMessage();
const lines = ref<string[]>([]);
// 「一开始拉多少行」这两个数字都归服务端：默认值决定初值，上限决定输入框能填到几。
// null 表示"不传 lines，按服务端自己的默认来"。过去 200 与 2000 在这里各抄了一份，
// 服务端把上限一改，输入框还允许填到旧数字，请求被静悄悄截断，画面看着像日志少了。
const linesCount = ref<number | null>(null);
// optsError 要说出来：清单没取到时 :max 是 undefined，输入框回到"随便填"，
// 填超了的数字会被服务端悄悄截掉，画面看着就像日志少了一段。
const { data: logOpts, error: optsError } = useCachedFetch<{ lines_default: number; lines_max: number }>({
  key: () => 'system:logopts',
  loader: () => req('/system/log-options'),
  maxAge: 300000,
});
const maxLines = computed(() => Number(logOpts.value?.lines_max) || undefined);
watch(() => Number(logOpts.value?.lines_default), (v) => {
  // 只回填用户还没碰过的那个初值；已经在读的流不因为"清单到了"重连一次。
  if (v && linesCount.value === null) linesCount.value = v;
}, { immediate: true });
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
  es = new EventSource(logUrl(props.urlPath, linesCount.value ?? undefined));
  // 服务端每次连接都是 `journalctl -n <lines> -f`：先把整段尾部重打一遍再跟随。
  // 浏览器在断线（网络闪断、面板重启）后会自动重连，而旧画面还留着上一段的
  // 同样内容，onmessage 只会往后追加 —— 于是日志框里出现整段重复，反复断连就反复叠加。
  // 重连即新的一次完整尾部，先清屏再收才是对的。
  es.onopen = () => {
    streamState.value = 'live';
    if (lines.value.length) lines.value = [];
  };
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

// NInputNumber 每敲一个字符就 update:value 一次：输入 2000 会连着重连 4 次
// （每次都先清屏，中途还按半截数字去请求），看着像日志在抽搐。等手停下再重连。
let countTimer = 0;
function onCountChange() {
  if (countTimer) window.clearTimeout(countTimer);
  countTimer = window.setTimeout(() => {
    countTimer = 0;
    refresh();
  }, 500);
}

function toggleFollow() {
  following.value ? close() : open();
}

onMounted(open);
onBeforeUnmount(() => {
  if (countTimer) window.clearTimeout(countTimer);
  close();
});
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
      <NInputNumber v-model:value="linesCount" size="tiny" style="width: 92px; max-width: 100%" :min="10" :max="maxLines"
                    placeholder="默认" :input-props="{ 'aria-label': '初始加载日志行数' }" @update:value="onCountChange" />
      <NText v-if="optsError" class="st warn" style="font-size: var(--fs-xs)" role="status">
        <span class="dot warn"></span>行数上限没取到，超出服务端上限的部分会被截断
      </NText>
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
