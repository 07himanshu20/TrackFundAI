/* ============================================================
   chatbot.js — hierarchy-scoped Gemini chat
   Window API: window.Chatbot.setScope(node)
============================================================ */

(() => {
  let currentScope = null;    // {id, name, level, ...}
  const messages = [];        // {role: 'user'|'model', content: str}

  const $ = (id) => document.getElementById(id);

  function setScope(node) {
    currentScope = node;
    const label = node && node.id
      ? `Scope: ${node.name} (${(node.level || '').toUpperCase()})`
      : 'Scope: Full portfolio';
    $('chat-scope-label').textContent = label;
  }

  async function send(text) {
    if (!text || !text.trim()) return;
    appendMessage('user', text);
    messages.push({role: 'user', content: text});

    const thinkingEl = appendMessage('assistant', '_Analysing…_', true);

    try {
      const body = {
        message: text,
        history: messages.slice(0, -1).map(m => ({
          role: m.role === 'assistant' ? 'model' : m.role,
          content: m.content,
        })),
        scope_id: currentScope && currentScope.id ? currentScope.id : null,
      };
      const res = await window.Portfolio.apiPost('/portfolio/chat/', body);
      thinkingEl.remove();
      appendMessage('assistant', res.reply || '(no reply)');
      messages.push({role: 'assistant', content: res.reply || ''});
    } catch (e) {
      thinkingEl.remove();
      appendMessage('assistant', `⚠️ ${e.message}`);
    }
  }

  function appendMessage(role, text) {
    const container = $('chat-messages');
    const row = document.createElement('div');
    row.className = `chat-message ${role}`;
    const avatar = document.createElement('div');
    avatar.className = 'chat-avatar';
    avatar.textContent = role === 'user' ? 'You' : 'AI';
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    if (role === 'assistant') {
      bubble.innerHTML = renderMarkdown(text);
      renderChartBlocks(bubble);
    } else {
      bubble.textContent = text;
    }
    row.appendChild(avatar);
    row.appendChild(bubble);
    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function renderMarkdown(text) {
    if (!window.marked) return escape(text);
    return marked.parse(text, {breaks: true});
  }

  function escape(s) {
    return String(s).replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));
  }

  function renderChartBlocks(bubble) {
    const codes = bubble.querySelectorAll('pre > code');
    codes.forEach(code => {
      const raw = code.textContent.trim();
      if (!raw.startsWith('{')) return;
      const looksLikeChart = code.className.includes('chart') ||
                             /"type"\s*:\s*"(bar|line|doughnut|pie)"/.test(raw);
      if (!looksLikeChart) return;
      try {
        const spec = JSON.parse(raw);
        const wrap = document.createElement('div');
        wrap.className = 'inline-chart-wrap';
        if (spec.title) {
          const t = document.createElement('div');
          t.className = 'chart-title';
          t.textContent = spec.title;
          wrap.appendChild(t);
        }
        const cv = document.createElement('canvas');
        cv.height = 220;
        wrap.appendChild(cv);
        code.parentElement.replaceWith(wrap);
        const palette = ['#00d4ff','#00ff9d','#ffb800','#a855f7','#ff4455','#06ffd6'];
        const datasets = (spec.datasets || []).map((d, i) => ({
          label: d.label,
          data: d.data,
          backgroundColor: palette[i % palette.length] + '99',
          borderColor: palette[i % palette.length],
          borderWidth: 2,
          tension: 0.3,
        }));
        new Chart(cv.getContext('2d'), {
          type: spec.type === 'line' ? 'line' : (spec.type || 'bar'),
          data: {labels: spec.labels || [], datasets},
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
              legend: {labels: {color: '#8888aa'}},
              tooltip: {
                callbacks: {
                  label: (c) => `${c.dataset.label}: ${window.Portfolio.formatNum(c.raw, spec.yFormat || 'USD')}`,
                },
              },
            },
            scales: (spec.type === 'doughnut' || spec.type === 'pie') ? {} : {
              x: {ticks: {color: '#8888aa'}, grid: {color: 'rgba(255,255,255,0.03)'}},
              y: {ticks: {color: '#8888aa', callback: v => window.Portfolio.formatNum(v, spec.yFormat || 'USD')}, grid: {color: 'rgba(255,255,255,0.05)'}},
            },
          },
        });
        if (spec.notes) {
          const n = document.createElement('div');
          n.className = 'chart-subtitle';
          n.textContent = spec.notes;
          wrap.appendChild(n);
        }
      } catch (err) {
        console.warn('chart block parse failed', err);
      }
    });
  }

  function init() {
    const input = $('chat-input');
    const sendBtn = $('chat-send');

    const sync = () => { sendBtn.disabled = !input.value.trim(); };
    input.addEventListener('input', sync);

    const submit = () => {
      const t = input.value.trim();
      if (!t) return;
      input.value = '';
      sync();
      send(t);
    };
    sendBtn.onclick = submit;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    });

    document.querySelectorAll('.suggestion-chip').forEach(c => {
      c.onclick = () => send(c.dataset.q || c.textContent);
    });

    const clr = $('chat-clear');
    if (clr) clr.onclick = () => {
      messages.length = 0;
      $('chat-messages').innerHTML = '';
      const greet = document.createElement('div');
      greet.className = 'chat-message assistant';
      greet.innerHTML = `<div class="chat-avatar">AI</div><div class="chat-bubble">Chat cleared. Ask me anything about the current scope.</div>`;
      $('chat-messages').appendChild(greet);
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.Chatbot = {setScope, send};
})();
