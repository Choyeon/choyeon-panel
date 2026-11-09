<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { NInput, NSpace, NIcon, NAlert, useMessage } from 'naive-ui';
import { NButton } from '../ui';
import { api, saveLoginData } from '../api';
import { icons } from '../icons';
import LogoMark from '../components/LogoMark.vue';
import { checkAccount, USERNAME_HINT, PASSWORD_HINT, USERNAME_MAX_LENGTH, PASSWORD_MAX_LENGTH } from '../validation';

const router = useRouter();
// 登录/初始化的错误已经有就地 NAlert 说明错在哪；成功原来只靠跳转体现，
// 补一句 toast 让「进去了」这件事有一个明确的回执（provider 在 App.vue，跨路由不丢）。
const msg = useMessage();
const needsSetup = ref(false);
const username = ref('');
const password = ref('');
const password2 = ref('');
const err = ref('');
const loading = ref(false);
const errAlert = ref<HTMLElement | null>(null);
// /api/auth/status 决定这一页是「初始化」还是「登录」。服务刚起来还没就绪、
// 或者用户手快刷新时请求会失败，旧写法只留一条死错误、表单停在登录态，
// 于是全新安装看起来像「装完了却登不进去」。给三态 + 一个明确的重试出口。
const checking = ref(true);
const statusFailed = ref(false);
// 两步验证的输入框只在后端**要**的时候才出现：登录页不预先查"这个账号开没开 2FA"，
// 因为 /auth/login 是唯一免认证入口，任何提前分流的响应都会变成账号名单探测面。
const code = ref('');
const codeVisible = ref(false);
const codeInput = ref<any>(null);
// 位数与步长来自响应的 digits/period（唯一出处是 backend/app/totp.py），这里不写死数字。
const digits = ref(0);
const period = ref(0);

// 用户名由本机记忆提供：/auth/status 不再回显账号名，避免未登录就泄露管理员用户名
const LAST_USER = 'cp_last_user';

async function readStatus() {
  checking.value = true;
  statusFailed.value = false;
  err.value = '';
  try {
    const s = await api.status();
    needsSetup.value = s.needsSetup;
  } catch {
    statusFailed.value = true;
  } finally {
    checking.value = false;
  }
  if (!needsSetup.value && !statusFailed.value) {
    try {
      username.value = localStorage.getItem(LAST_USER) || '';
    } catch {
      /* 隐私模式下 localStorage 不可用，留空即可 */
    }
  }
}

onMounted(readStatus);

const FEATS = [
  'Node / Python 项目一键部署与回滚日志',
  'nginx 反代 + Let’s Encrypt 全自动',
  'PostgreSQL / Redis 管理与定时备份',
  '实时监控 · 网页终端 · 多用户权限',
];

function validate(): string {
  // 与用户管理页、后端 security.py 同一把尺子：用户名正则、密码 8-128 位、两次一致。
  const reason = checkAccount(username.value, password.value, needsSetup.value ? password2.value : undefined);
  if (reason) return reason;
  // 已经要过一次了就别再空着提交：这一轮无论如何都会被拒，白等一个 scrypt 的往返。
  if (codeVisible.value && !code.value) return '请输入验证器里的动态口令';
  return '';
}

function acceptTotpError(e: any): boolean {
  if (e?.code !== 'totp_required' && e?.code !== 'totp_invalid') return false;
  const p = e.payload || {};
  if (p.digits) digits.value = Number(p.digits);
  if (p.period) period.value = Number(p.period);
  codeVisible.value = true;
  // 口令错一次就清空：重放同一枚会被服务端按"已用过"拒掉，留着只让人对着同一个框发懵。
  if (e.code === 'totp_invalid') code.value = '';
  err.value = e.message || '请输入验证器里的动态口令';
  // 用户名和口令保持原样（它们已经对了），只把焦点交给新出现的这一格。
  nextTick(() => codeInput.value?.focus?.());
  return true;
}

async function submit() {
  // 回车走的是 form 的 submit，不受按钮 disabled 约束：连按两次回车会打两次 /login，
  // 第二次带着同样的凭据撞进登录限速，把一次成功的登录判成"尝试过多"。
  if (loading.value) return;
  err.value = validate();
  if (err.value) {
    // 出错时把焦点交给那条说明，键盘/读屏用户不用自己去找页面里飘出来的红字。
    nextTick(() => errAlert.value?.focus?.());
    return;
  }
  loading.value = true;
  try {
    const r = needsSetup.value
      ? await api.setup(username.value, password.value)
      : await api.login(username.value, password.value, code.value);
    saveLoginData(r);
    try {
      localStorage.setItem(LAST_USER, username.value);
    } catch {
      /* 存不下不影响登录 */
    }
    msg.success(needsSetup.value ? '管理员已创建，已自动登录' : '登录成功，欢迎回来');
    router.replace('/dashboard');
  } catch (e: any) {
    if (!acceptTotpError(e)) {
      err.value = e.message || '请求失败';
      nextTick(() => errAlert.value?.focus?.());
    }
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="wrap">
    <div class="bg-glow g1" aria-hidden="true"></div>
    <div class="bg-glow g2" aria-hidden="true"></div>

    <div class="left cp-rise" style="--i: 0">
      <div class="brand">
        <LogoMark :size="34" />
        <span>choyeon <b>panel</b></span>
      </div>
      <h1>服务器，<br />尽在掌握。</h1>
      <p class="tagline">应用部署 · systemd 服务 · 数据库 · 备份 · 监控告警 —— 一台机器，一个面板。</p>
      <ul class="feats">
        <li v-for="(f, i) in FEATS" :key="f" class="cp-rise" :style="`--i:${i + 2}`">
          <NIcon :component="icons.CheckmarkCircleOutline" :size="16" :color="'var(--cp-ok)'" />
          <span>{{ f }}</span>
        </li>
      </ul>
    </div>

    <div class="right cp-rise" style="--i: 1">
      <div class="card">
        <h2>{{ needsSetup ? '初始化面板' : '欢迎回来' }}</h2>
        <p class="sub">{{ needsSetup ? '创建管理员账号开始使用' : '登录以管理 choyeon 服务器' }}</p>

        <div v-if="err" id="cp-login-error" ref="errAlert" tabindex="-1" class="err-alert">
          <NAlert type="error" :bordered="false" size="small" role="alert" aria-live="assertive">{{ err }}</NAlert>
        </div>

        <!-- 状态没读回来之前不渲染表单：否则全新安装会先闪一下「欢迎回来」，
             再跳成「初始化面板」，用户以为页面坏了。失败时给一个能点的重试。 -->
        <div v-if="checking" class="status-pending" role="status">正在检测面板状态…</div>
        <template v-else-if="statusFailed">
          <NAlert type="error" :bordered="false" size="small" role="alert" class="err-alert">
            无法连接面板后端。全新安装时服务可能还在启动，请稍等几秒后重试。
          </NAlert>
          <NButton block size="large" :loading="checking" :disabled="checking" @click="readStatus">重新检测</NButton>
        </template>
        <form v-else @submit.prevent="submit">
          <NSpace vertical :size="16">
            <div class="field">
              <label for="cp-username">用户名</label>
              <NInput
                v-model:value="username"
                :placeholder="needsSetup ? USERNAME_HINT : '请输入用户名'"
                :maxlength="USERNAME_MAX_LENGTH"
                size="large"
                :autofocus="true"
                :input-props="{
                  id: 'cp-username',
                  name: 'username',
                  autocomplete: 'username',
                  'aria-describedby': 'cp-login-error',
                  'aria-invalid': err ? 'true' : undefined,
                }"
              />
            </div>
            <div class="field">
              <label for="cp-password">密码</label>
              <NInput
                v-model:value="password"
                type="password"
                size="large"
                show-password-on="click"
                :placeholder="PASSWORD_HINT"
                :maxlength="PASSWORD_MAX_LENGTH"
                :input-props="{
                  id: 'cp-password',
                  name: 'password',
                  autocomplete: needsSetup ? 'new-password' : 'current-password',
                  'aria-describedby': 'cp-login-error',
                  'aria-invalid': err ? 'true' : undefined,
                }"
              />
            </div>
            <div v-if="codeVisible" class="field">
              <label for="cp-code">动态口令<template v-if="period">（{{ period }} 秒换一枚）</template></label>
              <!-- maxlength 只能挂在 NInput 上：写成 :input-props="{ maxlength }" 时它既没变成 attribute
                   也没变成 maxLength 属性（线上探针实测 el.maxLength === -1），位数上限会静默失效。 -->
              <NInput
                ref="codeInput"
                v-model:value="code"
                size="large"
                :maxlength="digits || undefined"
                :placeholder="digits ? `验证器里的 ${digits} 位数字` : '验证器里的数字'"
                :input-props="{
                  id: 'cp-code',
                  name: 'otp-code',
                  autocomplete: 'one-time-code',
                  inputmode: 'numeric',
                }"
              />
              <p class="hint">这个账号开启了两步验证：口令已经对了，再填验证器 App 里当前那一枚。</p>
            </div>
            <div v-if="needsSetup" class="field">
              <label for="cp-password2">确认密码</label>
              <NInput
                v-model:value="password2"
                type="password"
                size="large"
                show-password-on="click"
                placeholder="再输入一次"
                :maxlength="PASSWORD_MAX_LENGTH"
                :input-props="{
                  id: 'cp-password2',
                  name: 'password2',
                  autocomplete: 'new-password',
                  'aria-describedby': 'cp-login-error',
                  'aria-invalid': err ? 'true' : undefined,
                }"
              />
            </div>
            <NButton
              type="primary"
              size="large"
              block
              attr-type="submit"
              class="cp-press"
              :loading="loading"
              :disabled="loading"
            >
              {{ needsSetup ? '创建并进入' : '登 录' }}
              <template #icon><NIcon :component="icons.ArrowForwardOutline" /></template>
            </NButton>
          </NSpace>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: var(--viewport-h);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6vw;
  background: var(--cp-bg);
  position: relative;
  overflow: hidden;
  padding: var(--space-5);
  box-sizing: border-box;
}
.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(120px);
  opacity: 0.22;
  pointer-events: none;
  /* 两团光斑慢慢游动：登录页是面板的第一眼，纯静的渐变底看着像没加载完。
     只动 transform（blur 结果在合成层里缓存，不重算滤镜），22s 一轮慢到
     不被察觉成"动画"，只留下"这页是活的"。系统开了减少动效时由全局规则停掉。 */
  animation: cp-glow-drift 22s var(--ease) infinite;
}
@keyframes cp-glow-drift {
  0%,
  100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(4%, -5%, 0) scale(1.08); }
}
.g1 { width: 520px; height: 520px; background: var(--cp-brand); top: -140px; left: -120px; }
/* 反向相位：两团同起同落会像整屏在呼吸，错开半轮才是"各自漂" */
.g2 { width: 460px; height: 460px; background: var(--cp-brand-accent); bottom: -160px; right: -100px; animation-delay: -11s; }
.left { max-width: 460px; min-width: 0; }
.right { width: 360px; max-width: 100%; }
.brand {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-6);
  color: var(--cp-text);
  font-size: var(--fs-lg);
}
.brand b { color: var(--cp-brand-soft); }
h1 {
  font-size: var(--fs-3xl);
  line-height: 1.3;
  margin: 0 0 var(--space-4);
  color: var(--cp-text-strong);
  letter-spacing: -0.01em;
}
.tagline { color: var(--cp-text-dim); font-size: var(--fs-md); margin: 0 0 var(--space-5); }
.feats {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  color: var(--cp-text-dim);
  font-size: var(--fs-sm);
}
.feats li { display: flex; align-items: center; gap: var(--space-2); }
.card {
  background: var(--cp-elevated);
  border: 1px solid var(--cp-border);
  border-radius: var(--radius-xl);
  padding: var(--space-6) var(--space-5);
  box-shadow: var(--shadow-pop);
}
html[data-theme='dark'] .card { background: color-mix(in srgb, var(--cp-elevated) 86%, transparent); backdrop-filter: blur(8px); }
.card h2 { margin: 0 0 var(--space-1); font-size: var(--fs-xl); color: var(--cp-text-strong); }
.err-alert { margin-bottom: var(--space-3); outline: none; }
.status-pending { padding: var(--space-5) 0; text-align: center; color: var(--cp-text-mute); font-size: var(--fs-sm); }
.sub { margin: 0 0 var(--space-5); color: var(--cp-text-mute); font-size: var(--fs-sm); }
.field label { display: block; font-size: var(--fs-xs); color: var(--cp-text-dim); margin-bottom: var(--space-2); }
.hint { margin: var(--space-2) 0 0; font-size: var(--fs-xs); color: var(--cp-text-mute); line-height: 1.6; }
@media (max-width: 900px) {
  .wrap { gap: 0; }
  .left { display: none; }
  .right { width: 100%; max-width: 420px; }
}
@media (max-width: 640px) {
  .wrap { padding: var(--space-4); }
  .card { padding: var(--space-5); }
  .card h2 { font-size: var(--fs-lg); }
  .sub { margin-bottom: var(--space-4); }
}
</style>
