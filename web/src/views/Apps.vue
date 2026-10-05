<script setup lang="ts">
import { onMounted, ref, h, computed } from 'vue';
import { useRouter } from 'vue-router';
import {
  NButton, NSpace, NDataTable, NModal, NForm, NFormItem, NInput, NInputNumber, NSelect,
  NRadioGroup, NRadioButton, NPopconfirm, NIcon, NAlert, NTag, NCollapse, NCollapseItem,
  useMessage,
} from 'naive-ui';
import { api } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const router = useRouter();
const msg = useMessage();
const rows = ref<any[]>([]);
const kw = ref('');
const showCreate = ref(false);
const creating = ref(false);
const form = ref<any>({
  name: '', type: 'node', repo_url: '', branch: 'main', domain: '', port: null,
  install_cmd: '', start_cmd: 'npm start', unit_override: '', path: '', unit_template: '', template: '',
});

const runningCount = computed(() => rows.value.filter((r) => r.running).length);
const filtered = computed(() => {
  if (!kw.value) return rows.value;
  const k = kw.value.toLowerCase();
  return rows.value.filter((r) => `${r.name} ${r.domain || ''} ${(r.domains || []).join(' ')} ${r.port || ''} ${r.type}`.toLowerCase().includes(k));
});

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

const columns: any[] = [
  {
    title: '应用', key: 'name',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: r.type === 'node' ? icons.LogoElectron : icons.LogoPython, size: 20, color: r.type === 'node' ? '#8fd460' : '#8fa8ff' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h(NButton, { text: true, type: 'primary', class: 'cell-name', onClick: () => router.push(`/apps/${r.id}`) }, () => r.name),
          h('div', { class: 'cell-sub' }, r.domain || r.unit || r.path || '—'),
        ]),
      ]),
  },
  {
    title: '类型', key: 'type', width: 96,
    render: (r: any) => h('span', { class: r.type === 'node' ? 'vchip node' : 'vchip python' }, r.type === 'node' ? 'Node.js' : 'Python'),
  },
  {
    title: '端口', key: 'port', width: 100,
    render: (r: any) =>
      r.port
        ? h('span', { class: 'mono-dim' }, [
            String(r.port),
            ...(r.portAuto ? [h('span', { style: 'color:var(--cp-text-mute);font-size:10.5px;margin-left:5px' }, '自动')] : []),
          ])
        : h('span', { class: 'cell-sub' }, '—'),
  },
  {
    title: '域名', key: 'domains', width: 210,
    render: (r: any) => {
      const ds = (r.domains || []) as string[];
      if (!ds.length) return h('span', { class: 'cell-sub' }, '—');
      return h(NSpace, { size: 4, wrap: false }, () => [
        ...ds.slice(0, 2).map((d) =>
          h(NTag, { size: 'small', bordered: false, type: r.domain ? 'default' : 'info' }, { default: () => d }),
        ),
        ...(ds.length > 2 ? [h('span', { class: 'cell-sub' }, `+${ds.length - 2}`)] : []),
        ...(!r.domain ? [h('span', { style: 'color:var(--cp-text-mute);font-size:10.5px' }, 'nginx 检测')] : []),
      ]);
    },
  },
  {
    title: '状态', key: 'status', width: 110,
    render: (r: any) =>
      r.deploying
        ? h('span', { class: 'st warn' }, [h('span', { class: 'dot warn' }), '部署中'])
        : r.running
          ? h('span', { class: 'st ok' }, [h('span', { class: 'dot ok' }), '运行中'])
          : h('span', { class: 'st idle' }, [h('span', { class: 'dot idle' }), '已停止']),
  },
  {
    title: '操作', key: 'ops', width: 292,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, type: 'primary', loading: busyId.value === r.id, icon: ico('CloudUploadOutline'), onClick: () => act(r.id, 'deploy') }, () => '部署'),
        h(NButton, {
          size: 'tiny', tertiary: true, type: r.running ? 'warning' : 'success',
          icon: ico(r.running ? 'StopOutline' : 'PlayOutline'), onClick: () => act(r.id, r.running ? 'stop' : 'start'),
        }, () => (r.running ? '停止' : '启动')),
        h(NButton, { size: 'tiny', tertiary: true, disabled: !r.running, icon: ico('SyncOutline'), onClick: () => act(r.id, 'restart') }, () => '重启'),
        h(NButton, { size: 'tiny', quaternary: true, title: '回滚到上一次成功部署', 'aria-label': '回滚', icon: ico('ArrowBackOutline'), onClick: () => rollback(r) }),
        h(NPopconfirm, { onPositiveClick: () => del(r) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: `删除应用 ${r.name}`, 'aria-label': `删除应用 ${r.name}` }, { default: () => '' }),
          default: () => '仅删除面板配置，服务器文件保留。彻底清理请到应用详情页操作。',
        }),
      ]),
  },
];

const loading = ref(false);
const busyId = ref<number | null>(null);

async function load() {
  loading.value = true;
  try {
    rows.value = await api.apps();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
  }
}
async function rollback(r: any) {
  try {
    const res = await api.appAction(r.id, 'rollback');
    msg.success(`已回滚到 ${res.commit || '上一版本'}`);
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function act(id: number, verb: string) {
  busyId.value = id;
  try {
    if (verb === 'deploy') await api.deploy(id);
    else await api.appAction(id, verb);
    msg.success(verb === 'deploy' ? '部署任务已启动' : '操作已下发');
    setTimeout(load, 800);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busyId.value = null;
  }
}
const templates = ref<any[]>([]);
const templateOptions = computed(() => [
  { label: '自定义（手动填写启动命令）', value: '' },
  ...templates.value.map((t: any) => ({ label: `${t.name} — ${t.desc}`, value: t.key })),
]);

async function loadTemplates() {
  try {
    templates.value = (await api.templates()) as any[];
  } catch {
    templates.value = []; // 模板加载失败不阻塞建应用，用户可手动填
  }
}

// 选模板即填默认值，但用户已手改的字段不覆盖
function pickTemplate(key: string) {
  if (!key) return;
  const t = templates.value.find((x: any) => x.key === key);
  if (!t) return;
  form.value.type = t.type;
  form.value.install_cmd = t.install_cmd || '';
  form.value.start_cmd = t.start_cmd || '';
  form.value.port = t.port || null;
}

async function del(r: any) {
  try {
    await api.deleteApp(r.id, false);
    msg.success('已删除配置');
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function create() {
  if (!form.value.name) return msg.error('请填写应用名称');
  creating.value = true;
  try {
    const a = await api.createApp({
      name: form.value.name,
      type: form.value.type,
      repo_url: form.value.repo_url,
      branch: form.value.branch || 'main',
      domain: form.value.domain || null,
      port: form.value.port || null,
      install_cmd: form.value.install_cmd || null,
      start_cmd: form.value.start_cmd,
      unit_override: form.value.unit_override || null,
      path: form.value.path || null,
      unit_template: form.value.unit_template || null,
      template: form.value.template || undefined,
    });
    msg.success('创建成功');
    showCreate.value = false;
    router.push(`/apps/${a.id}`);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    creating.value = false;
  }
}

onMounted(() => {
  loadTemplates();
  load();
});
</script>

<template>
  <div>
    <PageHeader title="应用" :sub="`共 ${rows.length} 个应用 · ${runningCount} 个运行中。部署、启停、日志与域名反代集中在此管理`">
      <template #actions>
        <NInput :value="kw" @update:value="(v: string) => (kw = v)" placeholder="搜索名称/域名/端口" size="small" clearable style="width: 200px; max-width: 100%">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
        <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="showCreate = true">新建应用</NButton>
      </template>
    </PageHeader>

    <NDataTable :columns="columns" :data="filtered" :bordered="false" size="small" :row-key="(r: any) => r.id" :scroll-x="1020" :max-height="'calc(100vh - 260px)'">
      <template #empty>
        <EmptyBox :text="rows.length ? '没有匹配的应用' : '还没有应用 —— 点击「新建应用」接入你的第一个 Node / Python 项目'" />
      </template>
    </NDataTable>

    <NModal v-model:show="showCreate" preset="card" title="新建应用" style="width: 620px; max-width: 94vw">
      <NSpace vertical :size="4">
        <NAlert type="info" :bordered="false" size="small" style="margin-bottom: 8px">
          Git 仓库与「已有 unit」都可留空：仓库留空时纳管服务器上已有目录，unit 留空时由面板生成 systemd 服务并接管。
        </NAlert>
        <NForm label-placement="left" label-width="96">
          <NSpace vertical :size="12">
            <NFormItem label="名称" required>
              <NInput v-model:value="form.name" placeholder="小写字母/数字/连字符，如 my-site" />
            </NFormItem>
            <NFormItem label="一键模板">
              <NSelect
                v-model:value="form.template"
                :options="templateOptions"
                size="small"
                placeholder="选一个预设，自动填入运行时/命令/端口（可再手动改）"
                @update:value="pickTemplate"
              />
            </NFormItem>
            <NFormItem label="运行时">
              <NRadioGroup v-model:value="form.type" size="small">
                <NRadioButton value="node">Node.js</NRadioButton>
                <NRadioButton value="python">Python</NRadioButton>
              </NRadioGroup>
            </NFormItem>
            <NFormItem label="Git 仓库">
              <NInput v-model:value="form.repo_url" placeholder="https://… 可留空：纳管服务器上已有目录" />
            </NFormItem>
            <NFormItem label="分支">
              <NInput v-model:value="form.branch" placeholder="main" />
            </NFormItem>
            <NFormItem label="安装目录">
              <NInput v-model:value="form.path" placeholder="留空默认 /root/www/<名称>；可指定如 /root/www/my-site" />
            </NFormItem>
            <NFormItem label="安装命令">
              <NInput
                v-model:value="form.install_cmd"
                :placeholder="form.type === 'node' ? '默认 npm install' : '默认 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'"
              />
            </NFormItem>
            <NFormItem label="启动命令" required>
              <NInput v-model:value="form.start_cmd" :placeholder="form.type === 'node' ? '如 npm start 或 node server.js' : '如 .venv/bin/uvicorn main:app --port 8000'" />
            </NFormItem>
            <NFormItem label="端口">
              <NInputNumber v-model:value="form.port" placeholder="监听端口，配域名时必填" style="width: 100%" />
            </NFormItem>
            <NFormItem label="域名">
              <NInput v-model:value="form.domain" placeholder="如 app.choyeon.cc，留空不创建反代" />
            </NFormItem>
            <NFormItem label="已有 unit">
              <NInput v-model:value="form.unit_override" placeholder="纳管用：如 rosetta-backend.service，填了则不覆写 unit" />
            </NFormItem>
            <NCollapse>
              <NCollapseItem title="高级：自定义 systemd unit 模板" name="tpl">
                <NInput
                  v-model:value="form.unit_template"
                  type="textarea"
                  :autosize="{ minRows: 6, maxRows: 14 }"
                  class="mono-dim"
                  placeholder="留空则使用面板默认模板。支持占位符：{{name}} {{path}} {{start_cmd}} {{port}}，部署时自动渲染写入 /etc/systemd/system/panel-<名称>.service"
                />
                <div style="color:var(--cp-text-mute);font-size:12px;margin-top:6px">
                  创建后也可在应用详情「systemd 单元」页签随时修改，保存前自动 systemd-analyze verify 校验，失败即回滚。
                </div>
              </NCollapseItem>
            </NCollapse>
            <NButton type="primary" block :loading="creating" @click="create">创建应用</NButton>
          </NSpace>
        </NForm>
      </NSpace>
    </NModal>
  </div>
</template>
