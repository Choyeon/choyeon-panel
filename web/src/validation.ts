/**
 * 账号规则的唯一前端出处，口径与 backend/app/security.py 的
 * USERNAME_RE / PW_MIN_LEN / PW_MAX_LEN 一致。
 *
 * 以前登录页只查「密码至少 8 位」、用户管理页查「8–128 位」，placeholder 一个写
 * 「字母、数字、下划线」另一个写「字母/数字/-/_」。后端拒了 128 位以上或含连字符
 * 以外的字符时，用户只能收到一条后端中文报错，却不知道是前端没拦住。这里把
 * 校验与提示文案收成一份，两个页面共用；后端仍会独立再验一次（前端校验只为反馈快）。
 */
export const USERNAME_MAX_LENGTH = 32;
export const PASSWORD_MAX_LENGTH = 128;
export const USERNAME_HINT = '3-32 位字母、数字、下划线或连字符';
export const PASSWORD_HINT = '8-128 位';

const USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/;
const PW_MIN_LENGTH = 8;

function usernameError(value: string): string {
  const v = value.trim();
  if (!v) return '请输入用户名';
  if (!USERNAME_RE.test(v)) return '用户名只能包含字母、数字、下划线与连字符（3-32 位）';
  return '';
}

export function passwordError(value: string, label = '密码'): string {
  if (!value) return `请输入${label}`;
  if (value.length < PW_MIN_LENGTH) return `${label}至少 ${PW_MIN_LENGTH} 位`;
  if (value.length > PASSWORD_MAX_LENGTH) return `${label}最多 ${PASSWORD_MAX_LENGTH} 位`;
  return '';
}

/**
 * 返回第一条可读的错误文案，空串表示可以提交。
 * `confirm` 只在初始化 / 改密这类需要二次输入时才传；登录不传。
 */
export function checkAccount(username: string, password: string, confirm?: string): string {
  return (
    usernameError(username) ||
    passwordError(password) ||
    (confirm !== undefined && confirm !== password ? '两次输入的密码不一致' : '')
  );
}
