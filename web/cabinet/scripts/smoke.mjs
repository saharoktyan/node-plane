// Walks the cabinet on mocks in a real browser: every page opens without
// errors in the console and shows what it should. Screenshots go to
// SMOKE_SHOTS when it is set.
//   NODE_PLANE_CHROMIUM=/usr/bin/chromium node scripts/smoke.mjs
import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { chromium } from 'playwright-core';

const port = 5199;
const base = `http://127.0.0.1:${port}`;
const shots = process.env.SMOKE_SHOTS;
if (shots) mkdirSync(shots, { recursive: true });

const server = spawn('npx', ['vite', 'dev', '--mode', 'mock', '--port', String(port), '--strictPort', '--host', '127.0.0.1'],
  { stdio: ['ignore', 'pipe', 'inherit'] });
await new Promise((resolve, reject) => {
  server.stdout.on('data', (chunk) => String(chunk).includes('Local') && resolve());
  server.on('exit', reject);
});

const browser = await chromium.launch({ executablePath: process.env.NODE_PLANE_CHROMIUM ?? '/usr/bin/chromium' });
const failures = [];
try {
  // A phone's width for the member: the side panel slides in over the page.
  for (const [scenario, role, lang, theme, width] of [['default', 'owner', 'ru', 'light', 1440],
    ['many', 'support', 'en', 'dark', 1440], ['empty', 'member', 'ru', 'dark', 390], ['errors', 'owner', 'en', 'light', 1100]]) {
    const context = await browser.newContext({ colorScheme: theme, viewport: { width, height: 800 } });
    const page = await context.newPage();
    const errors = [];
    // Answers the page expects are not errors: 401 before a refreshed
    // access token after a reload, 503 in the errors scenario.
    page.on('console', (message) => message.type() === 'error' && !/status of (401|503)/.test(message.text())
      && errors.push(message.text()));
    page.on('pageerror', (error) => errors.push(String(error)));
    await page.goto(`${base}/login?scenario=${scenario}&role=${role}&lang=${lang}`);
    await page.fill('input[autocomplete=username]', 'saharoktyan');
    await page.fill('input[type=password]', 'secret');
    await page.click('button.primary');
    await page.waitForURL(`${base}/`);
    const pages = ['/', '/inbox', '/settings', ...(role === 'member' ? [] : ['/admin', '/admin/users', '/admin/nodes',
      '/admin/services'])];
    if (scenario !== 'empty' && scenario !== 'errors') pages.splice(1, 0, '/services/6f1c1a52-8f3e-4c8e-9b8a-0f6f2d0c1a11');
    if (scenario === 'many') pages.splice(2, 0, '/services/9a7c2d1e-3b4f-4a5b-8c6d-7e8f9a0b1c22');
    for (const path of pages) {
      await page.goto(base + path);
      await page.waitForLoadState('networkidle');
      await page.waitForFunction(() => !document.body.innerText.includes('Загрузка…') && !document.body.innerText.includes('Loading…'));
      const text = await page.innerText('main');
      if (!text.trim()) failures.push(`${scenario}/${role} ${path}: empty page`);
      if (scenario === 'errors' && path !== '/settings' && !/unavailable|недоступ/i.test(text))
        failures.push(`${scenario}/${role} ${path}: no error shown`);
      if (shots) await page.screenshot({ path: `${shots}/${scenario}-${role}${path.replace(/\//g, '_') || '_'}.png`, fullPage: true });
    }
    // A configuration is issued on the VPN page and opens with its QR code.
    if (scenario === 'default') {
      await page.goto(base + '/services/6f1c1a52-8f3e-4c8e-9b8a-0f6f2d0c1a11');
      await page.getByRole('button', { name: 'VLESS · XHTTP' }).click();
      try {
        await page.waitForSelector('.config svg', { timeout: 5000 });
        if (!(await page.inputValue('.config textarea')).startsWith('vless://'))
          failures.push(`${scenario}/${role}: the configuration has no link`);
      } catch {
        failures.push(`${scenario}/${role}: the configuration did not open`);
      }
      if (shots) await page.screenshot({ path: `${shots}/${scenario}-${role}_config.png`, fullPage: true });
    }
    if (role === 'member') {
      await page.goto(base + '/admin');
      await page.waitForURL(`${base}/`);
    }
    for (const error of errors) failures.push(`${scenario}/${role}: ${error}`);
    await context.close();
  }
} finally {
  await browser.close();
  server.kill();
}
if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log('smoke: every page opened');
