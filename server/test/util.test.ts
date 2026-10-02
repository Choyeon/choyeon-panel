import { describe, expect, it } from 'vitest';
import { isBranch, isDomain, isGitUrl, isName, isPort, isUnit, shEscape } from '../src/util.js';

describe('isName (app names)', () => {
  it('accepts lowercase dns-like names', () => {
    expect(isName('ab')).toBe(true);
    expect(isName('my-app-2')).toBe(true);
    expect(isName('a'.repeat(38) + 'b')).toBe(true); // 39 chars max
  });
  it('rejects invalid names', () => {
    expect(isName('a')).toBe(false); // too short
    expect(isName('Abc')).toBe(false); // uppercase
    expect(isName('-abc')).toBe(false);
    expect(isName('abc; rm -rf /')).toBe(false);
    expect(isName('a'.repeat(40))).toBe(false);
    expect(isName('')).toBe(false);
  });
});

describe('isDomain', () => {
  it('accepts real domains', () => {
    expect(isDomain('example.com')).toBe(true);
    expect(isDomain('a.b.c.example.co.uk')).toBe(true);
    expect(isDomain('xn--fiqs8s.example')).toBe(true);
  });
  it('rejects invalid domains', () => {
    expect(isDomain('example')).toBe(false);
    expect(isDomain('-bad.com')).toBe(false);
    expect(isDomain('bad-.com')).toBe(false);
    expect(isDomain('a..b.com')).toBe(false);
    expect(isDomain('')).toBe(false);
  });
});

describe('isUnit (systemd unit names)', () => {
  it('accepts unit names', () => {
    expect(isUnit('nginx')).toBe(true);
    expect(isUnit('my-app.service')).toBe(true);
    expect(isUnit('getty@tty1.service')).toBe(true);
    expect(isUnit('user@1000.slice')).toBe(true);
  });
  it('rejects injection attempts', () => {
    expect(isUnit('nginx; reboot')).toBe(false);
    expect(isUnit('nginx&&ls')).toBe(false);
    expect(isUnit('')).toBe(false);
    expect(isUnit('a'.repeat(72))).toBe(false);
  });
});

describe('isPort', () => {
  it('allows unprivileged ports only', () => {
    expect(isPort(1024)).toBe(true);
    expect(isPort(3000)).toBe(true);
    expect(isPort(65535)).toBe(true);
  });
  it('rejects privileged or malformed ports', () => {
    expect(isPort(80)).toBe(false);
    expect(isPort(22)).toBe(false);
    expect(isPort(65536)).toBe(false);
    expect(isPort(3000.5)).toBe(false);
    expect(isPort(NaN)).toBe(false);
  });
});

describe('isBranch / isGitUrl', () => {
  it('validates branch names', () => {
    expect(isBranch('main')).toBe(true);
    expect(isBranch('feature/foo_1.2')).toBe(true);
    expect(isBranch('bad name')).toBe(false);
    expect(isBranch('a'.repeat(101))).toBe(false);
  });
  it('validates git urls', () => {
    expect(isGitUrl('https://github.com/user/repo.git')).toBe(true);
    expect(isGitUrl('git@github.com:user/repo.git')).toBe(true);
    expect(isGitUrl('/srv/local-repo')).toBe(true);
    expect(isGitUrl('file:///etc/passwd;rm -rf /')).toBe(false);
    expect(isGitUrl('https://x y')).toBe(false);
  });
});

describe('shEscape', () => {
  it('wraps values in single quotes safely', () => {
    expect(shEscape('plain')).toBe("'plain'");
    expect(shEscape("it's")).toBe(`'it'\\''s'`);
    expect(shEscape('a; rm -rf /')).toBe("'a; rm -rf /'");
  });
});
