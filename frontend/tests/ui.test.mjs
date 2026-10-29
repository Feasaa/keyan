import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { ApiClient, ApiError, readSSE } from '../api-client.mjs';

const user = { id: '373814368841760768', email: 'student@example.com', nickname: '同学' };
const auth = { user, token: { access_token: 'access-old', refresh_token: 'refresh-old' } };
const conv = { id: '473814368841760768', title: '测试会话' };
const reply = (data, status = 200) => new Response(JSON.stringify({ code: status < 400 ? 0 : 22410, message: '测试响应', data }), { status, headers: { 'Content-Type': 'application/json' } });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const stream = frames => new Response(frames, { headers: { 'Content-Type': 'text/event-stream' } });

async function setup(route = () => undefined, signedIn = true) {
  const dom = new JSDOM(readFileSync(new URL('../index.html', import.meta.url), 'utf8'), { url: 'http://127.0.0.1:4318', runScripts: 'outside-only' });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addEventListener() {} });
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.fetch = async (url, options = {}) => {
    const custom = await route(url, options); if (custom !== undefined) return custom;
    if (url === '/frontend-config.json') return Response.json({ captchaEnabled: false, captchaVid: '' });
    if (url.endsWith('/health/ready')) return reply({ status: 'ok' });
    if (url.endsWith('/chat/status')) return reply({ configured: true, model: 'test' });
    if (url.endsWith('/auth/me')) return reply(user);
    if (url.includes('/conversations?')) return reply({ items: [conv], next_cursor: null });
    if (url.endsWith('/auth/logout')) return reply(null);
    throw new Error('Unhandled test request: ' + url);
  };
  w.ApiClient = class extends ApiClient { constructor(options) { super({ ...options, fetcher: w.fetch, storage: w.sessionStorage }); } };
  w.ApiError = ApiError; w.readSSE = readSSE;
  if (signedIn) w.sessionStorage.setItem('qixue-auth-v1', JSON.stringify(auth));
  const markdown = readFileSync(new URL('../markdown.mjs', import.meta.url), 'utf8').replace('export function', 'function');
  const source = readFileSync(new URL('../app.js', import.meta.url), 'utf8').replace(/^import .*;\r?\n/gm, '').replace(/init\(\);\s*$/, 'globalThis.ready = init();');
  w.eval(markdown + '\n' + source + '\nglobalThis.testApp = { state, api, openConversation, submitQuestion, loadHistory, newChat, resetSession };');
  await w.ready;
  return { w, app: w.testApp, close: () => dom.window.close() };
}

test('Expired resume retains the partial answer already loaded from history', async () => {
  const t = await setup(url => {
    if (url.includes('/messages?')) return reply({ items: [{ id: '573814368841760768', role: 'assistant', content: '已经生成的部分回答', status: 'streaming' }], next_before_seq: null });
    if (url.includes('/chat/stream/')) return reply(null, 410);
  });
  try {
    await t.app.openConversation(conv);
    await t.w.document.getElementById('resumeBtn').onclick();
    assert.equal(t.app.state.messages[0].content, '已经生成的部分回答');
    assert.match(t.w.document.getElementById('statusText').textContent, /过期/);
    assert.equal(t.app.state.pending, null);
  } finally { t.close(); }
});

test('An SSE error ends the turn instead of offering to replay the same terminal error', async () => {
  const t = await setup(url => url.endsWith('/chat/stream') ? stream('event: start\ndata: {"conversation_id":"473814368841760768","message_id":"573814368841760768"}\n\nevent: error\nid: 3-0\ndata: {"code":22502,"message":"上游暂时不可用"}\n\n') : undefined);
  try {
    await t.app.submitQuestion('你好');
    assert.equal(t.app.state.pending, null);
    assert.equal(t.app.state.messages.at(-1).status, 'failed');
    assert.equal(t.w.document.getElementById('resumeBtn').hidden, true);
  } finally { t.close(); }
});

test('A history request completing after logout cannot restore private messages', async () => {
  const pending = deferred();
  const t = await setup(url => url.includes('/messages?') ? pending.promise : undefined);
  try {
    const opening = t.app.openConversation(conv);
    await t.w.document.getElementById('logoutBtn').onclick();
    pending.resolve(reply({ items: [{ role: 'assistant', content: '退出后不应显示', status: 'complete' }], next_before_seq: null }));
    await opening;
    assert.equal(t.app.state.messages.length, 0);
    assert.equal(t.app.state.current, null);
    assert.equal(t.w.document.getElementById('messages').textContent, '');
  } finally { t.close(); }
});

test('Markdown renders raw HTML and unsafe URLs as text, while preserving safe links and code', async () => {
  const t = await setup(undefined, false);
  try {
    const target = t.w.document.createElement('div');
    t.w.renderMarkdown('<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1)) [教材](https://example.com)\n\n```html\n<script>alert(1)</script>\n```', target, () => {});
    assert.equal(target.querySelectorAll('img,script').length, 0);
    assert.equal(target.querySelectorAll('a').length, 1);
    assert.equal(target.querySelector('a').protocol, 'https:');
    assert.match(target.querySelector('pre code').textContent, /<script>/);
  } finally { t.close(); }
});

test('Chat uses the real request contract; feedback, rename and deletion update the UI', async () => {
  const calls = []; let title = conv.title, deleted = false;
  const t = await setup((url, options) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, method: options.method, body });
    if (url.includes('/conversations?')) return reply({ items: deleted ? [] : [{ ...conv, title }], next_cursor: null });
    if (url.endsWith('/chat/stream')) return stream('event: start\ndata: {"conversation_id":"473814368841760768","message_id":"573814368841760768"}\n\nid: 1-0\nevent: delta\ndata: {"delta":"请先检查输入条件。"}\n\nid: 2-0\nevent: done\ndata: [DONE]\n\n');
    if (url.endsWith('/feedback')) return reply({ rating: body.rating });
    if (options.method === 'PATCH') { title = body.title; return reply({ ...conv, title }); }
    if (options.method === 'DELETE') { deleted = true; return reply(null); }
  });
  try {
    await t.app.submitQuestion('如何调试？');
    assert.equal(t.app.state.messages.at(-1).content, '请先检查输入条件。');
    assert.equal(t.app.state.messages.at(-1).status, 'complete');
    const post = calls.find(c => c.url.endsWith('/chat/stream'));
    assert.match(post.body.message, /逐步提示/);
    assert.equal('history' in post.body, false);
    await t.w.document.querySelector('[aria-label="有帮助"]').onclick();
    assert.equal(t.app.state.messages.at(-1).feedback, 'up');
    await t.w.document.querySelector('[aria-label="有帮助"]').onclick();
    assert.equal(t.app.state.messages.at(-1).feedback, null);
    t.w.document.querySelector('[aria-label^="重命名："]').click();
    t.w.document.getElementById('editInput').value = '调试笔记';
    await t.w.document.getElementById('editForm').onsubmit({ preventDefault() {} });
    assert.equal(t.w.document.getElementById('conversationTitle').textContent, '调试笔记');
    t.w.document.querySelector('[aria-label^="删除："]').click();
    await t.w.document.getElementById('confirmDelete').onclick();
    assert.equal(t.app.state.current, null);
    assert.equal(t.app.state.messages.length, 0);
    assert.equal(t.w.document.querySelectorAll('.history-item').length, 0);
  } finally { t.close(); }
});

test('Interrupted streaming reconnects with GET and Last-Event-ID, without another POST', async () => {
  const calls = [];
  const t = await setup((url, options) => {
    if (!url.includes('/chat/stream')) return;
    calls.push({ url, ...options });
    if (options.method === 'POST') return stream('event: start\ndata: {"conversation_id":"473814368841760768","message_id":"573814368841760768"}\n\nevent: delta\nid: 10-0\ndata: {"delta":"第一段"}\n\n');
    return stream('event: start\ndata: {"conversation_id":"473814368841760768","message_id":"573814368841760768"}\n\nevent: delta\nid: 11-0\ndata: {"delta":"第二段"}\n\nevent: done\nid: 12-0\ndata: [DONE]\n\n');
  });
  try {
    await t.app.submitQuestion('请分步解释');
    assert.equal(t.app.state.messages.at(-1).content, '第一段第二段');
    assert.equal(calls.length, 2);
    assert.equal(calls[1].method, 'GET');
    assert.equal(calls[1].headers['Last-Event-ID'], '10-0');
    assert.equal(calls.filter(c => c.method === 'POST').length, 1);
  } finally { t.close(); }
});

test('Older messages and more conversations use backend cursors without losing existing items', async () => {
  const urls = [];
  const t = await setup(url => {
    urls.push(url);
    if (url.includes('/conversations?')) return reply({ items: url.includes('cursor=') ? [{ id: '2', title: '更早会话' }] : [conv], next_cursor: url.includes('cursor=') ? null : 'page+/=' });
    if (url.includes('/messages?')) return reply({ items: [{ id: url.includes('before_seq=') ? 'old' : 'recent', role: 'user', content: '历史问题', status: 'complete' }], next_before_seq: url.includes('before_seq=') ? null : 21 });
  });
  try {
    await t.app.loadHistory(true);
    assert.equal(t.app.state.conversations.length, 2);
    assert.ok(urls.some(u => u.includes('cursor=page%2B%2F%3D')));
    await t.app.openConversation(conv);
    await t.app.openConversation(conv, true);
    assert.equal(t.app.state.messages.map(m => m.id).join(','), 'old,recent');
    assert.ok(urls.some(u => u.includes('before_seq=21')));
  } finally { t.close(); }
});

test('Anonymous questions open login; successful sign-in normalizes email and loads history', async () => {
  const calls = [];
  const t = await setup((url, options) => {
    calls.push({ url, body: options.body ? JSON.parse(options.body) : null });
    if (url.endsWith('/auth/login')) return reply(auth);
  }, false);
  try {
    await t.app.submitQuestion('未登录问题');
    assert.equal(t.w.document.getElementById('authDialog').open, true);
    assert.equal(calls.some(c => c.url.endsWith('/chat/stream')), false);
    t.w.document.getElementById('email').value = 'Student@Example.COM';
    t.w.document.getElementById('password').value = 'test-password';
    await t.w.document.getElementById('authForm').onsubmit({ preventDefault() {} });
    assert.equal(calls.find(c => c.url.endsWith('/auth/login')).body.email, 'student@example.com');
    assert.equal(t.app.api.loggedIn, true);
    assert.equal(t.w.document.getElementById('authDialog').open, false);
    assert.equal(t.app.state.conversations.length, 1);
  } finally { t.close(); }
});
