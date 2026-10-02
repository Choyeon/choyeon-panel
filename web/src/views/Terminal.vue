<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { NButton, NTag, NIcon, NSpace } from 'naive-ui';
import '@xterm/xterm/css/xterm.css';
import { getToken } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';

const host = ref<HTMLElement | null>(null);
const connected = ref(false);
const disconnected = ref(false);
let term: Terminal | null = null;
let fit: FitAddon | null = null;
let ws: WebSocket | null = null;
let pingTimer: number | null = null;
let onWinResize: (() => void) | null = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/terminal?token=${encodeURIComponent(getToken())}`);
  ws.onopen = () => {
    connected.value = true;
    disconnected.value = false;
    term!.writeln('\x1b[32m已连接服务器终端（root shell，请谨慎操作）\x1b[0m\r\n');
    ws!.send(JSON.stringify({ d: 'resize', cols: term!.cols, rows: term!.rows }));
  };
  ws.onmessage = (e) => {
    try {
      const m = JSON.parse(e.data);
      if (m.d === 'out') term!.write(m.data);
    } catch {}
  };
  ws.onclose = () => {
    connected.value = false;
    disconnected.value = true;
    term?.writeln('\r\n\x1b[31m连接已断开。点击右上角「重新连接」恢复终端。\x1b[0m');
  };
}
function reconnect() {
  ws?.close();
  term?.clear();
  connect();
}

onMounted(() => {
  term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "'JetBrains Mono','Fira Code',monospace",
    theme: { background: '#0c0c10', foreground: '#d4d4d4', cursor: '#4f7cff' },
  });
  fit = new FitAddon();
  term.loadAddon(fit);
  term.open(host.value!);
  fit.fit();
  term.onData((d) => {
    if (ws?.readyState === 1) ws.send(JSON.stringify({ d: 'input', data: d }));
  });
  term.onResize(({ cols, rows }) => {
    if (ws?.readyState === 1) ws.send(JSON.stringify({ d: 'resize', cols, rows }));
  });
  connect();
  onWinResize = () => fit?.fit();
  window.addEventListener('resize', onWinResize);
  pingTimer = window.setInterval(() => {
    if (ws?.readyState === 1) ws.send(JSON.stringify({ d: 'ping' }));
  }, 25000);
});

onBeforeUnmount(() => {
  if (pingTimer) clearInterval(pingTimer);
  if (onWinResize) window.removeEventListener('resize', onWinResize);
  ws?.close();
  term?.dispose();
});
</script>

<template>
  <div>
    <PageHeader title="网页终端" sub="root shell · 所有操作即时生效且无二次确认，请谨慎执行">
      <template #actions>
        <NTag round :bordered="false" size="small" :type="connected ? 'success' : 'error'">
          <template #icon><span class="dot" :class="connected ? 'ok' : 'err'"></span></template>
          {{ connected ? '已连接' : disconnected ? '已断开' : '连接中' }}
        </NTag>
        <NButton size="small" tertiary @click="reconnect">
          <template #icon><NIcon :component="icons.SyncOutline" /></template>
          重新连接
        </NButton>
      </template>
    </PageHeader>

    <div class="term-wrap">
      <div class="term-bar">
        <span class="tdot" style="background: #f5616c"></span>
        <span class="tdot" style="background: #f5a623"></span>
        <span class="tdot" style="background: #34c77b"></span>
        <NSpace align="center" :size="6" style="margin-left: 10px; color: #80808c; font-size: 12px">
          <NIcon :component="icons.TerminalOutline" :size="13" /> root@choyeon — 面板终端
        </NSpace>
      </div>
      <div ref="host" class="term-host"></div>
    </div>
  </div>
</template>

<style scoped>
.term-wrap {
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
  background: #0c0c10;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
}
.term-bar {
  display: flex;
  align-items: center;
  padding: 9px 14px;
  background: rgba(255, 255, 255, 0.04);
  border-bottom: 1px solid var(--line);
}
.tdot { width: 11px; height: 11px; border-radius: 50%; margin-right: 6px; display: inline-block; }
.term-host { height: calc(100vh - 260px); min-height: 380px; padding: 10px 6px 10px 12px; }
</style>
