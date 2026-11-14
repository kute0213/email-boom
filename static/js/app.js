/* ============================================================
   EmailBoom 前端脚本 —— Toast / 图标 / 弹窗 / 轮询 / 表单辅助
   依赖：lucide.min.js（本地）、base.css 的 toast/modal 结构
   ============================================================ */
(function () {
    'use strict';

    /* ---------- Lucide 图标 ---------- */
    var ICON_MAP = { success: 'check-circle', error: 'x-circle', warning: 'alert-triangle', info: 'info' };

    function renderIcons(root) {
        if (typeof lucide === 'undefined' || !lucide.createIcons) return;
        try {
            lucide.createIcons({ icons: lucide.icons || undefined, root: root || document });
        } catch (e) {
            try { lucide.createIcons({ icons: lucide.icons || undefined }); } catch (e2) { /* noop */ }
        }
    }
    window.renderIcons = renderIcons;

    /* ---------- Toast ---------- */
    function showToast(type, message, duration) {
        type = ICON_MAP[type] ? type : 'info';
        var container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.className = 'toast-container';
            container.id = 'toast-container';
            container.setAttribute('aria-live', 'polite');
            document.body.appendChild(container);
        }
        var toast = document.createElement('div');
        toast.className = 'toast ' + type;
        toast.setAttribute('role', 'status');

        var icon = document.createElement('i');
        icon.className = 'w-5 h-5';
        icon.setAttribute('data-lucide', ICON_MAP[type]);

        var text = document.createElement('span');
        text.className = 'toast-text';
        text.textContent = message == null ? '' : String(message);

        toast.appendChild(icon);
        toast.appendChild(text);
        container.appendChild(toast);
        renderIcons(toast);

        var leaving = false;
        function remove() { if (toast.parentNode) toast.parentNode.removeChild(toast); }
        function dismiss() {
            if (leaving) return;
            leaving = true;
            toast.classList.add('is-leaving');
            setTimeout(remove, 480);
        }
        toast.addEventListener('click', dismiss);
        setTimeout(dismiss, typeof duration === 'number' ? duration : 3200);
    }

    window.Toast = {
        show: function (m, t, d) { showToast(t, m, d); },
        success: function (m, d) { showToast('success', m, d); },
        error: function (m, d) { showToast('error', m, d); },
        warning: function (m, d) { showToast('warning', m, d); },
        info: function (m, d) { showToast('info', m, d); }
    };

    /* ---------- 导航滚动收缩 ---------- */
    function initNav() {
        var nav = document.getElementById('glass-nav');
        if (!nav) return;
        function sync() { nav.classList.toggle('scrolled', window.scrollY > 24); }
        window.addEventListener('scroll', sync, { passive: true });
        sync();
    }

    /* ---------- 服务端 Flash → Toast ---------- */
    function initFlashes() {
        var el = document.getElementById('flash-data');
        if (!el) return;
        try {
            var items = JSON.parse(el.textContent || '[]');
            items.forEach(function (pair) {
                var cat = pair[0] || 'info';
                var map = { success: 'success', error: 'error', message: 'info', info: 'info', warning: 'warning' };
                showToast(map[cat] || 'info', pair[1] || '');
            });
        } catch (e) { /* noop */ }
    }

    /* ---------- 危险操作确认 ---------- */
    function initConfirm() {
        document.addEventListener('submit', function (e) {
            var form = e.target;
            var msg = form.getAttribute('data-confirm');
            if (msg && !window.confirm(msg)) e.preventDefault();
        });
    }

    /* ---------- 弹窗（主题 .modal-overlay / .modal-box） ---------- */
    function openModal(id) {
        var el = document.getElementById(id);
        if (!el) return;
        el.classList.add('open');
        document.body.style.overflow = 'hidden';
        var input = el.querySelector('input, textarea, select');
        if (input) setTimeout(function () { input.focus(); }, 120);
    }
    function closeModal(el) {
        el = typeof el === 'string' ? document.getElementById(el) : el;
        if (!el) return;
        el.classList.remove('open');
        document.body.style.overflow = '';
    }
    window.openModal = openModal;
    window.closeModal = closeModal;

    function initModals() {
        document.addEventListener('click', function (e) {
            var opener = e.target.closest('[data-open-modal]');
            if (opener) { e.preventDefault(); openModal(opener.getAttribute('data-open-modal')); return; }
            var closer = e.target.closest('[data-close-modal]');
            if (closer) { e.preventDefault(); closeModal(closer.closest('.modal-overlay')); return; }
            if (e.target.classList && e.target.classList.contains('modal-overlay')) closeModal(e.target);
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                document.querySelectorAll('.modal-overlay.open').forEach(closeModal);
            }
        });
    }

    /* ---------- 工具 ---------- */
    function $(sel, ctx) { return (ctx || document).querySelector(sel); }
    function $all(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }

    function apiGet(url) {
        return fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
            .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
    }

    var STATUS_CLASS = {
        running: 'badge badge-running', pending: 'badge badge-pending',
        scheduled: 'badge badge-scheduled', paused: 'badge badge-paused',
        completed: 'badge badge-completed', failed: 'badge badge-failed',
        stopped: 'badge badge-stopped'
    };
    var STATUS_TEXT = {
        running: '运行中', pending: '待运行', scheduled: '已定时', paused: '已暂停',
        completed: '已完成', failed: '已失败', stopped: '已停止'
    };

    function setBadge(el, status, control) {
        if (!el) return;
        el.className = STATUS_CLASS[status] || 'badge';
        var dot = '<span class="dot"></span>';
        var label = STATUS_TEXT[status] || status;
        if (status === 'running' && control === 'pause') label = '暂停中…';
        if (status === 'running' && control === 'stop') label = '停止中…';
        el.innerHTML = dot + label;
    }

    /* ---------- 列表页轮询 ---------- */
    function initListPolling() {
        var panel = document.querySelector('[data-poll="list"]');
        if (!panel) return;

        function tick() {
            apiGet('/api/tasks/summary').then(function (data) {
                (data.tasks || []).forEach(function (t) {
                    var row = panel.querySelector('tr[data-task-id="' + t.id + '"]');
                    if (!row) return;
                    setBadge(row.querySelector('[data-role="badge"]'), t.status, t.control);
                    var fill = row.querySelector('[data-role="fill"]');
                    var pct = t.percent || 0;
                    if (fill) fill.style.width = pct + '%';
                    var pm = row.querySelector('[data-role="progress-meta"]');
                    if (pm) {
                        pm.innerHTML = '<b>' + (t.sent_count || 0) + '</b> / ' + (t.total_recipients || 0) +
                            (t.failed_count ? ' · <span class="text-danger">失败 ' + t.failed_count + '</span>' : '');
                    }
                    var pctTxt = row.querySelector('[data-role="percent"]');
                    if (pctTxt) pctTxt.textContent = pct + '%';
                    var nr = row.querySelector('[data-role="next-run"]');
                    if (nr) nr.textContent = t.next_run_at ? t.next_run_at : (t.status === 'running' ? '进行中' : '—');
                });
                var st = data.stats || {};
                $all('[data-stat]').forEach(function (el) {
                    var key = el.getAttribute('data-stat');
                    if (st[key] != null) el.textContent = st[key];
                });
                syncScheduler(data.scheduler);
            }).catch(function () { /* 网络抖动忽略 */ });
        }
        setInterval(tick, 3000);
    }

    function syncScheduler(alive) {
        $all('[data-scheduler]').forEach(function (el) {
            el.classList.toggle('text-success', !!alive);
            el.classList.toggle('text-danger', !alive);
            var dot = el.querySelector('.dot');
            if (dot) dot.style.background = alive ? '#22c55e' : '#f43f5e';
        });
    }

    /* ---------- 详情页轮询 ---------- */
    function initDetailPolling() {
        var root = document.querySelector('[data-poll="detail"]');
        if (!root) return;
        var taskId = root.getAttribute('data-task-id');
        var lastStatus = root.getAttribute('data-status');
        var logLatest = parseInt(root.getAttribute('data-log-latest') || '0', 10);
        var logConsole = $('#log-console');
        var rcptBody = $('#rcpt-body');
        var rcptFilter = root.getAttribute('data-rcpt-filter') || '';
        var csrfToken = root.getAttribute('data-csrf') || '';
        var ticks = 0;

        function scrollLogs(force) {
            if (!logConsole) return;
            var nearBottom = logConsole.scrollHeight - logConsole.scrollTop - logConsole.clientHeight < 60;
            if (force || nearBottom) logConsole.scrollTop = logConsole.scrollHeight;
        }

        function applyStatus(s) {
            setBadge($('[data-role="badge"]', root), s.status, s.control);
            var fill = $('[data-role="fill"]', root);
            if (fill) fill.style.width = (s.percent || 0) + '%';
            var pm = $('[data-role="progress-meta-main"]', root);
            if (pm) pm.innerHTML = '<b>' + (s.percent || 0) + '%</b> 已完成';
            var cnts = $('[data-role="counts"]', root);
            if (cnts) {
                var map = { pending: '.c-pending .cb-n', sent: '.c-sent .cb-n', failed: '.c-failed .cb-n', skipped: '.c-skipped .cb-n' };
                Object.keys(map).forEach(function (k) {
                    var el = cnts.querySelector(map[k]);
                    if (el && s.recipient_counts && s.recipient_counts[k] != null) el.textContent = s.recipient_counts[k];
                });
                var tot = cnts.querySelector('.c-total .cb-n');
                if (tot && s.recipient_counts) tot.textContent = s.recipient_counts.total;
            }
            var nr = $('[data-role="next-run"]', root);
            if (nr) nr.textContent = s.next_run_at || '—';
            var lr = $('[data-role="last-run"]', root);
            if (lr) lr.textContent = s.last_run_at || '—';
            var hb = $('[data-role="heartbeat"]', root);
            if (hb) hb.textContent = s.heartbeat_at || '—';
            syncScheduler(s.scheduler);

            if (s.status !== lastStatus) {
                lastStatus = s.status;
                renderActions(s.status);
            }
        }

        /* 运行/暂停/停止按钮就地更新，避免状态变化时整页刷新 */
        function renderActions(status) {
            var box = $('[data-role="actions"]', root);
            if (!box) return;
            function form(action, cls, icon, label, confirmMsg) {
                return '<form method="post" action="/tasks/' + taskId + '/' + action + '"' +
                    (confirmMsg ? ' data-confirm="' + confirmMsg + '"' : '') + '>' +
                    '<input type="hidden" name="csrf_token" value="' + csrfToken + '">' +
                    '<button type="submit" class="' + cls + '"><i data-lucide="' + icon + '"></i>' + label + '</button>' +
                    '</form>';
            }
            if (status === 'running') {
                box.innerHTML =
                    form('pause', 'btn-secondary btn-sm', 'pause', '暂停', '') +
                    form('stop', 'btn-danger btn-sm', 'square', '停止', '确认停止该任务？未发送的收件人将保持待发送状态。');
            } else {
                box.innerHTML = form('start', 'btn-primary btn-sm', 'play', '运行', '');
            }
            renderIcons(box);
        }

        function applyLogs(payload) {
            if (!logConsole || !payload.logs || !payload.logs.length) return;
            var empty = logConsole.querySelector('.log-empty');
            if (empty) empty.remove();
            payload.logs.forEach(function (lg) {
                var div = document.createElement('div');
                div.className = 'log-line lv-' + (lg.level || 'info');
                var t = document.createElement('span');
                t.className = 'lt';
                t.textContent = (lg.created_at || '').slice(11, 19);
                var v = document.createElement('span');
                v.className = 'lv';
                v.textContent = ({ info: 'INFO', success: 'OK', warn: 'WARN', error: 'ERR' })[lg.level] || 'INFO';
                var m = document.createElement('span');
                m.className = 'lm';
                m.textContent = lg.message || '';
                div.appendChild(t); div.appendChild(v); div.appendChild(m);
                logConsole.appendChild(div);
            });
            logLatest = payload.latest || logLatest;
            // 控制台体积：超过 800 行裁掉旧的
            while (logConsole.children && logConsole.children.length > 800) {
                var first = logConsole.querySelector('.log-line');
                if (!first) break;
                first.remove();
            }
            scrollLogs(false);
        }

        function applyRecipients(payload) {
            if (!rcptBody || !payload.rows) return;
            rcptBody.innerHTML = '';
            if (!payload.rows.length) {
                var tr = document.createElement('tr');
                tr.innerHTML = '<td colspan="6" class="text-center text-muted" style="padding:26px">暂无收件人</td>';
                rcptBody.appendChild(tr);
            }
            payload.rows.forEach(function (r) {
                var tr = document.createElement('tr');
                var vars = r.vars ? Object.keys(r.vars) : [];
                tr.innerHTML =
                    '<td class="mono-td">' + esc(r.email) + '</td>' +
                    '<td>' + esc(r.name || '—') + '</td>' +
                    '<td><span class="rcpt-status s-' + r.status + '"><span class="sdot"></span>' +
                    ({ pending: '待发送', sent: '已发送', failed: '失败', skipped: '跳过' })[r.status] + '</span></td>' +
                    '<td class="text-center">' + (r.attempts || 0) + '</td>' +
                    '<td class="hide-sm mono-td text-muted" title="' + esc(r.last_error || '') + '">' + esc(clip(r.last_error, 60)) + '</td>' +
                    '<td class="hide-sm mono-td">' + esc(r.sent_at || '—') + '</td>';
                rcptBody.appendChild(tr);
            });
        }

        function esc(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }
        function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }

        function tick() {
            ticks++;
            apiGet('/api/tasks/' + taskId + '/status').then(applyStatus).catch(function () {});
            apiGet('/api/tasks/' + taskId + '/logs?after=' + logLatest + '&limit=200')
                .then(applyLogs).catch(function () {});
            if (ticks % 2 === 0 && rcptBody) {
                apiGet('/api/tasks/' + taskId + '/recipients?limit=200' + (rcptFilter ? '&status=' + rcptFilter : ''))
                    .then(applyRecipients).catch(function () {});
            }
        }
        scrollLogs(true);
        setInterval(tick, 2500);
    }

    /* ---------- 表单辅助 ---------- */
    function initFormHelpers() {
        // 运行模式面板切换
        var modeSel = $('#run_mode');
        if (modeSel) {
            function syncMode() {
                var mode = modeSel.value;
                $all('[data-mode-panel]').forEach(function (p) {
                    // 支持空格分隔的多模式："once cron interval"
                    var modes = (p.getAttribute('data-mode-panel') || '').split(/\s+/);
                    p.style.display = modes.indexOf(mode) >= 0 ? '' : 'none';
                });
            }
            modeSel.addEventListener('change', syncMode);
            syncMode();
        }

        // 收件人行数统计
        var ta = $('#recipients_text');
        var est = $('#rcpt-est');
        if (ta && est) {
            function count() {
                var lines = ta.value.split('\n').filter(function (l) { return l.trim() && l.trim()[0] !== '#'; });
                est.textContent = lines.length;
            }
            ta.addEventListener('input', count);
            count();
        }

        // 表单内任何文件选择都自动提交（服务器测试页快捷用法）——不做，保持显式提交
    }

    /* ---------- 富文本编辑器（本地实现，无第三方依赖） ---------- */
    function initRichEditor() {
        $all('[data-rte]').forEach(function (wrap) {
            var body = $('[data-rte-body]', wrap);
            var ta = $('textarea', wrap);
            if (!body || !ta) return;

            var sourceBtn = $('[data-rte-source]', wrap);
            var sourceMode = false;
            body.innerHTML = ta.value;

            function sync() { ta.value = body.innerHTML; }

            // 保持 contenteditable 的选区：按下工具栏按钮时不抢焦点
            var toolbar = $('.rte-toolbar', wrap);
            if (toolbar) {
                toolbar.addEventListener('mousedown', function (e) {
                    if (e.target.closest('button')) e.preventDefault();
                });
                toolbar.addEventListener('click', function (e) {
                    var btn = e.target.closest('button');
                    if (!btn) return;
                    if (btn.hasAttribute('data-rte-source')) return;
                    e.preventDefault();
                    if (sourceMode) return;

                    body.focus();
                    var cmd = btn.getAttribute('data-cmd');
                    var block = btn.getAttribute('data-block');
                    var variable = btn.getAttribute('data-var');
                    var link = btn.getAttribute('data-link');

                    if (cmd) {
                        document.execCommand(cmd, false, null);
                    } else if (block) {
                        document.execCommand('formatBlock', false, block);
                    } else if (variable) {
                        document.execCommand('insertText', false, variable);
                    } else if (link) {
                        var href = window.prompt('请输入链接地址（含 https://）', 'https://');
                        if (href) document.execCommand('createLink', false, href);
                    }
                    sync();
                    refreshActive();
                });
            }

            function refreshActive() {
                if (!toolbar) return;
                $all('[data-cmd]', toolbar).forEach(function (btn) {
                    var cmd = btn.getAttribute('data-cmd');
                    var on = false;
                    try { on = document.queryCommandState(cmd); } catch (err) { on = false; }
                    btn.classList.toggle('active', !!on);
                });
            }
            body.addEventListener('input', sync);
            body.addEventListener('blur', sync);
            body.addEventListener('keyup', refreshActive);
            body.addEventListener('mouseup', refreshActive);

            // 源码模式切换
            if (sourceBtn) {
                sourceBtn.addEventListener('click', function (e) {
                    e.preventDefault();
                    sourceMode = !sourceMode;
                    if (sourceMode) {
                        sync();
                        ta.hidden = false;
                        body.style.display = 'none';
                    } else {
                        body.innerHTML = ta.value;
                        ta.hidden = true;
                        body.style.display = '';
                    }
                    sourceBtn.classList.toggle('active', sourceMode);
                    sourceBtn.innerHTML = sourceMode
                        ? '<i data-lucide="eye"></i><span>预览</span>'
                        : '<i data-lucide="code-2"></i><span>源码</span>';
                    renderIcons(sourceBtn);
                });
            }

            // 提交前兜底同步
            var form = wrap.closest('form');
            if (form) {
                form.addEventListener('submit', function () {
                    if (!sourceMode) sync();
                });
            }
        });
    }

    /* ---------- 收件人状态过滤（详情页跳转参数） ---------- */
    function initRcptFilter() {
        $all('[data-rcpt-filter]').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var status = chip.getAttribute('data-rcpt-filter');
                var url = new URL(window.location.href);
                if (status) url.searchParams.set('rcpt', status);
                else url.searchParams.delete('rcpt');
                window.location.href = url.toString();
            });
        });
    }

    /* ---------- 启动 ---------- */
    document.addEventListener('DOMContentLoaded', function () {
        renderIcons(document);
        initNav();
        initFlashes();
        initConfirm();
        initModals();
        initFormHelpers();
        initRichEditor();
        initRcptFilter();
        initListPolling();
        initDetailPolling();
    });
})();
