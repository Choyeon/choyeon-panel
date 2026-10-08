<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, h } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NCard, NTabs, NTabPane, NSpace, NButton, NText, NTag, NInput, NDynamicInput,
  useMessage, NPopconfirm, NIcon, NForm, NFormItem, NSkeleton, NGrid, NGridItem, NSelect, NAlert,
} from 'naive-ui';
import { api } from '../api';
import { icons } from '../icons';
import LogStream from '../components/LogStream.vue';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const route = useRoute();
const router = useRouter();
const msg = useMessage();
const id = Number(route.params.id);
const app = ref<any>(null);
const env = ref<{ k: string; v: string }[]>([]);
const edit = ref<any>({});
const deps = ref<any[]>([]);
const depLog = ref<any>(null);
// 延迟刷新不能拖过页面卸载：那是一次没人看的请求，失败还会把错误 toast 打到新页面上。
const timers: number[] = [];
function later(fn: () => void, ms: number) {
  timers.push(window.setTimeout(fn, ms));
}
onBeforeUnmount(() => timers.forEach(clearTimeout));
const saving = ref(false);
const acting = ref('');
const loadErr = ref('');
const sslEmail = ref('');

const ngx = ref<any>(null);
const ngxFile = ref<any>(null);
const ngxContent = ref('');
const ngxSaving = ref(false);
const bodySizeVal = ref('50m');
const pgOptions = ref<any[]>([]);
const dbSel = ref<string[]>([]);

const unitInfo = ref<any>(null);
const unitContent = ref('');
const unitSaving = ref(false);
const proc = ref<any>(null);

async function loadUnit() {
  try {
    unitInfo.value = await api.appUnit(id);
    unitContent.value = unitInfo.value.content || unitInfo.value.defaultTemplate;
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function saveUnitFile() {
  unitSaving.value = true;
  try {
    await api.saveUnit(id, unitContent.value);
    msg.success('已保存并通过 systemd-analyze verify，重启服务后生效');
    await loadUnit();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    unitSaving.value = false;
  }
}
function restoreDefaultTpl() {
  unitContent.value = unitInfo.value?.defaultTemplate || '';
}
async function loadProc() {
  try {
    proc.value = await api.appProc(id);
  } catch {}
}
function fmtMem(n: number | null) {
  if (n == null) return '—';
  if (n > 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
  if (n > 1048576) return Math.round(n / 1048576) + ' MB';
  return Math.round(n / 1024) + ' KB';
}

async function loadNginx() {
  try {
    ngx.value = await api.appNginx(id);
    if (ngx.value.configs?.length) selConfig(ngx.value.configs[0]);
  } catch (e: any) {
    msg.error(e.message);
  }
}
function selConfig(c: any) {
  ngxFile.value = c;
  ngxContent.value = c.content;
}
async function saveNginx() {
  ngxSaving.value = true;
  try {
    await api.saveNginx(id, ngxFile.value.file, ngxContent.value);
    msg.success('已保存并通过 nginx -t 校验');
    await loadNginx();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    ngxSaving.value = false;
  }
}
async function quick(kind: string) {
  try {
    await api.quickNginx(id, ngxFile.value.file, kind, kind === 'body' ? bodySizeVal.value : undefined);
    msg.success('快捷配置已生效');
    await loadNginx();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function loadPg() {
  try {
    const dbs = await api.pgDbs();
    if (Array.isArray(dbs)) pgOptions.value = dbs.map((d: any) => ({ label: d.name, value: d.name }));
  } catch {}
}

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

async function load() {
  loadErr.value = '';
  try {
    const data = await api.app(id);
    if (!data) {
      loadErr.value = '未找到该应用，可能已被删除';
      return;
    }
    app.value = data;
    edit.value = { ...app.value, port: app.value.port ? String(app.value.port) : '' };
    env.value = JSON.parse(app.value.env || '[]');
    dbSel.value = (app.value.db_names || '').split(',').filter(Boolean);
    deps.value = await api.deployments(id);
  } catch (e: any) {
    loadErr.value = e.message || '加载应用详情失败';
    return;
  }
  loadNginx();
  loadPg();
  loadUnit();
  loadProc();
}

async function save() {
  saving.value = true;
  try {
    await api.updateApp(id, {
      name: edit.value.name,
      type: edit.value.type,
      repo_url: edit.value.repo_url || '',
      branch: edit.value.branch || 'main',
      domain: edit.value.domain || null,
      port: edit.value.port ? Number(edit.value.port) : null,
      install_cmd: edit.value.install_cmd || null,
      start_cmd: edit.value.start_cmd,
      unit_override: edit.value.unit_override || null,
      path: edit.value.path || null,
      db_names: dbSel.value.join(',') || null,
      env: env.value.filter((x) => x && typeof x.k === 'string' && x.k),
    });
    msg.success('已保存');
    await load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    saving.value = false;
  }
}

async function deploy() {
  acting.value = 'deploy';
  try {
    await api.deploy(id);
    msg.success('部署已开始');
    later(pollDeps, 1000);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acting.value = '';
  }
}
async function pollDeps() {
  // 轮询是 setTimeout 里跑的，失败若不在这里吞掉就变成未处理的 promise rejection：
  // 界面上部署记录停在旧数据、也没有任何提示。
  try {
    deps.value = await api.deployments(id);
  } catch (e: any) {
    msg.error(`部署记录获取失败：${e.message}`);
  }
}
async function viewDep(depId: number) {
  try {
    depLog.value = await api.deployment(id, depId);
  } catch (e: any) {
    msg.error(`部署日志获取失败：${e.message}`);
  }
}
async function act(verb: string) {
  acting.value = verb;
  try {
    await api.appAction(id, verb);
    later(load, 600);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acting.value = '';
  }
}
async function ssl() {
  try {
    await api.appSsl(id, sslEmail.value || undefined);
    msg.success('证书已配置');
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function purge() {
  try {
    await api.deleteApp(id, true);
    msg.success('已删除并清理文件');
    router.push('/apps');
  } catch (e: any) {
    msg.error(e.message);
  }
}

onMounted(load);
</script>

<template>
  <div v-if="loadErr" class="page-loading">
    <NAlert type="error" :bordered="false" role="alert">{{ loadErr }}</NAlert>
    <NSpace justify="center">
      <NButton size="small" class="cp-press" @click="load">重新加载</NButton>
      <NButton size="small" quaternary @click="router.push('/apps')">返回列表</NButton>
    </NSpace>
  </div>
  <div v-else-if="!app" class="page-loading">
    <NSkeleton height="52px" class="cp-shimmer" style="border-radius: var(--radius-lg)" />
    <NSkeleton v-for="i in 3" :key="i" height="88px" class="cp-shimmer" style="border-radius: var(--radius-lg)" />
  </div>
  <div v-else>
    <PageHeader :title="app.name" :sub="`${app.unit} · ${app.path}`">
      <template #actions>
        <NButton size="small" quaternary @click="router.push('/apps')">
          <template #icon><NIcon :component="icons.ArrowBackOutline" /></template>
          返回列表
        </NButton>
        <span class="st" :class="app.running ? 'ok' : 'idle'">
          <span class="dot" :class="app.running ? 'ok' : 'idle'"></span>{{ app.running ? '运行中' : '已停止' }}
        </span>
        <span v-if="app.deploying" class="st warn"><span class="dot warn"></span>部署中</span>
        <NButton size="small" type="primary" class="cp-press" :loading="acting === 'deploy'" :disabled="!!acting" :icon="ico('CloudUploadOutline')" @click="deploy">立即部署</NButton>
        <NButton size="small" :tertiary="!app.running" :type="app.running ? 'warning' : 'success'" :loading="acting === (app.running ? 'stop' : 'start')" :disabled="!!acting" :icon="ico(app.running ? 'StopOutline' : 'PlayOutline')" @click="act(app.running ? 'stop' : 'start')">
          {{ app.running ? '停止' : '启动' }}
        </NButton>
        <NButton size="small" tertiary :disabled="!app.running || !!acting" :loading="acting === 'restart'" :icon="ico('SyncOutline')" @click="act('restart')">重启</NButton>
        <NPopconfirm @positive-click="purge">
          <template #trigger>
            <NButton size="small" quaternary type="error" :icon="ico('TrashOutline')" title="彻底删除应用与文件" aria-label="彻底删除应用与文件" />
          </template>
          将同时删除 {{ app.path }} 目录文件与 systemd unit，不可恢复！
        </NPopconfirm>
      </template>
    </PageHeader>

    <NGrid :cols="4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive style="margin-bottom: var(--space-4)">
      <NGridItem span="4 2:1">
        <NCard size="small" class="info-card cp-rise" style="--i: 0">
          <div class="info-k">运行时</div>
          <div class="info-v"><span :class="app.type === 'node' ? 'vchip node' : 'vchip python'">{{ app.type === 'node' ? 'Node.js' : 'Python' }}</span></div>
        </NCard>
      </NGridItem>
      <NGridItem span="4 2:1">
        <NCard size="small" class="info-card cp-rise" style="--i: 1">
          <div class="info-k">监听端口</div>
          <div class="info-v mono-dim" style="font-size: var(--fs-md)">
            {{ app.port || '—' }}
            <NTag v-if="app.portAuto" size="tiny" :bordered="false" style="margin-left: var(--space-2)">自动检测</NTag>
          </div>
        </NCard>
      </NGridItem>
      <NGridItem span="4 2:1">
        <NCard size="small" class="info-card cp-rise" style="--i: 2">
          <div class="info-k">域名</div>
          <div class="info-v" style="font-size: var(--fs-md)">
            {{ app.domain || ngx?.configs?.[0]?.analysis?.serverNames?.[0] || '—' }}
            <NTag v-if="!app.domain && ngx?.configs?.length" size="tiny" :bordered="false" style="margin-left: var(--space-2)">nginx 检测</NTag>
          </div>
        </NCard>
      </NGridItem>
      <NGridItem span="4 2:1">
        <NCard size="small" class="info-card cp-rise" style="--i: 3">
          <div class="info-k">仓库 / 分支</div>
          <div class="info-v" style="font-size: var(--fs-sm)">{{ app.repo_url ? (app.repo_url.replace(/^https?:\/\//, '').replace(/\.git$/, '') + ' @ ' + (app.branch || 'main')) : '本地目录' }}</div>
        </NCard>
      </NGridItem>
    </NGrid>

    <NCard v-if="proc" size="small" class="res-bar" style="margin-bottom: var(--space-4)">
      <NSpace :size="24" align="center" wrap>
        <span class="res-item"><span class="res-k">进程</span><b class="mono-dim">{{ proc.running ? 'PID ' + proc.pid : '未运行' }}</b></span>
        <span class="res-item"><span class="res-k">内存</span><b class="mono-dim">{{ fmtMem(proc.memoryBytes) }}</b></span>
        <span class="res-item"><span class="res-k">CPU 累计</span><b class="mono-dim">{{ proc.cpuSeconds != null ? proc.cpuSeconds + 's' : '—' }}</b></span>
        <span class="res-item"><span class="res-k">重启次数</span><b class="mono-dim" :style="proc.restarts > 3 ? 'color:var(--cp-warn)' : ''">{{ proc.restarts }}</b></span>
        <span class="res-item"><span class="res-k">自启动</span><b class="mono-dim">{{ proc.execStartAt || proc.activeSince || '—' }}</b></span>
        <span class="res-item"><span class="res-k">状态</span><b class="mono-dim">{{ proc.subState }}</b></span>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="loadProc">刷新</NButton>
      </NSpace>
    </NCard>

    <NCard size="small" :content-style="{ padding: '0' }">
      <NTabs type="line" animated pane-style="padding: var(--space-4); padding-top: var(--space-2)">
        <NTabPane name="config" tab="基本配置">
          <NForm label-placement="left" label-width="100" style="max-width: 660px; margin-top: var(--space-3)">
            <NSpace vertical :size="12">
              <NFormItem label="名称"><NInput v-model:value="edit.name" :input-props="{ 'aria-label': '应用名称' }" /></NFormItem>
              <NFormItem label="安装目录"><NInput v-model:value="edit.path" placeholder="/root/www/<名称>" :input-props="{ 'aria-label': '安装目录' }" /></NFormItem>
              <NFormItem label="Git 仓库"><NInput v-model:value="edit.repo_url" :input-props="{ 'aria-label': 'Git 仓库地址' }" /></NFormItem>
              <NFormItem label="分支"><NInput v-model:value="edit.branch" :input-props="{ 'aria-label': '分支' }" /></NFormItem>
              <NFormItem label="安装命令"><NInput v-model:value="edit.install_cmd" :input-props="{ 'aria-label': '安装命令' }" /></NFormItem>
              <NFormItem label="启动命令"><NInput v-model:value="edit.start_cmd" :input-props="{ 'aria-label': '启动命令', class: 'mono' }" /></NFormItem>
              <NFormItem label="端口"><NInput v-model:value="edit.port" placeholder="留空表示无端口" :input-props="{ 'aria-label': '监听端口' }" /></NFormItem>
              <NFormItem label="域名"><NInput v-model:value="edit.domain" :input-props="{ 'aria-label': '域名' }" /></NFormItem>
              <NFormItem label="关联数据库">
                <NSelect v-model:value="dbSel" multiple filterable tag :options="pgOptions" placeholder="选择该应用使用的 PostgreSQL 数据库，便于项目管理与备份联动" aria-label="关联数据库" style="width: 100%" />
              </NFormItem>
              <NButton type="primary" :loading="saving" :icon="ico('SaveOutline')" @click="save">保存配置</NButton>
            </NSpace>
          </NForm>
        </NTabPane>

        <NTabPane name="env" tab="环境变量">
          <NSpace vertical style="width: 100%">
            <NText depth="3" style="font-size: var(--fs-xs)">写入应用目录的 .env 并由 systemd EnvironmentFile 加载；保存后重启应用生效。</NText>
            <NDynamicInput v-model:value="env" :on-create="() => ({ k: '', v: '' })">
              <template #default="{ value }">
                <NInput v-model:value="value.k" placeholder="KEY" style="width: 220px; margin-right: var(--space-2)" :input-props="{ class: 'mono', 'aria-label': '环境变量名' }" />
                <NInput v-model:value="value.v" placeholder="value" style="flex: 1" :input-props="{ class: 'mono', 'aria-label': '环境变量值' }" />
              </template>
            </NDynamicInput>
            <NButton size="small" type="primary" :icon="ico('SaveOutline')" style="align-self: flex-start" @click="save">保存环境变量</NButton>
          </NSpace>
        </NTabPane>

        <NTabPane name="deploy" tab="部署记录">
          <div class="split-view">
            <div class="dep-list">
              <div v-for="d in deps" :key="d.id" class="dep-item" :class="{ active: depLog?.id === d.id }"
                   role="button" tabindex="0" :aria-label="`查看部署记录 #${d.id}`"
                   @click="viewDep(d.id)" @keyup.enter="viewDep(d.id)" @keyup.space.prevent="viewDep(d.id)">
                <NSpace align="center" justify="space-between" style="width: 100%">
                  <NText depth="3" style="font-size: var(--fs-xs)">#{{ d.id }} · {{ d.started_at }}</NText>
                  <span class="st" :class="d.status === 'success' ? 'ok' : d.status === 'running' ? 'warn' : 'err'" style="font-size: var(--fs-2xs)">
                    <span class="dot" :class="d.status === 'success' ? 'ok' : d.status === 'running' ? 'warn' : 'err'"></span>{{ d.status }}
                  </span>
                </NSpace>
              </div>
              <EmptyBox v-if="!deps.length" text="暂无部署记录" />
              <NButton size="small" tertiary :icon="ico('RefreshOutline')" style="margin-top: var(--space-2)" @click="pollDeps">刷新</NButton>
            </div>
            <div class="log-view" style="flex: 1; min-width: 0; max-height: 460px">{{ depLog?.log || '点击左侧记录查看部署日志' }}</div>
          </div>
        </NTabPane>

        <NTabPane name="logs" tab="运行日志">
          <LogStream :url-path="`/apps/${id}/logs`" />
        </NTabPane>

        <NTabPane name="nginx" tab="域名 / Nginx">
          <div v-if="!ngx?.configs?.length" style="padding: var(--space-5) 0">
            <EmptyBox text="未找到与该应用关联的 nginx 配置（按域名或反代端口匹配）。配置域名并创建反代后，这里可直接快捷编辑" />
          </div>
          <div v-else class="split-view">
            <div class="ngx-list">
              <div
                v-for="c in ngx.configs" :key="c.file" class="ngx-item" :class="{ active: ngxFile?.file === c.file }"
                role="button" tabindex="0" :aria-label="`查看 nginx 配置 ${c.name}`"
                @click="selConfig(c)" @keyup.enter="selConfig(c)" @keyup.space.prevent="selConfig(c)"
              >
                <NIcon :component="icons.GlobeOutline" :size="15" :color="c.analysis.ssl ? 'var(--cp-ok)' : 'var(--cp-text-mute)'" />
                <div style="min-width: 0">
                  <div class="ngx-name">{{ c.name }}</div>
                  <div class="ngx-dom">{{ c.analysis.serverNames.slice(0, 2).join(', ') || '（无 server_name）' }}</div>
                </div>
              </div>
              <div v-if="ngxFile" style="margin-top: var(--space-3); display: flex; flex-wrap: wrap; gap: var(--space-2)">
                <NTag size="tiny" :bordered="false" :type="ngxFile.analysis.ssl ? 'success' : 'default'">{{ ngxFile.analysis.ssl ? 'HTTPS' : '仅 HTTP' }}</NTag>
                <NTag size="tiny" :bordered="false" :type="ngxFile.analysis.httpsRedirect ? 'success' : 'default'">301 跳转 {{ ngxFile.analysis.httpsRedirect ? '✓' : '✗' }}</NTag>
                <NTag size="tiny" :bordered="false" :type="ngxFile.analysis.websocket ? 'success' : 'default'">WebSocket {{ ngxFile.analysis.websocket ? '✓' : '✗' }}</NTag>
                <NTag size="tiny" :bordered="false">body {{ ngxFile.analysis.bodySize || '默认 1m' }}</NTag>
              </div>
              <NText v-if="ngxFile" depth="3" style="font-size: var(--fs-2xs); display: block; margin-top: var(--space-2); word-break: break-all">{{ ngxFile.file }}</NText>
            </div>
            <div style="flex: 1; min-width: 0">
              <NSpace size="small" style="margin-bottom: var(--space-3)" wrap>
                <NButton size="small" tertiary :disabled="!ngxFile || ngxFile.analysis.websocket" :icon="ico('PulseOutline')" @click="quick('ws')">添加 WebSocket 支持</NButton>
                <NInput v-model:value="bodySizeVal" size="small" style="width: 80px" placeholder="50m" :input-props="{ 'aria-label': '上传大小限制' }" />
                <NButton size="small" tertiary :disabled="!ngxFile" :icon="ico('DownloadOutline')" @click="quick('body')">设置上传限制</NButton>
                <NButton size="small" tertiary :disabled="!ngxFile || !ngxFile.analysis.ssl || ngxFile.analysis.httpsRedirect" :icon="ico('ShieldCheckmarkOutline')" @click="quick('redirect')">强制 HTTPS 跳转</NButton>
              </NSpace>
              <NInput v-model:value="ngxContent" type="textarea" :autosize="{ minRows: 14, maxRows: 26 }" :input-props="{ class: 'mono', 'aria-label': 'nginx 配置内容', style: 'font-size: var(--fs-xs)' }" />
              <NSpace justify="space-between" align="center" style="margin-top: var(--space-3)">
                <NText depth="3" style="font-size: var(--fs-xs)">保存将执行 nginx -t 校验，失败自动回滚并 reload 生效</NText>
                <NButton type="primary" size="small" class="cp-press" :loading="ngxSaving" :icon="ico('SaveOutline')" @click="saveNginx">保存配置</NButton>
              </NSpace>
            </div>
          </div>
        </NTabPane>

        <NTabPane name="unit" tab="systemd 单元">
          <NSpace vertical :size="12" style="width: 100%">
            <NAlert v-if="unitInfo && !unitInfo.managed" type="warning" :bordered="false" size="small">
              该应用为纳管模式（unit {{ app.unit }} 由外部管理），面板只读展示，不会写入该文件。
            </NAlert>
            <NText depth="3" style="font-size: var(--fs-2xs)">
              文件：<span class="mono-dim">{{ unitInfo?.path }}</span>。保存流程：写入 → systemd-analyze verify 校验 → daemon-reload，任一步失败自动回滚。修改后需重启服务才生效；面板生成模式下保存会同步更新部署模板，后续部署按此模板渲染。占位符 <code v-pre>{{name}} {{path}} {{start_cmd}} {{port}}</code> 可在模板中使用。
            </NText>
            <NInput
              v-model:value="unitContent"
              type="textarea"
              :autosize="{ minRows: 14, maxRows: 26 }"
              :read-only="!!unitInfo && !unitInfo.managed"
              :input-props="{ class: 'mono', 'aria-label': 'systemd unit 内容', style: 'font-size: var(--fs-xs)' }"
            />
            <NSpace justify="space-between" align="center">
              <NSpace size="small">
                <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="loadUnit">重新读取</NButton>
                <NButton v-if="unitInfo?.managed" size="small" tertiary @click="restoreDefaultTpl">恢复面板默认模板</NButton>
                <NButton v-if="unitInfo?.managed" size="small" tertiary type="warning" :icon="ico('SyncOutline')" @click="act('restart')">保存后重启服务</NButton>
              </NSpace>
              <NButton type="primary" size="small" :loading="unitSaving" :disabled="!unitInfo?.managed" :icon="ico('SaveOutline')" @click="saveUnitFile">保存 Unit</NButton>
            </NSpace>
          </NSpace>
        </NTabPane>

        <NTabPane name="ssl" tab="SSL 证书">
          <NSpace vertical :size="16">
            <NSpace align="center" :size="8">
              <NIcon :component="icons.ShieldCheckmarkOutline" :size="20" color="var(--cp-brand-soft)" />
              <NText style="font-size: var(--fs-sm)">{{ app.domain || '（未配置域名）' }}</NText>
              <NTag v-if="app.domain" size="tiny" :bordered="false" type="info">Let's Encrypt</NTag>
            </NSpace>
            <NText depth="3" style="font-size: var(--fs-2xs)">要求该域名 DNS A 记录已指向本机 IP。申请成功后自动切换为 HTTPS 反代并开启 80 → 443 跳转。</NText>
            <NSpace>
              <NInput v-model:value="sslEmail" placeholder="邮箱（可选，用于到期通知）" style="width: 300px" :input-props="{ 'aria-label': 'SSL 到期通知邮箱' }" />
              <NButton type="primary" :disabled="!app.domain" :icon="ico('KeyOutline')" @click="ssl">申请 / 配置证书</NButton>
            </NSpace>
          </NSpace>
        </NTabPane>
      </NTabs>
    </NCard>
  </div>
</template>

<style scoped>
.page-loading { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-5) 0; }
.info-card :deep(.n-card__content) { padding: var(--space-3) var(--space-4); }
.info-k { font-size: var(--fs-2xs); color: var(--cp-text-mute); margin-bottom: var(--space-2); }
.info-v { color: var(--cp-text); font-weight: 500; }
.split-view { display: flex; align-items: flex-start; gap: var(--space-3); }
.dep-list { width: 300px; flex-shrink: 0; border-right: 1px solid var(--cp-border); padding-right: var(--space-3); }
.dep-item { padding: var(--space-3); border-radius: var(--radius); cursor: pointer; transition: background var(--dur-fast) var(--ease); }
.dep-item:hover { background: var(--cp-hover); }
.dep-item.active { background: var(--cp-selected); }
.ngx-list { width: 260px; flex-shrink: 0; border-right: 1px solid var(--cp-border); padding-right: var(--space-3); }
.ngx-item { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2) var(--space-3); border-radius: var(--radius); cursor: pointer; transition: background var(--dur-fast) var(--ease); }
.ngx-item:hover { background: var(--cp-hover); }
.ngx-item.active { background: var(--cp-selected); }
.ngx-name { font-size: var(--fs-xs); color: var(--cp-text); font-weight: 550; }
.ngx-dom { font-size: var(--fs-2xs); color: var(--cp-text-mute); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.res-bar :deep(.n-card__content) { padding: var(--space-3) var(--space-4); }
.res-item { display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--fs-sm); color: var(--cp-text); }
.res-k { font-size: var(--fs-2xs); color: var(--cp-text-mute); }
@media (max-width: 900px) {
  .split-view { flex-direction: column; }
  .dep-list, .ngx-list { width: 100%; border-right: 0; padding-right: 0; margin-bottom: var(--space-2); }
}
</style>
