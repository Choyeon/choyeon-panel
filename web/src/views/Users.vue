<script setup lang="ts">
import { onMounted, ref, h } from 'vue';
import {
  NCard, NText, NSpace, NDataTable, useMessage, NInput, NModal, NForm, NFormItem,
  NSelect, NPopconfirm, NIcon, NTag,
} from 'naive-ui';
import { NButton } from '../ui';
import { api } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const users = ref<any[]>([]);
const loading = ref(false);
const kw = ref('');
const showUser = ref(false);
const userForm = ref({ username: '', password: '', role: 'viewer' });
const showReset = ref(false);
const resetTarget = ref<any>(null);
const resetPwd = ref('');

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

async function load() {
  loading.value = true;
  try {
    users.value = await api.users();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
  }
}

async function createUser() {
  if (!userForm.value.username || userForm.value.password.length < 8) return msg.error('用户名必填，密码至少 8 位');
  try {
    await api.createUser(userForm.value.username, userForm.value.password, userForm.value.role);
    msg.success('已创建');
    showUser.value = false;
    userForm.value = { username: '', password: '', role: 'viewer' };
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function toggleRole(u: any) {
  try {
    await api.updateUser(u.id, { role: u.role === 'admin' ? 'viewer' : 'admin' });
    msg.success('角色已更新，该用户旧登录态已吊销');
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
function openReset(u: any) {
  resetTarget.value = u;
  resetPwd.value = '';
  showReset.value = true;
}
async function confirmReset() {
  if (resetPwd.value.length < 8) return msg.error('密码至少 8 位');
  try {
    await api.updateUser(resetTarget.value.id, { password: resetPwd.value });
    msg.success('密码已重置，该用户的旧 token 已失效');
    showReset.value = false;
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function delUser(u: any) {
  try {
    await api.deleteUser(u.id);
    msg.success('已删除');
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}

const cols: any[] = [
  {
    title: '用户', key: 'username',
    render: (u: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-avatar' }, [
          u.avatar
            ? h('img', { src: u.avatar, alt: '' })
            : h(NIcon, { component: icons.PersonCircleOutline, size: 28, color: 'var(--cp-text-mute)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name' }, u.nickname || u.username),
          h('div', { class: 'cell-sub' }, u.nickname ? `${u.username} · 创建于 ${u.created_at || '—'}` : `创建于 ${u.created_at || '—'}`),
        ]),
      ]),
  },
  {
    title: '角色', key: 'role', width: 110,
    render: (u: any) => h(NTag, { size: 'small', bordered: false, type: u.role === 'admin' ? 'primary' : 'default' }, () => (u.role === 'admin' ? '管理员' : '只读')),
  },
  {
    title: '权限说明', key: 'perm', width: 320,
    render: (u: any) =>
      h(NText, { depth: 3, style: 'font-size:var(--fs-xs)' }, () =>
        u.role === 'admin' ? '可执行全部操作：启停、部署、终端、文件、用户管理' : '只读：可查看全部监控/日志/数据，不能执行任何写操作',
      ),
  },
  {
    title: '操作', key: 'ops', width: 280,
    render: (u: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('SyncOutline', 12), onClick: () => toggleRole(u) }, () => (u.role === 'admin' ? '降为只读' : '升为管理')),
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('KeyOutline', 12), onClick: () => openReset(u) }, () => '重置密码'),
        h(NPopconfirm, { onPositiveClick: () => delUser(u) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline', 12), title: `删除用户 ${u.username}`, 'aria-label': `删除用户 ${u.username}` }, { default: () => '' }),
          default: () => `删除用户 ${u.username}？其已登录会话将立即失效。`,
        }),
      ]),
  },
];

const filtered = () => {
  if (!kw.value) return users.value;
  const k = kw.value.toLowerCase();
  return users.value.filter((u) => `${u.username} ${u.nickname || ''}`.toLowerCase().includes(k));
};

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="用户管理" sub="管理员可执行全部操作；只读账号可看监控/日志/数据，但不能启停、部署、删除或使用终端">
      <template #actions>
        <NInput v-model:value="kw" placeholder="搜索用户名/昵称" size="small" clearable style="width: 180px">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
        <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="showUser = true">新建用户</NButton>
      </template>
    </PageHeader>

    <NCard size="small">
      <NDataTable size="small" :bordered="false" :loading="loading" :scroll-x="820" :columns="cols" :data="filtered()">
        <template #empty><EmptyBox text="暂无用户" /></template>
      </NDataTable>
    </NCard>

    <NModal v-model:show="showUser" preset="card" title="新建用户" style="width: 420px; max-width: 94vw">
      <NForm label-placement="left" label-width="70">
        <NSpace vertical :size="12">
          <NFormItem label="用户名"><NInput v-model:value="userForm.username" placeholder="字母/数字/-/_，3-32 位" :input-props="{ 'aria-label': '用户名' }" /></NFormItem>
          <NFormItem label="密码"><NInput v-model:value="userForm.password" type="password" show-password-on="click" placeholder="至少 8 位" :input-props="{ 'aria-label': '密码' }" /></NFormItem>
          <NFormItem label="角色">
            <NSelect v-model:value="userForm.role" :options="[{ label: '只读 viewer', value: 'viewer' }, { label: '管理员 admin', value: 'admin' }]" aria-label="用户角色" />
          </NFormItem>
          <NButton type="primary" block class="cp-press" :icon="ico('AddOutline')" @click="createUser">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>

    <NModal v-model:show="showReset" preset="card" :title="`重置密码 · ${resetTarget?.username || ''}`" style="width: 420px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NText depth="3" style="font-size: var(--fs-xs)">重置后该用户的旧登录令牌立即失效，需要重新登录。</NText>
        <NInput v-model:value="resetPwd" type="password" show-password-on="click" placeholder="新密码（至少 8 位）" :input-props="{ 'aria-label': '新密码' }" @keyup.enter="confirmReset" />
        <NSpace justify="end">
          <NButton tertiary @click="showReset = false">取消</NButton>
          <NButton type="primary" class="cp-press" :icon="ico('SaveOutline')" @click="confirmReset">确认重置</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>
