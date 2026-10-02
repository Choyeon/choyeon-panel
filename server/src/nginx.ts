import { writeFileSync, unlinkSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync } from 'node:fs';
import { run, isDomain } from './util.js';

const CONF_DIR = '/etc/nginx/conf.d';
const TLS_DIR = '/etc/letsencrypt/live';

export interface VhostSpec {
  name: string;
  domain: string;
  port: number;
  proxyWebsocket?: boolean;
}

function hasCert(domain: string) {
  return existsSync(`${TLS_DIR}/${domain}/fullchain.pem`);
}

export function renderVhost(spec: VhostSpec): string {
  if (!isDomain(spec.domain)) throw new Error(`invalid domain: ${spec.domain}`);
  const ssl = hasCert(spec.domain);
  const proxyHeaders = `        proxy_pass http://127.0.0.1:${spec.port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;` + (spec.proxyWebsocket === false ? '' : `
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";`);
  const sslLines = ssl
    ? `    ssl_certificate ${TLS_DIR}/${spec.domain}/fullchain.pem;
    ssl_certificate_key ${TLS_DIR}/${spec.domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
`
    : '';
  const redirect = ssl
    ? `server {
    listen 80;
    listen [::]:80;
    server_name ${spec.domain};
    return 301 https://$host$request_uri;
}

`
    : '';
  return `# managed by choyeon-panel (${spec.name}) — do not edit by hand
${redirect}server {
    listen ${ssl ? '443 ssl' : '80'};
    listen ${ssl ? '[::]:443 ssl' : '[::]:80'};
${ssl ? sslLines : ''}    server_name ${spec.domain};
    client_max_body_size 100m;

    location / {
${proxyHeaders}
    }
}
`;
}

export function vhostPath(name: string) {
  return `${CONF_DIR}/panel-${name}.conf`;
}

export async function applyVhost(spec: VhostSpec) {
  mkdirSync(CONF_DIR, { recursive: true });
  const path = vhostPath(spec.name);
  const content = renderVhost(spec);
  writeFileSync(path, content);
  const check = await run('nginx', ['-t']);
  if (check.code !== 0) {
    unlinkSync(path);
    throw new Error(`nginx config test failed, rolled back: ${check.out}`);
  }
  const reload = await run('systemctl', ['reload', 'nginx']);
  if (reload.code !== 0) throw new Error(`nginx reload failed: ${reload.out}`);
}

export async function removeVhost(name: string) {
  const path = vhostPath(name);
  if (existsSync(path)) unlinkSync(path);
  const check = await run('nginx', ['-t']);
  if (check.code === 0) await run('systemctl', ['reload', 'nginx']);
}

export async function issueCert(domain: string, email?: string) {
  if (!isDomain(domain)) throw new Error(`invalid domain: ${domain}`);
  const args = ['--nginx', '-d', domain, '--non-interactive', '--redirect', '--agree-tos', '--no-eff-email'];
  if (email) args.splice(4, 0, '-m', email);
  else args.splice(4, 0, '--register-unsafely-without-email');
  const r = await run('certbot', args, { timeout: 300000 });
  if (r.code !== 0) throw new Error(`certbot failed: ${r.out}`);
  return r.out;
}

export async function renewCerts() {
  const r = await run('certbot', ['renew', '--non-interactive'], { timeout: 600000 });
  return r.out;
}

// ---------- 应用关联配置：发现 / 分析 / 编辑 / 快捷指令 ----------

export interface ConfigAnalysis {
  serverNames: string[];
  ssl: boolean;
  websocket: boolean;
  bodySize: string | null;
  httpsRedirect: boolean;
  proxyPorts: number[];
}

export function analyzeConfig(content: string): ConfigAnalysis {
  const names = new Set<string>();
  for (const m of content.matchAll(/server_name\s+([^;]+);/g)) {
    m[1].trim().split(/\s+/).forEach((n) => n && n !== '_' && names.add(n));
  }
  const proxyPorts = [...content.matchAll(/proxy_pass\s+https?:\/\/[\w.:-]*?:(\d{2,5})\s*;/g)].map((m) => +m[1]);
  // upstream 块：proxy_pass http://NAME 时解析 NAME 内的 server 端口
  const upstreams = new Map<string, number[]>();
  for (const m of content.matchAll(/upstream\s+([\w.-]+)\s*\{([^}]*)\}/g)) {
    const ports = [...m[2].matchAll(/server\s+[\w.:-]*?:(\d{2,5})/g)].map((x) => +x[1]);
    if (ports.length) upstreams.set(m[1], ports);
  }
  for (const m of content.matchAll(/proxy_pass\s+https?:\/\/([\w.-]+)\s*;/g)) {
    const ups = upstreams.get(m[1]);
    if (ups) proxyPorts.push(...ups);
  }
  return {
    serverNames: [...names],
    ssl: /listen\s+443\s+ssl|ssl_certificate\s/.test(content),
    websocket: /proxy_set_header\s+Upgrade/.test(content),
    bodySize: (content.match(/client_max_body_size\s+([^;]+);/) || [])[1] ?? null,
    httpsRedirect: /return\s+30[123]\s+https/.test(content),
    proxyPorts: [...new Set(proxyPorts)],
  };
}

export function findAppConfigs(port: number | null, domain: string | null) {  const out: { file: string; name: string; content: string; analysis: ConfigAnalysis }[] = [];
  for (const d of ['/etc/nginx/conf.d', '/etc/nginx/sites-available']) {
    let list: string[] = [];
    try {
      list = readdirSync(d);
    } catch {}
    for (const f of list) {
      const p = `${d}/${f}`;
      let c = '';
      try {
        c = readFileSync(p, 'utf8');
      } catch {
        continue;
      }
      const a = analyzeConfig(c);
      if ((domain && a.serverNames.includes(domain)) || (port != null && a.proxyPorts.includes(port))) {
        out.push({ file: p, name: f, content: c, analysis: a });
      }
    }
  }
  return out;
}

export function domainsByPort(): Map<number, string[]> {
  const map = new Map<number, string[]>();
  for (const d of ['/etc/nginx/conf.d', '/etc/nginx/sites-available']) {
    let list: string[] = [];
    try {
      list = readdirSync(d);
    } catch {}
    for (const f of list) {
      let c = '';
      try {
        c = readFileSync(`${d}/${f}`, 'utf8');
      } catch {
        continue;
      }
      const a = analyzeConfig(c);
      for (const p of a.proxyPorts) {
        const arr = map.get(p) || [];
        for (const n of a.serverNames) if (!arr.includes(n)) arr.push(n);
        if (arr.length) map.set(p, arr);
      }
    }
  }
  return map;
}

function safeConfPath(p: string): string {
  const real = existsSync(p) ? realpathSync(p) : p;
  if (!real.startsWith('/etc/nginx/')) throw new Error('只能编辑 /etc/nginx 下的配置文件');
  if (!/^\/etc\/nginx\/(conf\.d|sites-available)\//.test(real)) throw new Error('仅支持 conf.d / sites-available 下的文件');
  if (/(\.sw.|~)$/.test(real)) throw new Error('非法文件名');
  return real;
}

export async function saveAppConfig(path: string, content: string) {
  const real = safeConfPath(path);
  if (!content.trim()) throw new Error('内容不能为空');
  if (content.length > 200000) throw new Error('文件过大');
  const old = existsSync(real) ? readFileSync(real, 'utf8') : null;
  writeFileSync(real, content);
  const check = await run('nginx', ['-t']);
  if (check.code !== 0) {
    if (old != null) writeFileSync(real, old);
    throw new Error(`nginx -t 校验失败，已回滚：\n${check.out}`);
  }
  const reload = await run('systemctl', ['reload', 'nginx']);
  if (reload.code !== 0) throw new Error(`nginx reload 失败：${reload.out}`);
  return { ok: true, file: real };
}

export async function quickEditConfig(path: string, kind: 'ws' | 'body' | 'redirect', value?: string) {
  const real = safeConfPath(path);
  let c = readFileSync(real, 'utf8');
  if (kind === 'ws') {
    if (/proxy_set_header\s+Upgrade/.test(c)) throw new Error('该配置已包含 WebSocket 头');
    const m = c.match(/^([ \t]*)proxy_pass[^;]+;/m);
    if (!m) throw new Error('未找到 proxy_pass，无法插入');
    c = c.replace(m[0], `${m[0]}\n${m[1]}proxy_set_header Upgrade $http_upgrade;\n${m[1]}proxy_set_header Connection "upgrade";`);
  } else if (kind === 'body') {
    if (!/^\d{1,4}[km]?$/i.test(value || '')) throw new Error('大小格式应如 50m / 1g');
    if (/client_max_body_size/.test(c)) c = c.replace(/client_max_body_size\s+[^;]+;/, `client_max_body_size ${value};`);
    else {
      const m = c.match(/^([ \t]*)server_name[^;]+;/m);
      if (!m) throw new Error('未找到 server_name，无法插入');
      c = c.replace(m[0], `${m[0]}\n${m[1]}client_max_body_size ${value};`);
    }
  } else {
    const a = analyzeConfig(c);
    if (!a.ssl) throw new Error('该配置尚未启用 HTTPS，请先在「域名 / SSL」申请证书');
    if (a.httpsRedirect) throw new Error('已存在 HTTPS 强制跳转');
    const name = a.serverNames[0];
    if (!name) throw new Error('无法确定 server_name');
    const block = `server {\n    listen 80;\n    listen [::]:80;\n    server_name ${name};\n    return 301 https://$host$request_uri;\n}\n\n`;
    const idx = c.search(/^server\s*\{/m);
    c = idx === -1 ? block + c : c.slice(0, idx) + block + c.slice(idx);
  }
  return saveAppConfig(real, c);
}
