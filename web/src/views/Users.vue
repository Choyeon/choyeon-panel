<script setup lang="ts">
import { computed, ref, watch, h } from 'vue';
import {
  NCard, NText, NSpace, NDataTable, useMessage, NInput, NModal, NForm, NFormItem,
  NSelect, NPopconfirm, NIcon, NTag,
} from 'naive-ui';
import { NButton } from '../ui';
import { api } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { fmtDateTime } from '../composables/useFormat';

const msg = useMessage();
const kw = ref('');
const showUser = ref(false);
const userForm = ref({ username: '', password: '', role: 'viewer' });
const showReset = ref(false);
const resetTarget = ref<any>(null);
const resetPwd = ref('');

// 用户表改动极少，缓存 30s：进页面先画上一次的结果，后台再验证
const { data, error, loading, busy, refresh } = useCachedFetch<any[]>({
  key: () => 'users',
  loader: () => api.users(),
  maxAge: 30000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });

const users = computed<any[]>(() => data.value ?? []);

// useCachedFetch 已经占了 `busy`（刷新按钮的转圈），行内动作另起一个名字。
// 三个弹窗/行内按钮原来都没有 loading：连点「创建」会发两次 POST，第二次拿到
// 「用户名已存在」，用户看到的是"我明明建成功了怎么报错"；连点「升为管理」会在
// 请求返回前一直读着同一份旧 `u.role`，两次同样的 PUT 把角色翻回原地还弹两声成功。
const creating = ref(false);
const resetting = ref(false);
// 行内动作按行号记在一张 map 里（同 Apps.vue）。旧写法是单槽 {id, verb}：
// B 行点一下就把 A 行在飞的那份顶掉，A 的 finally 又无条件清掉 B 的，
// 于是两行的按钮都在自己请求还没回来时就恢复，连点会并发同一行的角色翻转/删除。
const rowBusyMap = ref<Record<number, string>>({});
const rowBusy = (id: number) => rowBusyMap.value[id] !== undefined;
const opBusy = (id: number, verb: string) => rowBusyMap.value[id] === verb;
function startRowOp(id: number, verb: string) { rowBusyMap.value = { ...rowBusyMap.value, [id]: verb }; }
function endRowOp(id: number) {
  const next = { ...rowBusyMap.value };
  delete next[id];
  rowBusyMap.value = next;
}

function reload() {
  invalidateCache('users');
  // 用户表的每一次写操作都会留一条审计，审计页缓存一起作废
  invalidateCache('audit:');
  return refresh().catch(() => undefined);
}

async function createUser() {
  if (creating.value) return;
  if (!userForm.value.username || userForm.value.password.length < 8 || userForm.value.password.length > 128)
    return msg.error('用户名必填，密码需 8–128 位');
  creating.value = true;
  try {
    await api.createUser(userForm.value.username, userForm.value.password, userForm.value.role);
    msg.success('已创建');
    showUser.value = false;
    userForm.value = { username: '', password: '', role: 'viewer' };
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    creating.value = false;
  }
}
async function toggleRole(u: any) {
  if (rowBusy(u.id)) return;
  startRowOp(u.id, 'role');
  try {
    await api.updateUser(u.id, { role: u.role === 'admin' ? 'viewer' : 'admin' });
    msg.success('角色已更新，该用户旧登录态已吊销');
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    endRowOp(u.id);
  }
}
function openReset(u: any) {
  // 这一行的角色翻转/删除还在飞时不把它的快照放进弹窗：按钮此刻已经是禁用态，
  // 这里再挡一道，是为了 confirmReset 不会对着一个可能已被删除的 id 发 PUT。
  if (rowBusy(u.id)) return;
  resetTarget.value = u;
  resetPwd.value = '';
  showReset.value = true;
}
async function confirmReset() {
  if (resetting.value) return;
  if (resetPwd.value.length < 8 || resetPwd.value.length > 128) return msg.error('密码需 8–128 位');
  resetting.value = true;
  try {
    await api.updateUser(resetTarget.value.id, { password: resetPwd.value });
    msg.success('密码已重置，该用户的旧 token 已失效');
    showReset.value = false;
    // 列表里没有哪一列会因为改口令而变，所以不整表 reload；但审计页缓存 30s，
    // 不作废就得等过期才看得到这条「重置密码」，看起来像操作没留痕。
    invalidateCache('audit:');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    resetting.value = false;
  }
}
async function delUser(u: any) {
  if (rowBusy(u.id)) return;
  startRowOp(u.id, 'del');
  try {
    const res = await api.deleteUser(u.id);
    // 账号行删了但旧头像没删掉时，那张脸还挂在免登录的公开头像路由上，面板里没有任何
    // 地方能再看清它——不能说"已删除"了事，把后端交出的残留原因如实提示（批次97）。
    if (res?.warn) msg.warning(res.warn, { duration: 8000 });
    else msg.success('已删除');
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    endRowOp(u.id);
  }
}

const cols: any[] = [
  {
    // 弹性列必须带 minWidth：scroll-x 用 'auto' 时它拿到「总宽 − 固定列宽」，
    // 三个固定列 + 右侧操作列一超过容器，这列就是 0 宽（同批次㊴的 Apps/Files）。
    title: '用户', key: 'username', minWidth: 220,
    render: (u: any) => {
      // fmtDateTime 按 UTC 截断展示（见 useFormat 注释），这里不标注就会被读成浏览器本地时间。
      const sub = u.nickname
        ? `${u.username} · 创建于 ${fmtDateTime(u.created_at)} UTC`
        : `创建于 ${fmtDateTime(u.created_at)} UTC`;
      return h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-avatar' }, [
          u.avatar
            ? h('img', { src: u.avatar, alt: '' })
            : h(NIcon, { component: icons.PersonCircleOutline, size: 28, color: 'var(--cp-text-mute)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: u.nickname || u.username }, u.nickname || u.username),
          h('div', { class: 'cell-sub cp-prose', title: sub }, sub),
        ]),
      ]);
    },
  },
  {
    title: '角色', key: 'role', width: 110,
    render: (u: any) => h(NTag, { size: 'small', bordered: false, type: u.role === 'admin' ? 'primary' : 'default' }, () => (u.role === 'admin' ? '管理员' : '只读')),
  },
  {
    title: '两步验证', key: 'totp', width: 100, align: 'center',
    // 只报"有没有开"，不给开关的按钮：能远程关掉别人的第二因子，等于这个账号没有第二因子。
    // 验证器丢了走 root 才能跑的逃生口（choyeonctl user 2fa-disable <用户名> --yes），它连会话一起吊销。
    render: (u: any) =>
      h(NTag, { size: 'small', bordered: false, type: u.totp ? 'success' : 'default' }, () => (u.totp ? '已开启' : '仅口令')),
  },
  {
    // 整句话的说明：折行看得全，比「hover 才浮出来」诚实——窄屏上这一列本来就只有百来 px。
    title: '权限说明', key: 'perm', width: 320,
    render: (u: any) => {
      const t = u.role === 'admin' ? '可执行全部操作：启停、部署、终端、文件、用户管理' : '只读：可查看全部监控/日志/数据，不能执行任何写操作';
      return h(NText, { depth: 3, class: 'cp-prose', style: 'font-size:var(--fs-xs)', title: t }, () => t);
    },
  },
  {
    title: '操作', key: 'ops', width: 190, align: 'center', fixed: 'right' as const,
    // 1Panel 的操作列是纯文字按钮 + 5px gap，不带图标；图标版留给工具栏。
    render: (u: any) =>
      h('div', { class: 'cp-ops' }, [
        h(NButton, {
          text: true, size: 'small', type: 'primary', class: 'cp-op',
          loading: opBusy(u.id, 'role'), disabled: rowBusy(u.id),
          onClick: () => toggleRole(u),
        }, () => (u.role === 'admin' ? '降为只读' : '升为管理')),
        h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', disabled: rowBusy(u.id), onClick: () => openReset(u) }, () => '重置密码'),
        h(NPopconfirm, { onPositiveClick: () => delUser(u) }, {
          trigger: () => h(NButton, {
            text: true, size: 'small', type: 'error', class: 'cp-op',
            loading: opBusy(u.id, 'del'), disabled: rowBusy(u.id),
            title: `删除用户 ${u.username}`,
          }, { default: () => '删除' }),
          default: () => `删除用户 ${u.username}？其已登录会话将立即失效。`,
        }),
      ]),
  },
];

// 用户量小（面板就几个账号），分页只是兜住"批量导入过用户"的极端情况
const paging = { pageSize: 20, showSizePicker: false };

const filtered = computed(() => {
  if (!kw.value) return users.value;
  const k = kw.value.toLowerCase();
  return users.value.filter((u) => `${u.username} ${u.nickname || ''}`.toLowerCase().includes(k));
});
</script>

<template>
  <div>
    <PageHeader title="用户管理" sub="管理员可执行全部操作；只读账号可看监控/日志/数据，但不能启停、部署、删除或使用终端。两步验证由各账号在设置页自行开关，这里只显示状态">
      <template #actions>
        <NInput v-model:value="kw" placeholder="搜索用户名/昵称" size="small" clearable style="width: 180px; max-width: 100%">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NButton size="small" tertiary :loading="busy" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
        <NButton size="small" type="primary" :disabled="creating" :icon="icon('AddOutline', 14)" @click="showUser = true">新建用户</NButton>
      </template>
    </PageHeader>

    <NCard size="small">
      <NDataTable size="small" :bordered="false" :loading="loading" :scroll-x="'auto'" :row-key="(u: any) => u.id" :pagination="paging" :columns="cols" :data="filtered">
        <template #empty><EmptyBox :text="error ? '用户列表读取失败，点上方「刷新」重试' : '暂无用户'" /></template>
      </NDataTable>
    </NCard>

    <NModal v-model:show="showUser" preset="card" title="新建用户" style="width: 420px; max-width: 94vw">
      <NForm label-placement="left" label-width="70">
        <NSpace vertical :size="12">
          <NFormItem label="用户名"><NInput v-model:value="userForm.username" placeholder="字母/数字/-/_，3-32 位" maxlength="32" :input-props="{ 'aria-label': '用户名' }" /></NFormItem>
          <NFormItem label="密码"><NInput v-model:value="userForm.password" type="password" show-password-on="click" placeholder="8–128 位" maxlength="128" :input-props="{ 'aria-label': '密码' }" /></NFormItem>
          <NFormItem label="角色">
            <NSelect v-model:value="userForm.role" :options="[{ label: '只读 viewer', value: 'viewer' }, { label: '管理员 admin', value: 'admin' }]" aria-label="用户角色" />
          </NFormItem>
          <NButton type="primary" block class="cp-press" :loading="creating" :disabled="creating" :icon="icon('AddOutline', 14)" @click="createUser">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>

    <NModal v-model:show="showReset" preset="card" :title="`重置密码 · ${resetTarget?.username || ''}`" style="width: 420px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NText depth="3" style="font-size: var(--fs-xs)">重置后该用户的旧登录令牌立即失效，需要重新登录。</NText>
        <NInput v-model:value="resetPwd" type="password" show-password-on="click" placeholder="新密码（8–128 位）" maxlength="128" :input-props="{ 'aria-label': '新密码' }" @keyup.enter="confirmReset" />
        <NSpace justify="end">
          <NButton tertiary @click="showReset = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="resetting" :disabled="resetting" :icon="icon('SaveOutline', 14)" @click="confirmReset">确认重置</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>
