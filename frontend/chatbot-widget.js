/**
 * TrackFundAI — AI Chatbot Widget
 *
 * Inject a floating chat button + panel into any page.
 * Usage: <script src="chatbot-widget.js"></script>
 *
 * Requirements: tfai_token must be in localStorage (auth guard in parent page).
 * API: POST /api/chatbot/query/  GET /api/chatbot/history/  POST /api/chatbot/<id>/feedback/
 */
(function () {
  'use strict';

  const API = (() => {
    const p = window.location.port;
    const same = (p === '8000' || p === '' || p === '80' || p === '443');
    if (same) return '/api';
    const backendPort = localStorage.getItem('tfai_backend_port') || '8000';
    return `http://127.0.0.1:${backendPort}/api`;
  })();

  /* ── Inject styles ────────────────────────────────────────── */
  const style = document.createElement('style');
  style.textContent = `
    /* ── Chatbot floating button ── */
    #tfai-chat-btn {
      position: fixed; bottom: 28px; right: 28px; z-index: 8000;
      width: 52px; height: 52px; border-radius: 50%; border: none;
      background: linear-gradient(135deg, #00d4ff, #0066cc);
      color: #fff; font-size: 22px; cursor: pointer;
      box-shadow: 0 4px 20px rgba(0,212,255,0.35);
      transition: transform 0.2s, box-shadow 0.2s;
      display: flex; align-items: center; justify-content: center;
    }
    #tfai-chat-btn:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 28px rgba(0,212,255,0.5);
    }
    #tfai-chat-btn .chat-badge {
      position: absolute; top: 0; right: 0; width: 16px; height: 16px;
      background: #ff4455; border-radius: 50%; font-size: 9px; font-weight: 700;
      display: none; align-items: center; justify-content: center; color: #fff;
    }

    /* ── Chat panel ── */
    #tfai-chat-panel {
      position: fixed; bottom: 90px; right: 28px; z-index: 8000;
      width: 380px; max-height: 75vh;
      background: var(--bg-card, #111120);
      border: 1px solid var(--border-subtle, rgba(255,255,255,0.08));
      border-radius: 16px;
      box-shadow: 0 12px 48px rgba(0,0,0,0.6);
      display: flex; flex-direction: column;
      overflow: hidden;
      transform: translateY(12px) scale(0.97);
      opacity: 0; pointer-events: none;
      transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1), opacity 0.2s;
    }
    #tfai-chat-panel.open {
      transform: translateY(0) scale(1);
      opacity: 1; pointer-events: all;
    }

    /* ── Panel header ── */
    .chat-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 16px;
      background: linear-gradient(135deg, rgba(0,100,204,0.25), rgba(0,212,255,0.1));
      border-bottom: 1px solid rgba(255,255,255,0.06);
      flex-shrink: 0;
    }
    .chat-header-left { display: flex; align-items: center; gap: 10px; }
    .chat-avatar {
      width: 32px; height: 32px; border-radius: 50%;
      background: linear-gradient(135deg, #00d4ff, #0066cc);
      display: flex; align-items: center; justify-content: center;
      font-size: 14px; flex-shrink: 0;
    }
    .chat-title { font-size: 14px; font-weight: 700; color: #e0e8ff; }
    .chat-subtitle { font-size: 11px; color: #6c7a99; }
    .chat-close-btn {
      background: none; border: none; color: #6c7a99; font-size: 20px;
      cursor: pointer; padding: 0; line-height: 1; transition: color 0.2s;
    }
    .chat-close-btn:hover { color: #e0e8ff; }

    /* ── Messages area ── */
    .chat-messages {
      flex: 1; overflow-y: auto; padding: 14px 14px 0;
      scroll-behavior: smooth;
    }
    .chat-messages::-webkit-scrollbar { width: 4px; }
    .chat-messages::-webkit-scrollbar-track { background: transparent; }
    .chat-messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }

    /* ── Message bubbles ── */
    .chat-msg { margin-bottom: 14px; display: flex; gap: 8px; align-items: flex-start; }
    .chat-msg.user { flex-direction: row-reverse; }
    .msg-avatar {
      width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700;
    }
    .chat-msg.bot  .msg-avatar { background: rgba(0,212,255,0.15); color: #00d4ff; }
    .chat-msg.user .msg-avatar { background: rgba(0,232,143,0.15); color: #00e88f; }
    .msg-content {
      max-width: 78%; padding: 10px 13px; border-radius: 12px; font-size: 13px;
      line-height: 1.5; color: #e0e8ff;
    }
    .chat-msg.bot  .msg-content { background: rgba(255,255,255,0.06); border-radius: 4px 12px 12px 12px; }
    .chat-msg.user .msg-content { background: rgba(0,100,204,0.25); border-radius: 12px 4px 12px 12px; color: #b8d4ff; }

    /* ── Intent chip ── */
    .intent-chip {
      display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 8px;
      border-radius: 6px; margin-bottom: 5px;
      background: rgba(0,212,255,0.12); color: #00d4ff;
      text-transform: uppercase; letter-spacing: 0.5px;
    }

    /* ── Feedback buttons ── */
    .msg-feedback {
      display: flex; gap: 4px; margin-top: 6px;
    }
    .fb-btn {
      background: none; border: 1px solid rgba(255,255,255,0.1);
      border-radius: 6px; padding: 2px 8px; font-size: 12px;
      cursor: pointer; color: #6c7a99; transition: all 0.2s;
    }
    .fb-btn:hover { background: rgba(255,255,255,0.06); color: #e0e8ff; }
    .fb-btn.active-up   { color: #00e88f; border-color: rgba(0,232,143,0.3); }
    .fb-btn.active-down { color: #ff4455; border-color: rgba(255,68,85,0.3); }

    /* ── Typing indicator ── */
    .typing-indicator {
      display: flex; gap: 4px; padding: 10px 13px; align-items: center;
    }
    .typing-dot {
      width: 6px; height: 6px; border-radius: 50%; background: #00d4ff;
      animation: typingBounce 1.2s ease-in-out infinite;
    }
    .typing-dot:nth-child(2) { animation-delay: 0.15s; }
    .typing-dot:nth-child(3) { animation-delay: 0.30s; }
    @keyframes typingBounce {
      0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
      40% { transform: translateY(-5px); opacity: 1; }
    }

    /* ── Suggestions ── */
    .chat-suggestions {
      padding: 10px 14px 0; display: flex; flex-wrap: wrap; gap: 6px;
      flex-shrink: 0;
    }
    .suggestion-chip {
      padding: 5px 12px; background: rgba(0,212,255,0.08);
      border: 1px solid rgba(0,212,255,0.2); border-radius: 14px;
      font-size: 11px; color: #00d4ff; cursor: pointer; transition: all 0.2s;
      white-space: nowrap;
    }
    .suggestion-chip:hover {
      background: rgba(0,212,255,0.18); border-color: rgba(0,212,255,0.4);
    }

    /* ── Input area ── */
    .chat-input-area {
      padding: 12px 14px 14px; flex-shrink: 0;
      border-top: 1px solid rgba(255,255,255,0.06);
    }
    .chat-input-row {
      display: flex; gap: 8px; align-items: center;
    }
    .chat-input {
      flex: 1; background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;
      color: #e0e8ff; padding: 9px 13px; font-size: 13px;
      font-family: inherit; resize: none; line-height: 1.4;
      transition: border-color 0.2s;
      min-height: 38px; max-height: 100px;
    }
    .chat-input:focus {
      outline: none; border-color: rgba(0,212,255,0.4);
    }
    .chat-input::placeholder { color: #6c7a99; }
    .chat-send-btn {
      width: 36px; height: 36px; border-radius: 50%; border: none; flex-shrink: 0;
      background: linear-gradient(135deg, #00d4ff, #0066cc);
      color: #fff; font-size: 16px; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.15s, opacity 0.2s;
    }
    .chat-send-btn:hover { transform: scale(1.08); }
    .chat-send-btn:disabled { opacity: 0.4; cursor: default; transform: none; }

    /* ── Responsive: narrow screens ── */
    @media (max-width: 440px) {
      #tfai-chat-panel { right: 12px; left: 12px; width: auto; }
    }

    /* ── Light theme overrides ── */
    [data-theme='light'] #tfai-chat-panel {
      background: #ffffff;
      border-color: rgba(0,0,0,0.12);
      box-shadow: 0 12px 48px rgba(0,0,0,0.15);
    }
    [data-theme='light'] .chat-header {
      background: linear-gradient(135deg, rgba(0,100,204,0.1), rgba(0,212,255,0.06));
      border-bottom-color: rgba(0,0,0,0.08);
    }
    [data-theme='light'] .chat-title { color: #1a1a2e; }
    [data-theme='light'] .chat-subtitle { color: #666; }
    [data-theme='light'] .chat-close-btn { color: #888; }
    [data-theme='light'] .chat-close-btn:hover { color: #333; }
    [data-theme='light'] .chat-messages::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.15); }
    [data-theme='light'] .msg-content { color: #1a1a2e; }
    [data-theme='light'] .chat-msg.bot .msg-content {
      background: #f0f4f8;
      color: #1a1a2e;
    }
    [data-theme='light'] .chat-msg.user .msg-content {
      background: rgba(0,100,204,0.12);
      color: #0a2540;
    }
    [data-theme='light'] .msg-content h3,
    [data-theme='light'] .msg-content h4,
    [data-theme='light'] .msg-content strong { color: #0a2540; }
    [data-theme='light'] .msg-content table { border-color: #d0d5dd; }
    [data-theme='light'] .msg-content th {
      background: #e8ecf1; color: #1a1a2e; border-color: #d0d5dd;
    }
    [data-theme='light'] .msg-content td {
      border-color: #d0d5dd; color: #333;
    }
    [data-theme='light'] .intent-chip {
      background: rgba(0,100,204,0.1); color: #0066cc;
    }
    [data-theme='light'] .fb-btn {
      border-color: rgba(0,0,0,0.12); color: #888;
    }
    [data-theme='light'] .fb-btn:hover {
      background: rgba(0,0,0,0.04); color: #333;
    }
    [data-theme='light'] .suggestion-chip {
      background: rgba(0,100,204,0.06);
      border-color: rgba(0,100,204,0.2);
      color: #0066cc;
    }
    [data-theme='light'] .suggestion-chip:hover {
      background: rgba(0,100,204,0.12);
      border-color: rgba(0,100,204,0.35);
    }
    [data-theme='light'] .chat-input-area {
      border-top-color: rgba(0,0,0,0.08);
    }
    [data-theme='light'] .chat-input {
      background: #f5f7fa;
      border-color: rgba(0,0,0,0.12);
      color: #1a1a2e;
    }
    [data-theme='light'] .chat-input:focus {
      border-color: rgba(0,100,204,0.4);
    }
    [data-theme='light'] .chat-input::placeholder { color: #999; }
    [data-theme='light'] .chat-msg.bot .msg-avatar {
      background: rgba(0,100,204,0.1); color: #0066cc;
    }
    [data-theme='light'] .chat-msg.user .msg-avatar {
      background: rgba(0,180,100,0.1); color: #00a862;
    }
    [data-theme='light'] .typing-dot { background: #0066cc; }
  `;
  document.head.appendChild(style);

  /* ── Build DOM ────────────────────────────────────────────── */
  const btn = document.createElement('button');
  btn.id = 'tfai-chat-btn';
  btn.title = 'Ask TrackFundAI';
  btn.innerHTML = `<span>💬</span><span class="chat-badge" id="chat-badge"></span>`;

  const panel = document.createElement('div');
  panel.id = 'tfai-chat-panel';
  panel.innerHTML = `
    <div class="chat-header">
      <div class="chat-header-left">
        <div class="chat-avatar">🤖</div>
        <div>
          <div class="chat-title">TrackFundAI Assistant</div>
          <div class="chat-subtitle">Ask about your portfolio, funds &amp; compliance</div>
        </div>
      </div>
      <button class="chat-close-btn" id="chat-close-btn" title="Close">&times;</button>
    </div>

    <div class="chat-messages" id="chat-messages"></div>

    <div class="chat-suggestions" id="chat-suggestions">
      <div class="suggestion-chip">Portfolio summary</div>
      <div class="suggestion-chip">Compliance status</div>
      <div class="suggestion-chip">Fund performance</div>
      <div class="suggestion-chip">Latest NAV</div>
      <div class="suggestion-chip">Top risks</div>
    </div>

    <div class="chat-input-area">
      <div class="chat-input-row">
        <textarea class="chat-input" id="chat-input"
          placeholder="Ask about your portfolio, funds, compliance…"
          rows="1"></textarea>
        <button class="chat-send-btn" id="chat-send-btn" title="Send">
          &#10148;
        </button>
      </div>
    </div>
  `;

  document.body.appendChild(btn);
  document.body.appendChild(panel);

  /* ── State ────────────────────────────────────────────────── */
  let isOpen = false;
  let isLoading = false;
  let historyLoaded = false;

  /* ── Toggle panel ─────────────────────────────────────────── */
  btn.addEventListener('click', () => {
    isOpen = !isOpen;
    panel.classList.toggle('open', isOpen);
    btn.querySelector('span:first-child').textContent = isOpen ? '✕' : '💬';
    if (isOpen && !historyLoaded) {
      loadHistory();
    }
  });

  document.getElementById('chat-close-btn').addEventListener('click', () => {
    isOpen = false;
    panel.classList.remove('open');
    btn.querySelector('span:first-child').textContent = '💬';
  });

  /* ── Suggestion chips ─────────────────────────────────────── */
  document.querySelectorAll('.suggestion-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.getElementById('chat-input').value = chip.textContent.trim();
      sendMessage();
    });
  });

  /* ── Input auto-resize + Enter to send ───────────────────── */
  const inputEl = document.getElementById('chat-input');
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + 'px';
  });
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  document.getElementById('chat-send-btn').addEventListener('click', sendMessage);

  /* ── Helpers ──────────────────────────────────────────────── */
  function getToken() {
    // Auth module stores token as 'tfai_access'; fallback to legacy key
    return localStorage.getItem('tfai_access') || localStorage.getItem('tfai_token');
  }
  function apiHeaders() {
    return { 'Authorization': `Bearer ${getToken()}`, 'Content-Type': 'application/json' };
  }

  const messagesEl = document.getElementById('chat-messages');

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function appendMessage(role, text, intent, messageId, chartData) {
    // Hide suggestions after first real message
    document.getElementById('chat-suggestions').style.display = 'none';

    const rendered = role === 'bot' ? mdToHtml(text) : escapeHtml(text);
    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    div.innerHTML = `
      <div class="msg-avatar">${role === 'bot' ? '🤖' : 'You'}</div>
      <div>
        ${intent && role === 'bot' ? `<div class="intent-chip">${intent.replace(/_/g,' ')}</div>` : ''}
        <div class="msg-content">${rendered}</div>
        ${role === 'bot' && messageId ? `
          <div class="msg-feedback">
            <button class="fb-btn" data-mid="${messageId}" data-helpful="true" title="Helpful">👍</button>
            <button class="fb-btn" data-mid="${messageId}" data-helpful="false" title="Not helpful">👎</button>
          </div>` : ''}
      </div>`;
    messagesEl.appendChild(div);

    // Render chart if provided
    if (chartData && chartData.labels && chartData.datasets && typeof Chart !== 'undefined') {
      const isLight = document.documentElement.getAttribute('data-theme') === 'light';
      const wrap = document.createElement('div');
      wrap.style.cssText = `margin-top:8px;padding:10px;background:${isLight ? 'rgba(0,0,0,0.04)' : 'rgba(0,0,0,0.2)'};border-radius:8px;`;
      const canvas = document.createElement('canvas');
      canvas.height = 160;
      wrap.appendChild(canvas);
      div.querySelector('.msg-content').appendChild(wrap);
      const tickColor = isLight ? '#555' : '#94a3b8';
      const gridColor = isLight ? '#e0e4ea' : '#1e293b';
      try {
        const ds = chartData.datasets.map((d,i) => ({
          label: d.label, data: d.data,
          backgroundColor: chartData.type==='doughnut' ? d.data.map((_,j)=>['#00d4ff','#7c3aed','#10b981','#f59e0b','#ef4444','#3b82f6'][j%6]) : (d.color||'#00d4ff')+'99',
          borderColor: d.color||'#00d4ff', borderWidth: 2, fill: chartData.type==='line', tension: 0.3,
        }));
        new Chart(canvas, {
          type: chartData.type||'bar', data:{labels:chartData.labels,datasets:ds},
          options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:tickColor,font:{size:9}}}},
            scales: chartData.type==='doughnut'?{}:{x:{ticks:{color:tickColor,font:{size:8}},grid:{color:gridColor}},y:{ticks:{color:tickColor,font:{size:8}},grid:{color:gridColor}}}},
        });
      } catch(e) {}
    }

    // Attach feedback handlers
    div.querySelectorAll('.fb-btn').forEach(fbBtn => {
      fbBtn.addEventListener('click', () => {
        const helpful = fbBtn.dataset.helpful === 'true';
        submitFeedback(fbBtn.dataset.mid, helpful, div);
      });
    });

    scrollToBottom();
    return div;
  }

  function appendTyping() {
    const div = document.createElement('div');
    div.className = 'chat-msg bot';
    div.id = 'typing-msg';
    div.innerHTML = `
      <div class="msg-avatar">🤖</div>
      <div class="msg-content typing-indicator">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
      </div>`;
    messagesEl.appendChild(div);
    scrollToBottom();
  }

  function removeTyping() {
    const t = document.getElementById('typing-msg');
    if (t) t.remove();
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
      .replace(/\n/g,'<br>');
  }

  function mdToHtml(s) {
    let h = escapeHtml(s);
    h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    h = h.replace(/\*(.+?)\*/g, '<em>$1</em>');
    h = h.replace(/^[•\-]\s+(.+)$/gm, '<li>$1</li>');
    h = h.replace(/(<li>.*<\/li>(<br>)?)+/g, m => '<ul>' + m.replace(/<br>/g,'') + '</ul>');
    return h;
  }

  /* ── Send message ─────────────────────────────────────────── */
  async function sendMessage() {
    const query = inputEl.value.trim();
    if (!query || isLoading) return;
    const token = getToken();
    if (!token) {
      appendMessage('bot', 'Please log in to use the chatbot.', null, null);
      return;
    }

    isLoading = true;
    document.getElementById('chat-send-btn').disabled = true;
    inputEl.value = '';
    inputEl.style.height = 'auto';

    appendMessage('user', query, null, null);
    appendTyping();

    const payload = { query };
    const fundId = localStorage.getItem('tfai_active_fund');
    if (fundId) payload.fund_id = fundId;

    try {
      const res = await fetch(`${API}/chatbot/query/`, {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify(payload),
      });

      removeTyping();

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        appendMessage('bot', err.detail || 'Sorry, something went wrong. Please try again.', null, null);
        return;
      }

      const data = await res.json();
      appendMessage('bot', data.response, data.intent, data.message_id, data.chart);
    } catch (e) {
      removeTyping();
      appendMessage('bot', 'Connection error. Please check your network and try again.', null, null);
    } finally {
      isLoading = false;
      document.getElementById('chat-send-btn').disabled = false;
    }
  }

  /* ── Load chat history ────────────────────────────────────── */
  async function loadHistory() {
    const token = getToken();
    if (!token) return;
    historyLoaded = true;

    try {
      const res = await fetch(`${API}/chatbot/history/`, { headers: apiHeaders() });
      if (!res.ok) return;
      const data = await res.json();

      if (!data.length) {
        // Show welcome message
        appendWelcome();
        return;
      }

      // Render last 10 messages in chronological order (API returns newest first)
      const recent = [...data].reverse().slice(-10);
      recent.forEach(m => {
        appendMessage('user', m.query, null, null);
        appendMessage('bot', m.response, m.intent, m.id);
      });
    } catch (e) {
      appendWelcome();
    }
  }

  function appendWelcome() {
    const div = document.createElement('div');
    div.className = 'chat-msg bot';
    div.innerHTML = `
      <div class="msg-avatar">🤖</div>
      <div>
        <div class="msg-content">
          Hello! I'm your TrackFundAI assistant. I can answer questions about your portfolio companies,
          fund performance, compliance status, LP information, and more.<br><br>
          Try asking: <em>"What's the IRR of our top-performing company?"</em> or
          <em>"Show me overdue SEBI filings"</em>.
        </div>
      </div>`;
    messagesEl.appendChild(div);
    scrollToBottom();
  }

  /* ── Submit feedback ──────────────────────────────────────── */
  async function submitFeedback(messageId, helpful, containerDiv) {
    const token = getToken();
    if (!token) return;
    try {
      await fetch(`${API}/chatbot/${messageId}/feedback/`, {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify({ helpful }),
      });
      // Update button styles
      containerDiv.querySelectorAll('.fb-btn').forEach(b => {
        const isThis = (b.dataset.helpful === 'true') === helpful;
        b.classList.toggle('active-up', isThis && helpful);
        b.classList.toggle('active-down', isThis && !helpful);
        b.disabled = true;
      });
    } catch (e) { /* silent fail */ }
  }

})();
