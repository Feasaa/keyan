import { ApiClient, ApiError, readSSE } from './api-client.mjs';
import { renderMarkdown } from './markdown.mjs';

const $ = id => document.getElementById(id);
const state = { current: null, title: '', messages: [], conversations: [], cursor: null, before: null, busy: false, loading: false, hint: true, controller: null, pending: null, authMode: 'login', config: null, captcha: null, editing: null, deleting: null, toastTimer: null, codeUntil: 0, codeSending: false };
const hintPrefix = '【逐步提示学习方式】请先参考我的尝试，只给一小步提示，等我反馈后再深入；需要时提醒我用教材、计算、运行或仿真核验。\n\n';
const api = new ApiClient({ onExpire: () => { resetSession(); toast('登录已过期，请重新登录。'); openAuth(); } });
function icon(name) { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'), u = document.createElementNS(s.namespaceURI, 'use'); u.setAttribute('href', '#i-' + name); s.append(u); return s; }
function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4000); }
function banner(text = '', resumable = false) { $('statusBanner').hidden = !text; $('statusText').textContent = text; $('resumeBtn').hidden = !resumable; }
async function copy(text) { try { await navigator.clipboard.writeText(text); toast('已复制'); } catch { toast('浏览器不允许自动复制，请选中文字复制。'); } }
function closeSidebar() { $('sidebar').classList.remove('open'); $('scrim').hidden = true; document.querySelector('main').inert = false; syncSidebar(); }
function syncSidebar() {
  const mobile = matchMedia('(max-width:900px)').matches;
  $('sidebar').inert = mobile ? !$('sidebar').classList.contains('open') : document.body.classList.contains('sidebar-closed');
  $('menuBtn').setAttribute('aria-expanded', String(mobile ? $('sidebar').classList.contains('open') : !document.body.classList.contains('sidebar-closed')));
}
function toggleSidebar() {
  if (matchMedia('(max-width:900px)').matches) { const open = $('sidebar').classList.toggle('open'); $('scrim').hidden = !open; document.querySelector('main').inert = open; }
  else { const closed = document.body.classList.toggle('sidebar-closed'); $('sidebar').classList.toggle('collapsed', closed); }
  syncSidebar();
}
function updateAccount() {
  const u = api.auth?.user;
  $('accountName').textContent = u?.nickname || u?.email || '登录，保存你的思路';
  $('accountHint').textContent = u ? '已登录 · 继续你的学习' : '在不同对话间继续探索';
  $('avatar').textContent = (u?.nickname || u?.email || '启').slice(0, 1).toUpperCase();
  $('logoutBtn').hidden = !api.loggedIn;
}
function controls() {
  const blocked = state.loading;
  $('sendBtn').disabled = blocked || (!state.busy && (!$('question').value.trim() || Boolean(state.pending)));
  $('sendBtn').classList.toggle('stopping', state.busy);
  $('sendBtn').setAttribute('aria-label', state.busy ? '停止接收回答' : '发送消息');
  $('sendBtn').title = state.busy ? '停止接收回答' : '发送消息';
  $('question').disabled = blocked;
  $('olderMessages').disabled = state.busy || blocked;
  $('exportBtn').disabled = !state.messages.length;
}
function setBusy(value) { state.busy = value; controls(); }
function scrollBottom(force = false) { const v = $('viewport'); if (force || v.scrollHeight - v.scrollTop - v.clientHeight < 180) v.scrollTop = v.scrollHeight; }
function showPage() {
  $('welcome').hidden = Boolean(state.messages.length || state.current);
  $('thread').hidden = !($('welcome').hidden);
  $('conversationTitle').textContent = state.title || '学习，从一个好问题开始';
  $('olderMessages').hidden = !state.before;
  controls();
}
function resetSession() { state.controller?.abort(); state.pending = null; state.current = null; state.title = ''; state.messages = []; state.conversations = []; state.cursor = null; state.before = null; state.loading = false; renderHistory(); renderMessages(); updateAccount(); banner(); }
function newChat() {
  if (state.busy || state.loading) return toast('请先停止接收，或等当前操作完成。');
  state.current = null; state.title = ''; state.messages = []; state.before = null; state.pending = null;
  renderMessages(); renderHistory(); banner(); closeSidebar(); $('question').focus();
}
function renderHistory() {
  const list = $('history'); list.replaceChildren();
  $('moreHistory').hidden = true;
  if (!api.loggedIn) { list.append(node('p', 'history-empty', '登录后，你的对话会保存在这里。')); return; }
  if (!state.conversations.length) { list.append(node('p', 'history-empty', '还没有对话。\n从一个问题开始吧。')); }
  for (const c of state.conversations) {
    const item = node('div', 'history-item' + (c.id === state.current ? ' selected' : ''));
    const select = node('button', 'history-select', c.title || '新的对话'); select.title = c.title; select.onclick = () => openConversation(c);
    const rename = node('button', 'icon-btn'); rename.append(icon('edit')); rename.setAttribute('aria-label', '重命名：' + c.title); rename.onclick = () => { if (state.busy) return toast('请先停止接收。'); state.editing = c.id; $('editInput').value = c.title; $('editDialog').showModal(); };
    const del = node('button', 'icon-btn'); del.append(icon('trash')); del.setAttribute('aria-label', '删除：' + c.title); del.onclick = () => { if (state.busy) return toast('请先停止接收。'); state.deleting = c.id; $('deleteDialog').showModal(); };
    item.append(select, rename, del); list.append(item);
  }
  $('moreHistory').hidden = !state.cursor;
}
async function loadHistory(more = false) {
  if (!api.loggedIn) return;
  const owner = api.auth?.user?.id;
  try {
    const r = await api.json('/conversations?limit=20' + (more && state.cursor ? '&cursor=' + encodeURIComponent(state.cursor) : ''));
    if (owner !== api.auth?.user?.id) return;
    state.conversations = [...new Map([...(more ? state.conversations : []), ...r.items].map(c => [c.id, c])).values()];
    state.cursor = r.next_cursor; renderHistory();
  } catch (e) { toast(e.message); }
}
function displayContent(m) { return m.role === 'user' && m.content.startsWith(hintPrefix) ? m.content.slice(hintPrefix.length) : m.content; }
function addMessage(m) {
  const article = node('article', 'message ' + m.role); article.dataset.role = m.role;
  if (m.role === 'user') { article.append(node('div', 'user-bubble', displayContent(m))); }
  else if (m.role === 'assistant') {
    const head = node('div', 'assistant-heading'), logo = node('img'); logo.src = '/icon.svg'; logo.alt = ''; head.append(logo, node('span', '', '启学'));
    const body = node('div', 'message-body'); article.append(head, body); m.bodyEl = body;
    const actions = node('div', 'message-actions'); m.actionsEl = actions;
    const status = node('p', 'message-status'); m.statusEl = status;
    article.append(actions, status); updateMessage(m);
  } else return;
  $('messages').append(article);
}
function updateMessage(m) {
  if (!m.bodyEl) return;
  if (m.content) renderMarkdown(m.content, m.bodyEl, copy);
  else { const t = node('span', 'typing', m.status === 'streaming' ? '正在思考 ···' : ''); m.bodyEl.replaceChildren(t); }
  m.statusEl.textContent = m.notice || ({ interrupted: '回答未完整显示，可继续接收或重新打开会话查看保存内容。', failed: '这次回答未完成，可调整问题后重新提问。' }[m.status] || '');
  m.statusEl.hidden = !m.statusEl.textContent;
  const actions = m.actionsEl; actions.replaceChildren();
  if (!m.content || m.status === 'streaming') return;
  const c = node('button', 'icon-btn'); c.append(icon('copy')); c.title = '复制回答'; c.setAttribute('aria-label', c.title); c.onclick = () => copy(m.content); actions.append(c);
  if (m.id && !m.local) for (const [rating, label] of [['up', '有帮助'], ['down', '帮助不大']]) {
    const b = node('button', 'icon-btn' + (m.feedback === rating ? ' rated' : '')); b.append(icon(rating)); b.title = label; b.setAttribute('aria-label', label); b.setAttribute('aria-pressed', String(m.feedback === rating));
    b.onclick = async () => { b.disabled = true; try { const r = await api.json('/messages/' + encodeURIComponent(m.id) + '/feedback', { method: 'POST', body: { rating: m.feedback === rating ? null : rating } }); m.feedback = r.rating; updateMessage(m); } catch (e) { toast(e.message); b.disabled = false; } }; actions.append(b);
  }
}
function renderMessages() { $('messages').replaceChildren(); for (const m of state.messages) addMessage(m); showPage(); }
async function openConversation(c, older = false) {
  if (state.busy || state.loading) return toast('请先停止接收，或等待消息加载完成。');
  state.loading = true; controls(); const oldHeight = $('viewport').scrollHeight;
  try {
    const r = await api.json('/conversations/' + encodeURIComponent(c.id) + '/messages?limit=50' + (older && state.before ? '&before_seq=' + state.before : ''));
    state.current = c.id; state.title = c.title; state.before = r.next_before_seq;
    state.messages = older ? [...r.items, ...state.messages] : r.items; state.pending = null;
    const generating = state.messages.findLast(m => m.role === 'assistant' && m.status === 'streaming');
    if (generating) { state.pending = { message: generating, conversationId: c.id, lastId: null }; banner('这条回答尚在生成，可继续接收已生成内容。', true); } else banner();
    renderMessages(); renderHistory(); closeSidebar();
    if (older) $('viewport').scrollTop += $('viewport').scrollHeight - oldHeight; else scrollBottom(true);
  } catch (e) { toast(e.message); } finally { state.loading = false; controls(); }
}
function parseEvent(frame) { try { return JSON.parse(frame.data); } catch { throw new ApiError('流式数据格式有误，请重新读取会话。'); } }
async function receiveTurn(pending, initialMessage) {
  state.controller = new AbortController(); setBusy(true); banner(); let terminal = false;
  const m = pending.message;
  try {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const resuming = Boolean(pending.messageId);
        const response = await api.request(resuming ? '/chat/stream/' + encodeURIComponent(pending.messageId) : '/chat/stream', {
          method: resuming ? 'GET' : 'POST', body: resuming ? undefined : { message: initialMessage, ...(state.current ? { conversation_id: state.current } : {}) },
          headers: resuming && pending.lastId ? { 'Last-Event-ID': pending.lastId } : {}, signal: state.controller.signal
        });
        if (resuming && !pending.lastId) m.content = '';
        await readSSE(response, frame => {
          if (frame.id && frame.id === pending.lastId) return;
          const nearBottom = $('viewport').scrollHeight - $('viewport').scrollTop - $('viewport').clientHeight < 180;
          if (frame.event === 'start' || frame.event === 'meta') {
            const d = parseEvent(frame); pending.messageId = d.message_id; pending.conversationId = d.conversation_id;
            state.current = d.conversation_id; m.id = d.message_id; m.local = false; showPage();
          } else if (frame.event === 'delta') { m.content += parseEvent(frame).delta; }
          else if (frame.event === 'done') { m.status = 'complete'; terminal = true; }
          else if (frame.event === 'error') { const d = parseEvent(frame); terminal = true; throw new ApiError(d.message, 0, d.code); }
          else if (frame.event === 'shutdown') throw new ApiError('服务正在重启，可稍后继续接收。');
          if (frame.id) pending.lastId = frame.id;
          updateMessage(m); if (nearBottom) scrollBottom(true);
          return !terminal;
        }, { signal: state.controller.signal });
        if (!terminal) throw new ApiError('回答连接中断。');
        break;
      } catch (e) {
        if (e.name === 'AbortError' || state.controller.signal.aborted) throw e;
        if (e.status === 410) {
          m.status = 'interrupted'; m.notice = '续传窗口已过期，请重新打开此会话查看后端保存的回答。'; terminal = true; throw e;
        }
        if (pending.messageId && !e.code && !e.status && attempt < 2) { banner('连接中断，正在接回这一条回答…'); continue; }
        throw e;
      }
    }
    if (terminal) { m.notice = ''; state.pending = null; banner(); }
  } catch (e) {
    const cancelled = state.controller.signal.aborted;
    if (e.status === 410) {
      state.pending = null; m.status = 'interrupted';
      banner(m.notice);
    } else if (pending.messageId && !terminal && api.loggedIn) {
      state.pending = pending; m.status = 'interrupted'; m.notice = '';
      banner(cancelled ? '已停止接收，服务端可能继续短暂生成。可继续接收，或新建对话。' : e.message, true);
    } else {
      state.pending = null; m.status = 'failed';
      m.notice = cancelled ? '已停止接收。请求可能已经提交，可刷新历史确认后再提问。' : e.message;
      banner(m.notice);
    }
  } finally {
    updateMessage(m); state.controller = null; setBusy(false);
    if (api.loggedIn) await loadHistory();
  }
}
async function submitQuestion(text) {
  if (state.busy || state.loading || !text.trim()) return;
  if (!api.loggedIn) { openAuth(); return; }
  if (state.pending) return toast('请先继续接收上一条回答，或开启新对话。');
  const question = text.trim(); if (question.length > 7800) return toast('请把问题缩短到7800字以内。');
  const user = { role: 'user', content: question, local: true }, assistant = { role: 'assistant', content: '', status: 'streaming', local: true };
  state.messages.push(user, assistant); if (!state.title) state.title = question.slice(0, 28);
  addMessage(user); addMessage(assistant); showPage(); $('question').value = ''; $('question').style.height = '';
  scrollBottom(true);
  await receiveTurn({ message: assistant, conversationId: state.current, lastId: null }, (state.hint ? hintPrefix : '') + question);
}
function authMode(mode) {
  state.authMode = mode; const register = mode === 'register'; $('registration').hidden = !register; $('emailCode').required = register;
  $('password').minLength = register ? 8 : 1; $('password').autocomplete = register ? 'new-password' : 'current-password';
  $('password').placeholder = register ? '设置8—128位密码' : '输入密码'; $('authSubmit').textContent = register ? '创建账号' : '登录'; $('authError').textContent = '';
  for (const [id, selected] of [['loginTab', !register], ['registerTab', register]]) { $(id).classList.toggle('selected', selected); $(id).setAttribute('aria-selected', String(selected)); }
}
function openAuth() { closeSidebar(); if (!$('authDialog').open) $('authDialog').showModal(); }
async function loggedIn(result) { api.save(result); updateAccount(); $('authDialog').close(); $('authForm').reset(); banner(); await loadHistory(); controls(); toast('登录成功，可以开始提问了。'); $('question').focus(); }
async function captchaTicket() {
  if (!state.config) throw new Error('页面配置尚未加载，请稍后重试。');
  if (!state.config.captchaEnabled) return {};
  if (location.protocol !== 'https:') throw new Error('此后端启用了人机验证。注册需要使用HTTPS页面；已有账号可直接登录。设置方法见使用说明。');
  if (!state.config.captchaVid) throw new Error('人机验证尚未配置，请联系项目负责人。');
  if (!window.vaptcha) {
    await new Promise((resolve, reject) => { const s = document.createElement('script'); s.src = 'https://c4.vaptcha.com/src/v4.js'; s.onload = resolve; s.onerror = () => { s.remove(); reject(new Error('人机验证暂时无法加载，请检查网络。')); }; document.head.append(s); });
  }
  if (!state.captcha) state.captcha = await window.vaptcha({ vid: state.config.captchaVid, container: '#vaptcha-container', lang: 'zh-CN' });
  const result = await state.captcha.validate();
  if (!result?.token || !result?.knock) throw new Error('请完成人机验证后重新获取验证码。');
  return Object.fromEntries(['token', 'knock', 'dfu', 'ip'].map(k => [k, result[k] || '']));
}
async function checkConnection() {
  $('connection').className = 'connection'; $('connection').lastElementChild.textContent = '正在连接';
  const r = await Promise.allSettled([api.json('/health/ready', { authenticated: false }), api.json('/chat/status', { authenticated: false })]);
  const [ready, chat] = r; const reachable = chat.status === 'fulfilled';
  $('connection').className = 'connection ' + (ready.status === 'fulfilled' && reachable ? 'online' : 'offline');
  $('connection').lastElementChild.textContent = ready.status === 'fulfilled' ? '后端已连接' : reachable ? '依赖待启动' : '后端未连接';
  $('backendState').textContent = ready.status === 'fulfilled' ? '服务与依赖就绪' : reachable ? '服务可达，依赖未就绪' : '未连接';
  $('modelState').textContent = reachable ? chat.value.configured ? chat.value.model : '未配置模型密钥' : '暂时无法检查';
}
function downloadChat() {
  if (!state.messages.length) return;
  const text = '# ' + (state.title || '启学对话') + '\n\nAI内容需独立核验。\n\n' + state.messages.map(m => `## ${m.role === 'user' ? '我' : '启学'}\n\n${displayContent(m)}${m.status && m.status !== 'complete' ? '\n\n（回答状态：' + m.status + '）' : ''}`).join('\n\n');
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' })), a = node('a'); a.href = url; a.download = '启学-对话记录.md'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$('menuBtn').onclick = toggleSidebar; $('scrim').onclick = closeSidebar; $('newChat').onclick = newChat;
$('question').oninput = () => { $('question').style.height = 'auto'; $('question').style.height = Math.min($('question').scrollHeight, 180) + 'px'; controls(); };
$('question').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (state.busy) return; submitQuestion($('question').value); } };
$('composer').onsubmit = e => { e.preventDefault(); if (state.busy) state.controller?.abort(); else submitQuestion($('question').value); };
$('hintMode').onclick = () => { state.hint = !state.hint; $('hintMode').classList.toggle('active', state.hint); $('hintMode').setAttribute('aria-pressed', String(state.hint)); };
document.querySelectorAll('[data-prompt]').forEach(b => { b.onclick = () => { $('question').value = b.dataset.prompt; $('question').dispatchEvent(new Event('input')); $('question').focus(); }; });
$('accountBtn').onclick = () => { if (api.loggedIn) $('settingsDialog').showModal(); else openAuth(); };
$('settingsBtn').onclick = () => { closeSidebar(); $('settingsDialog').showModal(); };
document.querySelectorAll('[data-close]').forEach(b => { b.onclick = () => $(b.dataset.close).close(); });
for (const d of document.querySelectorAll('dialog')) d.addEventListener('click', e => { if (e.target === d) { const r = d.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close(); } });
$('loginTab').onclick = () => authMode('login'); $('registerTab').onclick = () => authMode('register');
$('authForm').onsubmit = async e => {
  e.preventDefault(); if (state.busy) return; const button = $('authSubmit'); button.disabled = true; $('authError').textContent = '';
  try {
    const body = { email: $('email').value.trim().toLowerCase(), password: $('password').value };
    if (state.authMode === 'register') { body.nickname = $('nickname').value.trim(); body.code = $('emailCode').value; }
    const result = await api.json('/auth/' + state.authMode, { method: 'POST', body, authenticated: false }); await loggedIn(result);
  } catch (e) { $('authError').textContent = e.message; } finally { button.disabled = false; }
};
$('sendCode').onclick = async () => {
  if (!$('email').reportValidity()) return;
  if (state.codeSending || state.codeUntil > Date.now()) return;
  state.codeSending = true; $('sendCode').disabled = true; $('authError').textContent = '';
  try { const ticket = await captchaTicket(); const r = await api.json('/auth/verification-code', { method: 'POST', body: { email: $('email').value.trim().toLowerCase(), scene: 'register', ...ticket }, authenticated: false }); state.codeUntil = Date.now() + r.resend_after * 1000; toast('验证码已发送，请查看邮箱。'); }
  catch (e) { $('authError').textContent = e.message; } finally { state.codeSending = false; tickCode(); }
};
function tickCode() { const left = Math.ceil((state.codeUntil - Date.now()) / 1000); $('sendCode').disabled = state.codeSending || left > 0; $('sendCode').textContent = state.codeSending ? '正在发送…' : left > 0 ? left + '秒后重发' : '获取验证码'; }
setInterval(tickCode, 1000);
$('logoutBtn').onclick = async () => {
  if (state.busy) return toast('请先停止接收回答。');
  try { await api.logout(); resetSession(); $('settingsDialog').close(); toast('已退出登录'); }
  catch (e) { toast('退出未完成：' + e.message); }
};
$('refreshHistory').onclick = () => { if (api.loggedIn) loadHistory(); else openAuth(); };
$('moreHistory').onclick = async () => { $('moreHistory').disabled = true; await loadHistory(true); $('moreHistory').disabled = false; };
$('olderMessages').onclick = () => openConversation({ id: state.current, title: state.title }, true);
$('resumeBtn').onclick = async () => { if (state.pending && !state.busy) { const p = state.pending; p.message.status = 'streaming'; p.messageId ||= p.message.id; updateMessage(p.message); await receiveTurn(p); } };
$('editForm').onsubmit = async e => {
  e.preventDefault(); const title = $('editInput').value.trim(); if (!title) return;
  const submit = $('editForm').querySelector('[type=submit],.primary-btn'); submit.disabled = true;
  try { const r = await api.json('/conversations/' + encodeURIComponent(state.editing), { method: 'PATCH', body: { title } }); if (state.current === r.id) { state.title = r.title; showPage(); } $('editDialog').close(); await loadHistory(); } catch (e) { toast(e.message); } finally { submit.disabled = false; }
};
$('confirmDelete').onclick = async () => {
  $('confirmDelete').disabled = true;
  try { await api.json('/conversations/' + encodeURIComponent(state.deleting), { method: 'DELETE' }); if (state.current === state.deleting) newChat(); $('deleteDialog').close(); await loadHistory(); toast('会话已移除'); } catch (e) { toast(e.message); } finally { $('confirmDelete').disabled = false; }
};
$('exportBtn').onclick = downloadChat; $('checkConnection').onclick = checkConnection;
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('sidebar').classList.contains('open')) { closeSidebar(); $('menuBtn').focus(); } if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !document.querySelector('dialog[open]')) { e.preventDefault(); newChat(); } });
matchMedia('(max-width:900px)').addEventListener('change', () => { closeSidebar(); syncSidebar(); });
async function init() {
  updateAccount(); renderHistory(); showPage(); authMode('login'); syncSidebar();
  try { const r = await fetch('/frontend-config.json'); if (!r.ok) throw new Error(); state.config = await r.json(); } catch { state.config = null; toast('页面配置读取失败，请刷新重试。'); }
  const connection = checkConnection();
  if (api.loggedIn) {
    try { const u = await api.json('/auth/me'); if (api.auth) { api.save({ ...api.auth, user: u }); updateAccount(); await loadHistory(); } } catch (e) { toast(e.message); }
  }
  await connection;
}
init();
