/* ============================================================
   英语课前自动视频播放系统 · 教师端脚本
   原生 JS，无任何外部依赖
   ============================================================ */
(function () {
  'use strict';

  /* ============================ 工具函数 ============================ */
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmtDate(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function fmtTime(ts) {
    if (!ts) return '-';
    var d = new Date(ts * 1000);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function fmtClock(ts) {
    if (!ts) return '-';
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function sinceText(ts) {
    if (!ts) return '从未上线';
    var s = Math.floor(Date.now() / 1000) - ts;
    if (s < 60) return s + ' 秒前';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }
  function fmtSize(b) {
    b = Number(b || 0);
    if (!b) return '-';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }
  var WD = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];

  function toast(msg, kind) {
    var box = document.getElementById('toasts');
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.innerHTML = esc(msg);
    box.appendChild(el);
    setTimeout(function () { el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 300); }, 3600);
  }

  /* ============================ 接口层 ============================ */
  var S = {
    token: localStorage.getItem('evs_token') || '',
    user: null,
    classes: [],
    classId: '',
    videos: [],
    plans: [],
    weekOffset: 0,
    overview: null,
    ws: null,
    wsTimer: null,
    wsAlive: false,
    pollTimer: null
  };

  function api(method, path, body) {
    var headers = {};
    if (S.token) headers['X-Auth-Token'] = S.token;
    var payload;
    if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    return fetch(path, { method: method, headers: headers, body: payload })
      .then(function (res) {
        var ct = res.headers.get('Content-Type') || '';
        return (ct.indexOf('json') >= 0 ? res.json() : res.text()).then(function (data) {
          if (res.status === 401) { logout(true); throw new Error('登录已过期，请重新登录'); }
          if (!res.ok) throw new Error((data && data.error) || ('请求失败 HTTP ' + res.status));
          return data;
        });
      });
  }
  function q(id) { return document.getElementById(id); }
  function qs(sel, root) { return (root || document).querySelector(sel); }

  /* ============================ 模态框 ============================ */
  function openModal(opt) {
    var mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML =
      '<div class="modal ' + (opt.wide ? 'wide' : '') + '">' +
        '<div class="head"><span>' + esc(opt.title) + '</span><span class="x">×</span></div>' +
        '<div class="body">' + (opt.body || '') + '</div>' +
        (opt.footer === null ? '' : '<div class="foot">' + (opt.footer || '<button class="btn ghost" data-close>关闭</button>') + '</div>') +
      '</div>';
    document.getElementById('modals').appendChild(mask);

    function close() { mask.remove(); }
    mask.querySelector('.x').onclick = close;
    mask.querySelectorAll('[data-close]').forEach(function (b) { b.onclick = close; });
    mask.addEventListener('mousedown', function (e) { if (e.target === mask) close(); });
    return { mask: mask, close: close, root: mask };
  }
  function confirmModal(title, text, onOk, okText) {
    var m = openModal({
      title: title,
      body: '<p>' + esc(text) + '</p>',
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn danger" id="__ok">' + esc(okText || '确定') + '</button>'
    });
    m.root.querySelector('#__ok').onclick = function () { m.close(); onOk(); };
    return m;
  }

  /* ============================ 登录 ============================ */
  function logout(silent) {
    S.token = '';
    localStorage.removeItem('evs_token');
    if (S.ws) { try { S.ws.close(); } catch (e) {} S.ws = null; }
    if (!silent) toast('已退出登录');
    document.getElementById('app').style.display = 'none';
    document.getElementById('login').style.display = 'flex';
  }

  function doLogin() {
    var u = q('login-user').value.trim();
    var p = q('login-pass').value;
    q('login-err').textContent = '';
    if (!u || !p) { q('login-err').textContent = '请输入用户名和密码'; return; }
    q('login-btn').disabled = true;
    api('POST', '/api/login', { username: u, password: p })
      .then(function (res) {
        S.token = res.token;
        S.user = res.user;
        localStorage.setItem('evs_token', res.token);
        boot();
      })
      .catch(function (e) { q('login-err').textContent = e.message; })
      .finally(function () { q('login-btn').disabled = false; });
  }

  /* ============================ 启动 ============================ */
  function boot() {
    document.getElementById('login').style.display = 'none';
    document.getElementById('app').style.display = 'block';
    q('who-name').textContent = S.user.username;
    q('tab-admin').style.display = (S.user.role === 'admin') ? '' : 'none';
    loadClasses().then(function () {
      loadVideos();
      renderWeek();
      connectWs();
      startFallbackPoll();
    });
  }

  /* ============================ 班级 ============================ */
  function loadClasses() {
    return api('GET', '/api/classes').then(function (res) {
      S.classes = res.items || [];
      var picker = q('class-picker');
      picker.innerHTML = '';
      if (S.user.role === 'admin') {
        picker.innerHTML = '<option value="">全部班级</option>' +
          S.classes.map(function (c) { return '<option value="' + c.id + '">' + esc(c.name) + '</option>'; }).join('');
        if (S.classId === '' && S.classes.length) S.classId = String(S.classes[0].id);
        picker.value = S.classId;
        picker.disabled = false;
      } else {
        var my = S.classes[0];
        S.classId = my ? String(my.id) : '';
        picker.innerHTML = my ? '<option value="' + my.id + '">' + esc(my.name) + '</option>' : '<option value="">未分配班级</option>';
        picker.disabled = true;
      }
      var mine = S.classes.filter(function (c) { return String(c.id) === S.classId; })[0];
      q('who-class').textContent = mine ? mine.name : (S.user.role === 'admin' ? '管理员' : '未分配班级');
    });
  }

  /* ============================ 周历 ============================ */
  function weekStart(offset) {
    var now = new Date();
    var dow = now.getDay() === 0 ? 7 : now.getDay();
    var d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (dow - 1) + (offset || 0) * 7);
    return d;
  }

  function buildRows() {
    var set = {};
    for (var h = 7; h <= 19; h++) {
      set[pad(h) + ':00'] = 1;
      if (h < 19) set[pad(h) + ':30'] = 1;
    }
    S.plans.forEach(function (p) {
      set[p.start_time] = 1;
      var mm = parseInt((p.start_time || '00:00').split(':')[1], 10);
      var hh = parseInt((p.start_time || '00:00').split(':')[0], 10);
      var slot = pad(mm < 30 ? hh : hh) + ':' + (mm < 30 ? '00' : '30');
      set[slot] = 1;
    });
    return Object.keys(set).sort();
  }

  function rowKeyOf(time) {
    var parts = (time || '00:00').split(':');
    var h = parseInt(parts[0], 10), m = parseInt(parts[1], 10);
    return pad(h) + ':' + (m < 30 ? '00' : '30');
  }

  function renderWeek() {
    var start = weekStart(S.weekOffset);
    var end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 4);
    q('week-label').textContent = (S.weekOffset === 0 ? '本周 ' : '') +
      (start.getMonth() + 1) + '月' + start.getDate() + '日 - ' + (end.getMonth() + 1) + '月' + end.getDate() + '日';

    var classId = S.classId;
    if (!classId) { renderCalendar([], buildRows()); q('week-count').textContent = '请选择班级'; return; }

    api('GET', '/api/plans?class_id=' + encodeURIComponent(classId)).then(function (res) {
      S.plans = res.items || [];
      q('week-count').textContent = '本周共 ' + S.plans.length + ' 条播放计划';
      renderCalendar(S.plans, buildRows());
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  function renderCalendar(plans, rows) {
    var now = new Date();
    var curSlot = pad(now.getHours()) + ':' + (now.getMinutes() < 30 ? '00' : '30');
    var html = '<thead><tr><th>时间</th>';
    for (var w = 1; w <= 5; w++) html += '<th>' + WD[w] + '</th>';
    html += '</tr></thead><tbody>';

    rows.forEach(function (row) {
      html += '<tr><td>' + row + '</td>';
      for (var w = 1; w <= 5; w++) {
        var items = plans.filter(function (p) { return p.weekday === w && rowKeyOf(p.start_time) === row; });
        var cls = 'slot' + (row === curSlot && S.weekOffset === 0 ? ' now' : '');
        html += '<td class="' + cls + '" data-wd="' + w + '" data-time="' + row + '">';
        items.forEach(function (p) {
          var tip = p.video_title + '（' + p.start_time + '）';
          if (p.reminder) tip += '\n课前提醒：' + p.reminder;
          html += '<div class="chip' + (p.enabled ? '' : ' off') + '" data-plan="' + p.id + '" title="' +
            esc(tip).replace(/\n/g, '&#10;') + '">' +
            '<span class="t">' + esc(p.start_time) + '</span>' + esc(p.video_title) +
            (p.reminder ? '<span class="chip-bell" title="已设课前提醒">铃</span>' : '') + '</div>';
        });
        html += '</td>';
      }
      html += '</tr>';
    });
    html += '</tbody>';
    q('calendar').innerHTML = html;

    q('calendar').querySelectorAll('td.slot').forEach(function (td) {
      td.onclick = function (e) {
        var chip = e.target.closest('.chip');
        if (!chip) {
          // 视频块没有铺满整格（td 高 40px），点在块下方的空白处也应视为点中该计划，
          // 否则会出现「明明看到有课，点下去却在新建」的误操作。
          var chips = td.querySelectorAll('.chip');
          if (chips.length === 1) chip = chips[0];
        }
        if (chip) {
          var pid = parseInt(chip.getAttribute('data-plan'), 10);
          var plan = S.plans.filter(function (p) { return p.id === pid; })[0];
          openPlanDialog(plan, parseInt(td.getAttribute('data-wd'), 10), td.getAttribute('data-time'));
          return;
        }
        openPlanDialog(null, parseInt(td.getAttribute('data-wd'), 10), td.getAttribute('data-time'));
      };
    });
  }

  function openPlanDialog(plan, weekday, rowTime) {
    if (!S.videos.length) {
      toast('请先在「视频库」中添加视频', 'warn');
      return;
    }
    var isEdit = !!plan;
    var opts = S.videos.map(function (v) {
      var sel = (isEdit && plan.video_id === v.id) ? ' selected' : '';
      return '<option value="' + v.id + '"' + sel + '>' + esc(v.title) + (v.kind === 'url' ? '（在线）' : '（本地）') + '</option>';
    }).join('');

    var body =
      '<label class="field"><span>星期</span><select class="select" id="f-wd">' +
        [1, 2, 3, 4, 5].map(function (w) {
          return '<option value="' + w + '"' + ((isEdit ? plan.weekday : weekday) === w ? ' selected' : '') + '>' + WD[w] + '</option>';
        }).join('') + '</select></label>' +
      '<label class="field"><span>播放时间（精确到分钟，到点自动全屏播放）</span>' +
        '<input class="input" type="time" id="f-time" value="' + esc(isEdit ? plan.start_time : rowTime) + '"></label>' +
      '<label class="field"><span>英语视频</span><select class="select" id="f-video">' + opts + '</select></label>' +
      '<label class="field"><span>播放时长上限（秒，留空或 0 = 播放到视频结束）</span>' +
        '<input class="input" type="number" min="0" id="f-dur" value="' + esc(isEdit ? (plan.duration || '') : '') + '"></label>' +
      '<label class="field"><span>课前提醒（播放前全屏展示并人声朗读；留空则使用默认提醒语，最长 200 字）</span>' +
        '<textarea class="textarea" id="f-reminder" rows="2" placeholder="例如：Please take out your textbook and notebook.">' +
        esc(isEdit ? (plan.reminder || '') : '') + '</textarea></label>' +
      '<label class="field"><span>备注（可选）</span><input class="input" id="f-note" value="' + esc(isEdit ? (plan.note || '') : '') + '"></label>' +
      (isEdit ? '<label class="row"><input type="checkbox" id="f-enabled"' + (plan.enabled ? ' checked' : '') + '> <span>启用该计划</span></label>' : '');

    var footer = isEdit
      ? '<button class="btn danger" id="d-del">删除</button><div class="grow"></div><button class="btn ghost" data-close>取消</button><button class="btn" id="d-save">保存</button>'
      : '<button class="btn ghost" data-close>取消</button><button class="btn" id="d-save">添加</button>';

    var m = openModal({ title: isEdit ? '编辑播放计划' : '新增播放计划', body: body, footer: footer });

    m.root.querySelector('#d-save').onclick = function () {
      var payload = {
        class_id: parseInt(S.classId, 10),
        weekday: parseInt(m.root.querySelector('#f-wd').value, 10),
        start_time: m.root.querySelector('#f-time').value,
        video_id: parseInt(m.root.querySelector('#f-video').value, 10),
        duration: parseInt(m.root.querySelector('#f-dur').value || '0', 10),
        note: m.root.querySelector('#f-note').value.trim(),
        reminder: m.root.querySelector('#f-reminder').value.trim()
      };
      if (!/^\d{2}:\d{2}$/.test(payload.start_time)) { toast('时间格式必须为 HH:MM', 'err'); return; }
      if (isEdit) payload.enabled = m.root.querySelector('#f-enabled').checked;
      var req = isEdit ? api('PUT', '/api/plans/' + plan.id, payload) : api('POST', '/api/plans', payload);
      req.then(function () { m.close(); toast('已保存', 'ok'); renderWeek(); })
        .catch(function (e) { toast(e.message, 'err'); });
    };
    if (isEdit) {
      m.root.querySelector('#d-del').onclick = function () {
        m.close();
        confirmModal('删除计划', '确定删除「' + WD[plan.weekday] + ' ' + plan.start_time + ' ' + plan.video_title + '」吗？', function () {
          api('DELETE', '/api/plans/' + plan.id).then(function () { toast('已删除', 'ok'); renderWeek(); })
            .catch(function (e) { toast(e.message, 'err'); });
        }, '删除');
      };
    }
  }

  /* ------------------------ 批量导入 ------------------------ */
  function parseWeekday(s) {
    s = String(s || '').trim();
    if (/^[1-5]$/.test(s)) return parseInt(s, 10);
    var map = { '周一': 1, '星期一': 1, '周二': 2, '星期二': 2, '周三': 3, '星期三': 3, '周四': 4, '星期四': 4, '周五': 5, '星期五': 5, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5 };
    return map[s] || 0;
  }

  function openBatchDialog() {
    var body =
      '<p class="hint">每行一条，格式：<b>星期,时间,视频标题</b>；星期可写「周一」或「1」，视频标题需与视频库中的标题完全一致。</p>' +
      '<pre class="log" style="max-height:none">示例：\n周一,08:05,Unit 1 Warming up\n周二,09:10,Unit 2 Listening\n周三,14:00,英文儿歌 - 天气</pre>' +
      '<p class="hint">也支持直接粘贴 JSON 数组：<code>[{"weekday":1,"start_time":"08:05","video_title":"Unit 1"}]</code></p>' +
      '<textarea class="textarea" id="b-text" placeholder="周一,08:05,Unit 1 Warming up"></textarea>' +
      '<div id="b-result" class="mt"></div>';

    var m = openModal({
      title: '批量导入播放计划', wide: true, body: body,
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="b-go">开始导入</button>'
    });

    m.root.querySelector('#b-go').onclick = function () {
      var text = m.root.querySelector('#b-text').value.trim();
      if (!text) { toast('请先粘贴内容', 'warn'); return; }
      var items = [];
      var lines = text.split(/\r?\n/).filter(function (l) { return l.trim(); });
      if (text[0] === '[') {
        try {
          var arr = JSON.parse(text);
          arr.forEach(function (o) {
            items.push({
              weekday: o.weekday,
              start_time: o.start_time,
              video_id: o.video_id,
              video_title: o.video_title,
              duration: o.duration || 0,
              note: o.note || '',
              reminder: o.reminder || ''
            });
          });
        } catch (e) { toast('JSON 解析失败：' + e.message, 'err'); return; }
      } else {
        lines.forEach(function (line, i) {
          var parts = line.split(/[,，\t]+/).map(function (s) { return s.trim(); });
          if (parts.length < 3) return;
          items.push({ weekday: parseWeekday(parts[0]), start_time: parts[1], video_title: parts[2], duration: 0, note: '' });
        });
      }
      if (!items.length) { toast('未解析出任何有效行', 'err'); return; }
      items.forEach(function (it) { it.class_id = parseInt(S.classId, 10); });

      m.root.querySelector('#b-go').disabled = true;
      api('POST', '/api/plans/batch', { items: items }).then(function (res) {
        var msg = '成功导入 ' + res.created + ' 条';
        if (res.errors && res.errors.length) {
          msg += '，失败 ' + res.errors.length + ' 条';
          m.root.querySelector('#b-result').innerHTML = '<div class="badge red">失败明细</div><pre class="log">' +
            esc(res.errors.map(function (e) { return '第 ' + (e.index + 1) + ' 行：' + e.message; }).join('\n')) + '</pre>';
        }
        toast(msg, res.errors && res.errors.length ? 'warn' : 'ok');
        renderWeek();
      }).catch(function (e) { toast(e.message, 'err'); })
        .finally(function () { m.root.querySelector('#b-go').disabled = false; });
    };
  }

  function downloadTemplate() {
    var csv = '星期,时间,视频标题\n周一,08:05,Unit 1 Warming up\n周二,09:10,Unit 2 Listening\n周三,14:00,英文儿歌-天气\n周四,15:30,Unit 3 Reading\n周五,08:05,周末复习\n';
    var blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '播放计划导入模板.csv';
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }

  function clearPlansDialog() {
    if (!S.classId) { toast('请先选择班级', 'warn'); return; }
    var body = '<p>将删除该班级的播放计划，该操作不可恢复。</p>' +
      '<label class="field"><span>范围</span><select class="select" id="c-wd"><option value="">全部（周一到周五）</option>' +
      [1, 2, 3, 4, 5].map(function (w) { return '<option value="' + w + '">' + WD[w] + '</option>'; }).join('') +
      '</select></label>';
    var m = openModal({
      title: '清空播放计划', body: body,
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn danger" id="c-go">确认清空</button>'
    });
    m.root.querySelector('#c-go').onclick = function () {
      var wd = m.root.querySelector('#c-wd').value;
      var payload = { class_id: parseInt(S.classId, 10) };
      if (wd) payload.weekday = parseInt(wd, 10);
      api('POST', '/api/plans/clear', payload).then(function () {
        m.close(); toast('已清空', 'ok'); renderWeek();
      }).catch(function (e) { toast(e.message, 'err'); });
    };
  }

  /* ============================ 视频库 ============================ */
  function loadVideos() {
    var path = '/api/videos' + (S.classId ? '?class_id=' + encodeURIComponent(S.classId) : '');
    return api('GET', path).then(function (res) {
      S.videos = res.items || [];
      renderVideos();
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  function renderVideos() {
    q('video-count').textContent = '共 ' + S.videos.length + ' 个视频';
    var box = q('video-list');
    if (!S.videos.length) {
      box.innerHTML = '<div class="empty">视频库为空，点击左上角「上传本地视频」或「添加在线链接」开始添加。</div>';
      return;
    }
    box.innerHTML = S.videos.map(function (v) {
      return '<div class="video-card">' +
        '<div class="thumb">' +
          '<svg width="38" height="38" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>' +
          '<span class="badge ' + (v.kind === 'url' ? 'amber' : '') + ' kind">' + (v.kind === 'url' ? '在线' : '本地') + '</span>' +
        '</div>' +
        '<div class="body">' +
          '<div class="title" title="' + esc(v.title) + '">' + esc(v.title) + '</div>' +
          '<div class="meta">' + esc(v.class_name || '公共视频') + ' · ' + fmtSize(v.file_size) + '</div>' +
          '<div class="meta muted">添加于 ' + fmtTime(v.created_at) + '</div>' +
          '<div class="ops">' +
            '<button class="btn small" data-play="' + v.id + '">预览</button>' +
            '<button class="btn ghost small" data-rename="' + v.id + '">改名</button>' +
            '<button class="btn danger small" data-del="' + v.id + '">删除</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    }).join('');

    box.querySelectorAll('[data-play]').forEach(function (b) {
      b.onclick = function () {
        var v = S.videos.filter(function (x) { return x.id === parseInt(b.getAttribute('data-play'), 10); })[0];
        var src = v.kind === 'url' ? v.url : ('/media/' + v.id + '?t=' + encodeURIComponent(S.token));
        var m = openModal({
          title: '预览：' + v.title, wide: true, footer: null,
          body: '<video src="' + esc(src) + '" controls autoplay style="width:100%;max-height:60vh;background:#000;border-radius:8px"></video>' +
                '<p class="hint mt">本地视频由本服务器直接分发；在线视频直接播放原始链接，离线缓存的教室设备会在联网时自动下载。</p>'
        });
      };
    });
    box.querySelectorAll('[data-rename]').forEach(function (b) {
      b.onclick = function () {
        var id = parseInt(b.getAttribute('data-rename'), 10);
        var v = S.videos.filter(function (x) { return x.id === id; })[0];
        var m = openModal({
          title: '重命名视频',
          body: '<label class="field"><span>标题</span><input class="input" id="r-title" value="' + esc(v.title) + '"></label>',
          footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="r-go">保存</button>'
        });
        m.root.querySelector('#r-go').onclick = function () {
          api('PUT', '/api/videos/' + id, { title: m.root.querySelector('#r-title').value.trim() })
            .then(function () { m.close(); toast('已保存', 'ok'); loadVideos(); })
            .catch(function (e) { toast(e.message, 'err'); });
        };
      };
    });
    box.querySelectorAll('[data-del]').forEach(function (b) {
      b.onclick = function () {
        var id = parseInt(b.getAttribute('data-del'), 10);
        var v = S.videos.filter(function (x) { return x.id === id; })[0];
        confirmModal('删除视频', '确定删除「' + v.title + '」吗？已被播放计划引用的视频无法删除。', function () {
          api('DELETE', '/api/videos/' + id).then(function () { toast('已删除', 'ok'); loadVideos(); })
            .catch(function (e) { toast(e.message, 'err'); });
        }, '删除');
      };
    });
  }

  function pickFileAndUpload() {
    if (!S.classId) { toast('请先在右上角选择班级', 'warn'); return; }
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = 'video/*,audio/*,.mp4,.mkv,.mov,.avi,.wmv,.webm';
    input.multiple = true;
    input.onchange = function () {
      var files = Array.prototype.slice.call(input.files || []);
      if (!files.length) return;
      uploadNext(files, 0);
    };
    input.click();
  }

  function uploadNext(files, index) {
    if (index >= files.length) { toast('全部上传完成', 'ok'); loadVideos(); return; }
    var file = files[index];
    var title = file.name.replace(/\.[^.]+$/, '');
    var url = '/api/videos/upload?title=' + encodeURIComponent(title) +
              '&class_id=' + encodeURIComponent(S.classId) +
              '&filename=' + encodeURIComponent(file.name);

    var box = q('upload-progress');
    box.style.display = '';
    q('up-name').textContent = '正在上传（' + (index + 1) + '/' + files.length + '）：' + file.name;

    var xhr = new XMLHttpRequest();
    xhr.open('PUT', url, true);
    xhr.setRequestHeader('X-Auth-Token', S.token);
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round(e.loaded * 100 / e.total);
      q('up-pct').textContent = pct + '%';
      q('up-bar').style.width = pct + '%';
    };
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        uploadNext(files, index + 1);
      } else {
        var msg = '上传失败';
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
        toast(file.name + '：' + msg, 'err');
        uploadNext(files, index + 1);
      }
    };
    xhr.onerror = function () { toast(file.name + '：网络错误', 'err'); uploadNext(files, index + 1); };
    xhr.onloadend = function () { if (index + 1 >= files.length) box.style.display = 'none'; };
    xhr.send(file);
  }

  function addLinkDialog() {
    var body =
      '<label class="field"><span>视频标题</span><input class="input" id="l-title" placeholder="例如：Unit 1 Warming up"></label>' +
      '<label class="field"><span>视频链接</span><input class="input" id="l-url" placeholder="https://... 或 http://..."></label>' +
      '<p class="hint">建议使用可直接下载的 mp4 直链，教室设备会自动缓存到本地，断网也能播。' +
      '若为网页播放页链接，则只能联网时在线播放。</p>';
    var m = openModal({
      title: '添加在线视频', body: body,
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="l-go">添加</button>'
    });
    m.root.querySelector('#l-go').onclick = function () {
      var payload = {
        title: m.root.querySelector('#l-title').value.trim(),
        url: m.root.querySelector('#l-url').value.trim(),
        class_id: parseInt(S.classId, 10) || null
      };
      api('POST', '/api/videos/link', payload).then(function () {
        m.close(); toast('已添加', 'ok'); loadVideos();
      }).catch(function (e) { toast(e.message, 'err'); });
    };
  }

  /* ============================ 作业布置 ============================ */
  function loadHomework() {
    var path = '/api/homework' + (S.classId ? '?class_id=' + encodeURIComponent(S.classId) : '');
    api('GET', path).then(function (res) {
      renderHomework(res.items || []);
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  function renderHomework(items) {
    var box = q('hw-table');
    if (!S.classId) {
      box.innerHTML = '<tbody><tr><td class="empty">请先在右上角选择要布置作业的班级。</td></tr></tbody>';
      return;
    }
    if (!items.length) {
      box.innerHTML = '<tbody><tr><td class="empty">该班级还没有作业记录，在上方输入框发布第一条作业吧。</td></tr></tbody>';
      return;
    }
    var rows = items.map(function (h) {
      return '<tr>' +
        '<td style="min-width:340px;white-space:pre-wrap;line-height:1.6">' + esc(h.content) + '</td>' +
        '<td>' + esc(h.class_name || '') + '</td>' +
        '<td class="muted">' + esc(h.created_by || '') + '<br><span class="hint">' + fmtTime(h.created_at) + '</span></td>' +
        '<td><button class="btn danger small" data-hwdel="' + h.id + '">删除</button></td>' +
      '</tr>';
    }).join('');
    box.innerHTML =
      '<thead><tr><th>作业内容</th><th>班级</th><th>发布人 / 时间</th><th>操作</th></tr></thead>' +
      '<tbody>' + rows + '</tbody>';

    box.querySelectorAll('[data-hwdel]').forEach(function (b) {
      b.onclick = function () {
        var id = parseInt(b.getAttribute('data-hwdel'), 10);
        confirmModal('删除作业', '删除后不影响已经弹出过提醒的设备，但新上线设备不会再收到该作业。确定删除？', function () {
          api('DELETE', '/api/homework/' + id).then(function () { toast('已删除', 'ok'); loadHomework(); })
            .catch(function (e) { toast(e.message, 'err'); });
        }, '删除');
      };
    });
  }

  function publishHomework() {
    var ta = q('hw-content');
    var content = ta.value.trim();
    if (!S.classId) { toast('请先在右上角选择班级', 'warn'); return; }
    if (!content) { toast('请输入作业内容', 'warn'); return; }
    if (content.length > 500) { toast('作业内容不能超过 500 字', 'warn'); return; }
    var btn = q('btn-hw-publish');
    btn.disabled = true;
    api('POST', '/api/homework', { class_id: parseInt(S.classId, 10), content: content })
      .then(function () {
        ta.value = '';
        toast('作业已发布，学生端将立即弹窗提醒', 'ok');
        loadHomework();
      })
      .catch(function (e) { toast(e.message, 'err'); })
      .finally(function () { btn.disabled = false; });
  }

  /* ============================ 设备监控 ============================ */
  function loadMonitor() {
    var date = q('mon-date').value || fmtDate(new Date());
    var path = '/api/overview?date=' + date + (S.classId ? '&class_id=' + encodeURIComponent(S.classId) : '');
    api('GET', path).then(function (res) {
      S.overview = res;
      renderOverview(res);
    }).catch(function (e) { toast(e.message, 'err'); });
    loadLogs(date);
  }

  function renderOverview(res) {
    q('mon-stats').innerHTML =
      stat('设备总数', res.device_total, '台') +
      stat('在线设备', res.device_online, '台') +
      stat('今日计划', res.plan_total, '节') +
      stat('日期', WD[res.weekday] || '-', res.date, true);

    var rows = (res.devices || []).map(function (d) {
      var pct = d.today_total ? Math.round(d.today_done * 100 / d.today_total) : 0;
      var cache = d.cache_state || {};
      var cached = (cache.cached || []).length;
      var pending = (cache.pending || []).length;
      return '<tr>' +
        '<td><b>' + esc(d.name) + '</b><div class="hint">' + esc(d.class_name || '') + '</div></td>' +
        '<td><span class="dot ' + (d.online ? 'on' : 'off') + '"></span>' + (d.online ? '在线' : '离线') +
          '<div class="hint">' + sinceText(d.last_seen) + '</div></td>' +
        '<td>' + esc(d.client_version || '-') + '</td>' +
        '<td><div class="row" style="gap:8px"><span>' + d.today_done + '/' + d.today_total + '</span>' +
          '<div class="progress grow"><i style="width:' + pct + '%"></i></div></div></td>' +
        '<td><span class="badge green">已缓存 ' + cached + '</span><span class="badge gray">待下载 ' + pending + '</span></td>' +
        '<td>' + (d.current_plan_id ? '<span class="badge amber">播放中 #' + d.current_plan_id + '</span>' : '<span class="muted">待机</span>') + '</td>' +
        '<td class="row" style="gap:6px">' +
          '<button class="btn ghost small" data-rename="' + d.id + '">改名</button>' +
          '<button class="btn ghost small" data-pwd="' + d.id + '">退出密码</button>' +
          '<button class="btn danger small" data-unbind="' + d.id + '">解绑</button>' +
        '</td>' +
      '</tr>';
    }).join('');
    q('mon-devices').innerHTML =
      '<thead><tr><th>设备</th><th>状态</th><th>版本</th><th>今日完成</th><th>离线缓存</th><th>当前</th><th>操作</th></tr></thead>' +
      '<tbody>' + (rows || '<tr><td colspan="7" class="empty">暂无已绑定设备，请用「生成设备绑定码」在教室电脑上完成绑定。</td></tr>') + '</tbody>';

    q('mon-devices').querySelectorAll('[data-rename]').forEach(function (b) {
      b.onclick = function () {
        var d = findDevice(parseInt(b.getAttribute('data-rename'), 10));
        var m = openModal({
          title: '修改设备名称',
          body: '<label class="field"><span>名称</span><input class="input" id="dn" value="' + esc(d.name) + '"></label>',
          footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="ok">保存</button>'
        });
        m.root.querySelector('#ok').onclick = function () {
          api('POST', '/api/devices/' + d.id + '/rename', { name: m.root.querySelector('#dn').value.trim() })
            .then(function () { m.close(); toast('已保存', 'ok'); loadMonitor(); })
            .catch(function (e) { toast(e.message, 'err'); });
        };
      };
    });
    q('mon-devices').querySelectorAll('[data-pwd]').forEach(function (b) {
      b.onclick = function () {
        var d = findDevice(parseInt(b.getAttribute('data-pwd'), 10));
        var m = openModal({
          title: '设置退出/解锁密码',
          body: '<p class="hint">该密码用于在教室设备上退出程序或解锁操作，至少 4 位，修改后立即同步到设备。</p>' +
                '<label class="field"><span>新密码</span><input class="input" id="dp" value="' + esc(d.exit_password || '') + '"></label>',
          footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="ok">下发</button>'
        });
        m.root.querySelector('#ok').onclick = function () {
          api('POST', '/api/devices/' + d.id + '/password', { password: m.root.querySelector('#dp').value.trim() })
            .then(function () { m.close(); toast('密码已下发', 'ok'); loadMonitor(); })
            .catch(function (e) { toast(e.message, 'err'); });
        };
      };
    });
    q('mon-devices').querySelectorAll('[data-unbind]').forEach(function (b) {
      b.onclick = function () {
        var d = findDevice(parseInt(b.getAttribute('data-unbind'), 10));
        confirmModal('解绑设备', '解绑后该设备将无法再获取课程表，需要重新用绑定码绑定。确定继续？', function () {
          api('DELETE', '/api/devices/' + d.id).then(function () { toast('已解绑', 'ok'); loadMonitor(); })
            .catch(function (e) { toast(e.message, 'err'); });
        }, '解绑');
      };
    });
  }

  function findDevice(id) {
    return ((S.overview && S.overview.devices) || []).filter(function (d) { return d.id === id; })[0] || { id: id };
  }

  function stat(k, v, unit, sub) {
    return '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) +
      (sub ? '<small> ' + esc(unit) + '</small>' : (unit ? '<small> ' + esc(unit) + '</small>' : '')) + '</div></div>';
  }

  function loadLogs(date) {
    var path = '/api/logs?date=' + date + (S.classId ? '&class_id=' + encodeURIComponent(S.classId) : '');
    api('GET', path).then(function (res) {
      var items = res.items || [];
      var rows = items.map(function (l) {
        var cls, txt;
        if (l.status === 'success') { cls = 'green'; txt = '播放完成'; }
        else if (l.status === 'failed') { cls = 'red'; txt = '播放失败'; }
        else if (l.status === 'reminded') { cls = 'amber'; txt = '仅提醒未播放'; }
        else { cls = 'amber'; txt = '开始播放'; }
        return '<tr><td>' + fmtClock(l.created_at) + '</td><td>' + esc(l.device_name || '-') + '</td>' +
          '<td>' + esc(l.video_title || '-') + '</td><td>' + esc(l.plan_time || '-') + '</td>' +
          '<td><span class="badge ' + cls + '">' + txt + '</span>' + (l.detail ? ' <span class="hint">' + esc(l.detail) + '</span>' : '') + '</td></tr>';
      }).join('');
      q('mon-logs').innerHTML = '<thead><tr><th>时间</th><th>设备</th><th>视频</th><th>计划时间</th><th>状态</th></tr></thead>' +
        '<tbody>' + (rows || '<tr><td colspan="5" class="empty">当日暂无播放记录</td></tr>') + '</tbody>';
    });
  }

  function bindCodeDialog() {
    if (!S.classId) { toast('请先在右上角选择班级', 'warn'); return; }
    api('POST', '/api/classes/' + S.classId + '/bind-code', {}).then(function (res) {
      var m = openModal({
        title: '设备绑定码',
        body: '<p class="hint">在教室电脑上运行安装脚本时输入下面这个 6 位绑定码，即可把该设备绑定到当前班级。</p>' +
              '<div class="code-big">' + esc(res.code) + '</div>' +
              '<pre class="log">powershell -ExecutionPolicy Bypass -File install.ps1 -Server ' + esc(location.origin) +
              ' -Code ' + esc(res.code) + ' -Name "教室电脑名称"</pre>' +
              '<p class="hint" id="bc-tip"></p>',
        footer: '<button class="btn" data-close>知道了</button>'
      });
      var left = res.ttl || 1800;
      var tip = m.root.querySelector('#bc-tip');
      var timer = setInterval(function () {
        left--;
        if (left <= 0) { tip.textContent = '该绑定码已过期，请重新生成。'; clearInterval(timer); return; }
        tip.textContent = '剩余有效期：' + Math.floor(left / 60) + ' 分 ' + (left % 60) + ' 秒';
      }, 1000);
      m.mask.addEventListener('DOMNodeRemoved', function () { clearInterval(timer); });
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  /* ============================ 账号与班级 ============================ */
  function loadAdmin() {
    if (S.user.role !== 'admin') return;
    api('GET', '/api/users').then(function (res) {
      q('user-table').innerHTML = '<thead><tr><th>用户名</th><th>角色</th><th>管理班级</th><th>操作</th></tr></thead><tbody>' +
        (res.items || []).map(function (u) {
          return '<tr><td>' + esc(u.username) + '</td>' +
            '<td>' + (u.role === 'admin' ? '<span class="badge">管理员</span>' : '<span class="badge gray">教师</span>') + '</td>' +
            '<td>' + esc(u.class_name || '—') + '</td>' +
            '<td class="row" style="gap:6px">' +
              '<button class="btn ghost small" data-uedit="' + u.id + '">编辑</button>' +
              '<button class="btn danger small" data-udel="' + u.id + '">删除</button>' +
            '</td></tr>';
        }).join('') + '</tbody>';

      q('user-table').querySelectorAll('[data-uedit]').forEach(function (b) {
        b.onclick = function () { userDialog(res.items.filter(function (x) { return x.id === parseInt(b.getAttribute('data-uedit'), 10); })[0]); };
      });
      q('user-table').querySelectorAll('[data-udel]').forEach(function (b) {
        b.onclick = function () {
          var u = res.items.filter(function (x) { return x.id === parseInt(b.getAttribute('data-udel'), 10); })[0];
          confirmModal('删除账号', '确定删除账号「' + u.username + '」吗？', function () {
            api('DELETE', '/api/users/' + u.id).then(function () { toast('已删除', 'ok'); loadAdmin(); })
              .catch(function (e) { toast(e.message, 'err'); });
          }, '删除');
        };
      });
    });

    api('GET', '/api/classes').then(function (res) {
      q('class-table').innerHTML = '<thead><tr><th>班级</th><th>绑定设备</th><th>播放计划</th><th>操作</th></tr></thead><tbody>' +
        (res.items || []).map(function (c) {
          return '<tr><td>' + esc(c.name) + '</td><td>' + c.device_count + '</td><td>' + c.plan_count + '</td>' +
            '<td><button class="btn danger small" data-cdel="' + c.id + '">删除</button></td></tr>';
        }).join('') + '</tbody>';
      q('class-table').querySelectorAll('[data-cdel]').forEach(function (b) {
        b.onclick = function () {
          var cid = parseInt(b.getAttribute('data-cdel'), 10);
          confirmModal('删除班级', '删除班级会同时删除该班级的播放计划与设备绑定，且不可恢复。确定继续？', function () {
            api('DELETE', '/api/classes/' + cid).then(function () { toast('已删除', 'ok'); loadClasses().then(loadAdmin); })
              .catch(function (e) { toast(e.message, 'err'); });
          }, '删除');
        };
      });
    });
  }

  function userDialog(user) {
    var isEdit = !!user;
    var body =
      '<label class="field"><span>用户名</span><input class="input" id="u-name" ' + (isEdit ? 'disabled value="' + esc(user.username) + '"' : 'placeholder="登录账号"') + '></label>' +
      '<label class="field"><span>密码' + (isEdit ? '（留空表示不修改）' : '（至少 6 位）') + '</span><input class="input" type="password" id="u-pass"></label>' +
      '<label class="field"><span>角色</span><select class="select" id="u-role">' +
        '<option value="teacher"' + (isEdit && user.role === 'teacher' ? ' selected' : '') + '>教师（只能管理所辖班级）</option>' +
        '<option value="admin"' + (isEdit && user.role === 'admin' ? ' selected' : '') + '>管理员（可管理全部）</option>' +
      '</select></label>' +
      '<label class="field"><span>负责班级</span><select class="select" id="u-class"><option value="">不指定</option>' +
        S.classes.map(function (c) { return '<option value="' + c.id + '"' + (isEdit && user.class_id === c.id ? ' selected' : '') + '>' + esc(c.name) + '</option>'; }).join('') +
      '</select></label>';

    var m = openModal({
      title: isEdit ? '编辑账号' : '新建教师账号', body: body,
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="ok">保存</button>'
    });
    m.root.querySelector('#ok').onclick = function () {
      var pass = m.root.querySelector('#u-pass').value;
      var payload = {
        role: m.root.querySelector('#u-role').value,
        class_id: parseInt(m.root.querySelector('#u-class').value, 10) || null
      };
      if (pass) payload.password = pass;
      var req;
      if (isEdit) {
        if (!pass && payload.class_id === user.class_id) { /* 仍提交以更新角色 */ }
        req = api('PUT', '/api/users/' + user.id, payload);
      } else {
        payload.username = m.root.querySelector('#u-name').value.trim();
        if (!payload.username) { toast('请输入用户名', 'warn'); return; }
        if (pass.length < 6) { toast('密码至少 6 位', 'warn'); return; }
        req = api('POST', '/api/users', payload);
      }
      req.then(function () { m.close(); toast('已保存', 'ok'); loadAdmin(); })
        .catch(function (e) { toast(e.message, 'err'); });
    };
  }

  function classDialog() {
    var m = openModal({
      title: '新建班级',
      body: '<label class="field"><span>班级名称</span><input class="input" id="c-name" placeholder="例如：高一(1)班"></label>',
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="ok">创建</button>'
    });
    m.root.querySelector('#ok').onclick = function () {
      api('POST', '/api/classes', { name: m.root.querySelector('#c-name').value.trim() })
        .then(function () { m.close(); toast('已创建', 'ok'); loadClasses().then(loadAdmin); })
        .catch(function (e) { toast(e.message, 'err'); });
    };
  }

  /* ============================ 实时推送 ============================ */
  // WebSocket 只用于"立即刷新"；部分云平台（如 Render 免费套餐）会断开长连接，
  // 因此额外用低频轮询兜底，保证页面数据始终能自己更新。
  function startFallbackPoll() {
    if (S.pollTimer) return;
    S.pollTimer = setInterval(function () {
      if (S.wsAlive || !S.token) return;
      renderWeek();
      var active = document.querySelector('.view.active');
      if (!active) return;
      if (active.id === 'view-monitor') loadMonitor();
      if (active.id === 'view-videos') loadVideos();
      if (active.id === 'view-homework') loadHomework();
    }, 30000);
  }

  function connectWs() {
    if (!S.token) return;
    if (S.ws) { try { S.ws.close(); } catch (e) {} }
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var ws;
    try { ws = new WebSocket(proto + location.host + '/ws?t=' + encodeURIComponent(S.token)); }
    catch (e) { return; }
    S.ws = ws;
    setLive('实时推送：连接中…', false);

    ws.onopen = function () { S.wsAlive = true; setLive('实时推送：已连接', true); };
    ws.onclose = function () {
      S.wsAlive = false;
      setLive('实时推送：已断开（3 秒后重连）', false);
      clearTimeout(S.wsTimer);
      S.wsTimer = setTimeout(connectWs, 3000);
    };
    ws.onerror = function () { S.wsAlive = false; };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'ping' || msg.type === 'welcome') return;
      if (msg.type === 'plans_changed' || msg.type === 'videos_changed') {
        if (!msg.data || String(msg.data.class_id) === String(S.classId)) { renderWeek(); loadVideos(); }
      } else if (msg.type === 'device_update' || msg.type === 'play_log' || msg.type === 'devices_changed') {
        var active = document.querySelector('.view.active');
        if (active && active.id === 'view-monitor') loadMonitor();
      }
    };
  }

  function setLive(text, ok) {
    var el = q('mon-live');
    if (!el) return;
    el.textContent = text;
    el.style.color = ok ? 'var(--success)' : 'var(--muted)';
  }

  /* ============================ 事件绑定 ============================ */
  function bindEvents() {
    q('login-btn').onclick = doLogin;
    q('login-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
    q('logout-btn').onclick = function () { logout(); };

    q('tabs').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-view]');
      if (!btn) return;
      q('tabs').querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var id = 'view-' + btn.getAttribute('data-view');
      document.querySelectorAll('.view').forEach(function (v) { v.classList.toggle('active', v.id === id); });
      if (btn.getAttribute('data-view') === 'videos') loadVideos();
      if (btn.getAttribute('data-view') === 'homework') loadHomework();
      if (btn.getAttribute('data-view') === 'monitor') loadMonitor();
      if (btn.getAttribute('data-view') === 'admin') loadAdmin();
    });

    q('class-picker').onchange = function () {
      S.classId = this.value;
      var mine = S.classes.filter(function (c) { return String(c.id) === S.classId; })[0];
      if (!(S.user.role === 'admin' && S.classId === '')) q('who-class').textContent = mine ? mine.name : '管理员';
      renderWeek(); loadVideos();
      var active = document.querySelector('.view.active');
      if (active && active.id === 'view-homework') loadHomework();
    };

    q('week-prev').onclick = function () { S.weekOffset--; renderWeek(); };
    q('week-next').onclick = function () { S.weekOffset++; renderWeek(); };
    q('week-today').onclick = function () { S.weekOffset = 0; renderWeek(); };
    q('btn-batch').onclick = openBatchDialog;
    q('btn-tpl').onclick = downloadTemplate;
    q('btn-clear').onclick = clearPlansDialog;

    q('btn-upload').onclick = pickFileAndUpload;
    q('btn-link').onclick = addLinkDialog;
    q('btn-video-refresh').onclick = loadVideos;

    q('btn-hw-publish').onclick = publishHomework;
    q('btn-hw-refresh').onclick = loadHomework;
    q('hw-content').addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') publishHomework();
    });

    q('mon-date').value = fmtDate(new Date());
    q('mon-date').onchange = loadMonitor;
    q('btn-mon-refresh').onclick = loadMonitor;
    q('btn-bindcode').onclick = bindCodeDialog;

    q('btn-add-user').onclick = function () { userDialog(null); };
    q('btn-add-class').onclick = classDialog;
  }

  /* ============================ 初始化 ============================ */
  function init() {
    bindEvents();
    q('mon-date').value = fmtDate(new Date());
    if (!S.token) { document.getElementById('login').style.display = 'flex'; return; }
    api('GET', '/api/me').then(function (res) {
      S.user = res.user;
      boot();
    }).catch(function () {
      logout(true);
      document.getElementById('login').style.display = 'flex';
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
