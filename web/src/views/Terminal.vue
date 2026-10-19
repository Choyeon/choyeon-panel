<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { NIcon, NInput, NSpace, useMessage } from 'naive-ui';
import { NButton } from '../ui';
import '@xterm/xterm/css/xterm.css';
import { getToken } from '../api';
import { icons } from '../icons';
import { cssVar } from '../theme';
import PageHeader from '../components/PageHeader.vue';

const msg = useMessage();
const host = ref<HTMLElement | null>(null);
const connected = ref(false);
const disconnected = ref(false);
const cmd = ref('');
const histIdx = ref(-1);
// 常用命令从这里走输入框，不碰剪贴板/输入法：粘贴多行、中文、转义符都原样进 shell
function loadHist(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem('cp-term-hist') || '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}
const history = ref<string[]>(loadHist());

let term: Terminal | null = null;
let fit: FitAddon | null = null;
let ws: WebSocket | null = null;
let pingTimer: number | null = null;
let ro: ResizeObserver | null = null;
let mo: MutationObserver | null = null;
// 断线期间在终端里打的字以前是静默丢弃的：sendInput 返回 false 没人接，
// xterm 又不回显（没有本地 echo，字符要 PTY 送回来才画得出来），
// 用户看到的是"敲了没反应"，连"为什么没反应"都要靠猜。每个断线片段只点名一次。
let inputLostNoticed = false;

function termTheme() {
  return {
    background: cssVar('--cp-code-bg', '#0c0c10'),
    foreground: cssVar('--cp-code-text', '#d4d4d4'),
    cursor: cssVar('--cp-brand', '#4f7cff'),
  };
}

function applyTheme() {
  if (term) term.options.theme = termTheme();
}

/** 发送原始输入；返回 false 表示 socket 不可用，调用方要据此提示而不是静默丢弃 */
function sendInput(data: string): boolean {
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({ d: 'input', data }));
    return true;
  }
  return false;
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/terminal?token=${encodeURIComponent(getToken())}`);
  ws.onopen = () => {
    connected.value = true;
    disconnected.value = false;
    inputLostNoticed = false;
    term!.writeln('\x1b[32m已连接服务器终端（root shell，请谨慎操作）。下方输入框可直接粘贴并执行命令。\x1b[0m\r\n');
    ws!.send(JSON.stringify({ d: 'resize', cols: term!.cols, rows: term!.rows }));
  };
  ws.onmessage = (e) => {
    try {
      const m = JSON.parse(e.data);
      if (m.d === 'out') term!.write(m.data);
    } catch { /* 忽略非 JSON 帧 */ }
  };
  ws.onclose = (e) => {
    connected.value = false;
    disconnected.value = true;
    // 后端用 close 码区分三种断开原因，不区分就只能统一提示"连接已断开"，
    // 用户会反复点重连，而实际要做的分别是重新登录 / 换管理员账号 / 等会话释放。
    if (e.code === 4401) msg.error('登录已过期或无效，请重新登录后再打开终端');
    else if (e.code === 4403) msg.error('当前账号无终端权限（仅管理员可用）');
    else if (e.code === 4498) msg.error('访问来源与面板地址不一致，终端连接被拒绝');
    else if (e.code === 4503) msg.error('终端会话数已达上限，请稍后再试');
    term?.writeln('\r\n\x1b[31m连接已断开。点击右上角「重新连接」恢复终端。\x1b[0m');
  };
}

function reconnect() {
  // 先摘掉旧 socket 的回调再 close：onclose 是异步派发的，新连接可能已经 onopen
  // 把状态置成"已连接"，旧 socket 随后回调一句"连接已断开"就把界面盖回错误态，
  // 终端看着断、其实还能打字。
  if (ws) {
    ws.onopen = ws.onmessage = ws.onclose = null;
    ws.close();
  }
  ws = null;
  term?.clear();
  connect();
}

function submitCmd() {
  const c = cmd.value;
  if (!c) return;
  // 断开时 sendInput 是空操作：命令既没执行，也不该写进历史，
  // 否则历史里全是"看着跑过其实没跑"的条目，翻出来一改就误执行。
  if (!sendInput(c + '\n')) {
    msg.warning('终端未连接，命令未执行');
    return;
  }
  history.value = [c, ...history.value.filter((h) => h !== c)].slice(0, 50);
  localStorage.setItem('cp-term-hist', JSON.stringify(history.value));
  histIdx.value = -1;
  cmd.value = '';
  term?.focus();
}
function histMove(dir: -1 | 1) {
  if (!history.value.length) return;
  histIdx.value = Math.max(-1, Math.min(history.value.length - 1, histIdx.value + dir));
  cmd.value = histIdx.value < 0 ? '' : history.value[histIdx.value];
}
async function pasteToInput() {
  // 直接粘贴进 xterm 时，浏览器可能把多行内容拆散、把括号粘贴模式序列丢给 shell，
  // 表现为"粘贴后命令串在一起执行"。走输入框就能整段可控地提交。
  try {
    const t = await navigator.clipboard.readText();
    cmd.value = t;
    msg.success('已读取剪贴板，可编辑后回车执行');
  } catch {
    msg.warning('浏览器拒绝了剪贴板访问，请手动粘贴到输入框');
  }
}

onMounted(() => {
  term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "'JetBrains Mono','Fira Code',monospace",
    theme: termTheme(),
    // 中文/全角字符按 2 列宽渲染，否则中文输出会出现字符重叠错位
    unicodeVersion: '11',
  } as any);
  fit = new FitAddon();
  term.loadAddon(fit);
  term.open(host.value!);
  try {
    fit.fit();
  } catch { /* 容器尺寸为 0 时忽略 */ }
  term.onData((data) => {
    if (sendInput(data)) return;
    // socket 不可用：xterm 没有本地回显，不自己写回去的话这些字符就凭空消失。
    // 回显让用户看见自己敲了什么，再在这个断线片段里点名一次为什么不会执行。
    term?.write(data);
    if (!inputLostNoticed) {
      inputLostNoticed = true;
      term?.writeln('\r\n\x1b[33m（终端未连接：以上按键没有发往服务器，点右上角「重新连接」后需重新输入。）\x1b[0m');
    }
  });
  term.onResize(({ cols, rows }) => {
    if (ws?.readyState === 1) ws.send(JSON.stringify({ d: 'resize', cols, rows }));
  });
  // Ctrl/Cmd+V：读剪贴板整段原样写入（xterm 默认粘贴走事件分块，多行/转义容易乱）
  term.attachCustomKeyEventHandler((e) => {
    if (e.type === 'keydown' && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') {
      navigator.clipboard.readText().then((t) => { if (t && !sendInput(t)) msg.warning('终端未连接，粘贴内容未发送'); }).catch(() => msg.warning('剪贴板不可用，请改用下方输入框'));
      return false;
    }
    return true;
  });
  term.focus();
  connect();

  if (window.ResizeObserver && host.value) {
    ro = new ResizeObserver(() => {
      try { fit?.fit(); } catch { /* ignore */ }
    });
    ro.observe(host.value);
  }
  mo = new MutationObserver(applyTheme);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  pingTimer = window.setInterval(() => {
    if (ws?.readyState === 1) ws.send(JSON.stringify({ d: 'ping' }));
  }, 25000);
});

onBeforeUnmount(() => {
  if (pingTimer) clearInterval(pingTimer);
  ro?.disconnect();
  mo?.disconnect();
  ws?.close();
  term?.dispose();
});
</script>

<template>
  <div>
    <PageHeader title="网页终端" sub="root shell · 所有操作即时生效且无二次确认，请谨慎执行">
      <template #actions>
        <span class="st" :class="connected ? 'ok' : disconnected ? 'err' : 'idle'">
          <span class="dot" :class="connected ? 'ok' : disconnected ? 'err' : 'idle'"></span>
          {{ connected ? '已连接' : disconnected ? '已断开' : '连接中' }}
        </span>
        <NButton size="small" tertiary aria-label="重新连接终端" @click="reconnect">
          <template #icon><NIcon :component="icons.SyncOutline" /></template>
          重新连接
        </NButton>
      </template>
    </PageHeader>

    <div class="term-wrap cp-tbl">
      <div class="term-bar">
        <span class="tdot" aria-hidden="true" style="background: var(--cp-err)"></span>
        <span class="tdot" aria-hidden="true" style="background: var(--cp-warn)"></span>
        <span class="tdot" aria-hidden="true" style="background: var(--cp-ok)"></span>
        <NSpace align="center" :size="6" style="margin-left: var(--space-3); color: var(--cp-text-dim); font-size: var(--fs-xs)">
          <NIcon :component="icons.TerminalOutline" :size="13" /> root@choyeon — 面板终端
        </NSpace>
      </div>
      <div ref="host" class="term-host" aria-label="终端输出区" role="region"></div>
      <div class="term-cmd">
        <NInput
          v-model:value="cmd"
          placeholder="粘贴或输入命令，回车提交（↑ ↓ 翻历史）"
          title="粘贴/输入命令后回车提交到终端，中文与多行内容不会乱码"
          :input-props="{ class: 'mono', 'aria-label': '终端命令输入框' }"
          @keyup.enter="submitCmd"
          @keydown.up.prevent="histMove(-1)"
          @keydown.down.prevent="histMove(1)"
        >
          <template #prefix><NIcon :component="icons.TerminalOutline" :size="14" /></template>
          <template #suffix>
            <NSpace :size="4">
              <NButton size="tiny" quaternary @click="pasteToInput">
                <template #icon><NIcon :component="icons.CopyOutline" :size="13" /></template>
                粘贴
              </NButton>
              <NButton size="tiny" type="primary" :disabled="!cmd || !connected" @click="submitCmd">执行</NButton>
            </NSpace>
          </template>
        </NInput>
      </div>
    </div>
  </div>
</template>

<style scoped>
.term-wrap {
  border: 1px solid var(--cp-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--cp-code-bg);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
}
.term-bar {
  display: flex;
  align-items: center;
  padding: var(--space-2) var(--space-4);
  background: var(--cp-hover);
  border-bottom: 1px solid var(--cp-border);
}
.tdot { width: 11px; height: 11px; border-radius: 50%; margin-right: var(--space-2); display: inline-block; }
.term-host {
  /* 外壳用 .cp-tbl 算好的 --table-h，这里再扣掉标题栏（36）与命令输入行（58）+ 边框 */
  height: calc(var(--table-h) - 96px);
  min-height: 280px;
  padding: var(--space-3) var(--space-2) var(--space-3) var(--space-3);
}
.term-cmd {
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--cp-border);
  background: var(--cp-bg-1);
}
</style>
