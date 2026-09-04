/**
 * 工数打刻 → Google スプレッドシート 連携
 *
 * 役割
 *   - 端末から送られた打刻イベントを events シートに追記(id で重複排除)
 *   - events からセッション(作業のかたまり)を組み立てて sessions シートを再生成
 *   - 案件×工程 / 工程 / 社員×工程 の集計シートを再生成し、積み上げ棒グラフを作る
 *
 * 端末側は「まだ送っていないイベント」だけを送る。同じ id が再送されても
 * 上書きされるだけなので、通信が途中で切れても二重計上にならない。
 *
 * 設置手順は README.md の「Google スプレッドシート連携」を参照。
 */

// スクリプトプロパティ SHARED_TOKEN と一致しないリクエストは拒否する
function token_() {
  return PropertiesService.getScriptProperties().getProperty('SHARED_TOKEN') || '';
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
    var body = JSON.parse(e.postData.contents);
    var expected = token_();
    if (!expected || body.token !== expected) return json_({ ok: false, error: 'bad token' });

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    upsertEvents_(ss, body.events || [], body.deleted || []);
    writeProjects_(ss, body.projects || []);
    rebuild_(ss);
    return json_({ ok: true, received: (body.events || []).length });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    try { lock.releaseLock(); } catch (ignore) {}
  }
}

function doGet() {
  return json_({ ok: true, hint: 'POST only' });
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}

function sheet_(ss, name, header) {
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (header && sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

var EV_HEADER = ['id', 'timestamp', 'type', 'employee_id', 'employee',
                 'project_id', 'project_code', 'project', 'process_id', 'process', 'source'];

/* ---------------- events の追記と削除 ---------------- */

function upsertEvents_(ss, rows, deletedIds) {
  var sh = sheet_(ss, 'events', EV_HEADER);

  // 端末で消された打刻をシートからも消す
  if (deletedIds && deletedIds.length) {
    var idx = idIndex_(sh);
    var victims = [];
    deletedIds.forEach(function (id) {
      var r = idx[String(id)];
      if (r) victims.push(r);
    });
    victims.sort(function (a, b) { return b - a; });
    victims.forEach(function (r) { sh.deleteRow(r); });
  }

  if (!rows || !rows.length) return;

  var index = idIndex_(sh);
  var appends = [];
  rows.forEach(function (e) {
    var row = [e.id, e.ts, e.type, e.emp || '', e.empName || '',
               e.proj || '', e.projCode || '', e.projName || '',
               e.proc || '', e.procName || '', e.src || ''];
    var at = index[String(e.id)];
    if (at) sh.getRange(at, 1, 1, row.length).setValues([row]);
    else appends.push(row);
  });
  if (appends.length) {
    sh.getRange(sh.getLastRow() + 1, 1, appends.length, EV_HEADER.length).setValues(appends);
  }
}

// id -> シート上の行番号
function idIndex_(sh) {
  var index = {};
  var last = sh.getLastRow();
  if (last < 2) return index;
  var ids = sh.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < ids.length; i++) index[String(ids[i][0])] = i + 2;
  return index;
}

function writeProjects_(ss, projects) {
  if (!projects || !projects.length) return;
  var sh = sheet_(ss, 'projects', ['project_id', 'code', 'name', 'plan_hours']);
  if (sh.getLastRow() > 1) sh.getRange(2, 1, sh.getLastRow() - 1, 4).clearContent();
  sh.getRange(2, 1, projects.length, 4).setValues(projects.map(function (p) {
    return [p.id, p.code, p.name, p.plan];
  }));
}

/* ---------- events からセッションを組み立てる(端末側と同じ規則) ---------- */

function buildSessions_(evs, now) {
  if (!now) now = Date.now();
  var byEmp = {};
  evs.filter(function (e) { return e.type === 'process' || e.type === 'break'; })
     .sort(function (a, b) { return a.ts - b.ts; })
     .forEach(function (e) {
       if (!byEmp[e.emp]) byEmp[e.emp] = [];
       byEmp[e.emp].push(e);
     });

  var out = [];
  Object.keys(byEmp).forEach(function (emp) {
    var cur = null, brk = null;
    byEmp[emp].forEach(function (e) {
      if (e.type === 'break') {
        if (!cur) return;
        if (brk === null) { brk = e.ts; } else { cur.brk += e.ts - brk; brk = null; }
        return;
      }
      if (cur && brk !== null) {
        cur.brk += e.ts - brk;
        brk = null;
        if (e.proc === cur.procId && e.proj === cur.proj) return;   // 休憩明けに同じ作業へ復帰
      }
      if (!cur) {
        cur = open_(e);
      } else if (e.proc === cur.procId && e.proj === cur.proj) {
        cur.end = e.ts; out.push(cur); cur = null;
      } else {
        cur.end = e.ts; out.push(cur); cur = open_(e);
      }
    });
    // 終了打刻がないものも残す。アプリ側と同じく、いま時点までを経過として数える
    if (cur) {
      if (brk !== null) cur.brk += now - brk;
      cur.isOpen = true;
      out.push(cur);
    }
  });
  out.forEach(function (s) {
    s.net = Math.max(0, (s.end === null ? now : s.end) - s.start - s.brk);
  });
  return out;
}

function open_(e) {
  return { emp: e.emp, empName: e.empName, proj: e.proj, projName: e.projName,
           procId: e.proc, procName: e.procName, start: e.ts, end: null, brk: 0, isOpen: false };
}

function readEvents_(ss) {
  var sh = ss.getSheetByName('events');
  if (!sh || sh.getLastRow() < 2) return [];
  var v = sh.getRange(2, 1, sh.getLastRow() - 1, EV_HEADER.length).getValues();
  return v.map(function (r) {
    return { id: r[0], ts: new Date(r[1]).getTime(), type: r[2],
             emp: r[3], empName: r[4], proj: r[5], projCode: r[6], projName: r[7],
             proc: r[8], procName: r[9], src: r[10] };
  }).filter(function (e) { return e.ts; });
}

/* ---------------- 集計シートの再生成 ---------------- */

function rebuild_(ss) {
  var H = 3600000;
  var tz = Session.getScriptTimeZone();
  var now = Date.now();
  var sessions = buildSessions_(readEvents_(ss), now);

  // --- sessions ---
  var sh = sheet_(ss, 'sessions',
    ['date', 'employee', 'project', 'process', 'start', 'end', 'break_h', 'net_h', 'unfinished']);
  if (sh.getLastRow() > 1) sh.getRange(2, 1, sh.getLastRow() - 1, 9).clearContent();
  if (sessions.length) {
    sh.getRange(2, 1, sessions.length, 9).setValues(sessions.map(function (s) {
      return [Utilities.formatDate(new Date(s.start), tz, 'yyyy-MM-dd'),
              s.empName, s.projName, s.procName,
              Utilities.formatDate(new Date(s.start), tz, 'HH:mm'),
              s.end ? Utilities.formatDate(new Date(s.end), tz, 'HH:mm') : '',
              round2_(s.brk / H), round2_(s.net / H), s.isOpen ? '要確認' : ''];
    }));
  }

  var done = sessions.filter(function (s) { return !s.isOpen; });

  // --- 案件 × 工程 のピボット ---
  // cell[案件名][工程名] = 時間  (区切り文字を使わないので名前に空白があっても壊れない)
  // 進行中のセッションも含める。アプリの集計画面と同じ数字にするため。
  var cell = {};
  sessions.forEach(function (s) {
    if (!cell[s.projName]) cell[s.projName] = {};
    cell[s.projName][s.procName] = (cell[s.projName][s.procName] || 0) + s.net / H;
  });

  var procs = uniq_(sessions.map(function (s) { return s.procName; })).sort();
  var projs = uniq_(sessions.map(function (s) { return s.projName; }));
  projs.sort(function (a, b) { return total_(cell, b, procs) - total_(cell, a, procs); });

  var plan = planMap_(ss);
  var head = ['案件'].concat(procs).concat(['合計', '予定h', '差']);
  var body = projs.map(function (p) {
    var m = cell[p] || {};
    var row = [p];
    var tot = 0;
    procs.forEach(function (c) {
      var v = m[c] || 0;
      tot += v;
      row.push(round2_(v));
    });
    row.push(round2_(tot));
    var pl = plan[p] || 0;
    row.push(pl ? pl : '');
    row.push(pl ? round2_(tot - pl) : '');
    return row;
  });

  var pv = sheet_(ss, 'summary_案件x工程');
  pv.clear();
  if (body.length && procs.length) {
    pv.getRange(1, 1, 1, head.length).setValues([head]).setFontWeight('bold');
    pv.setFrozenRows(1);
    pv.getRange(2, 1, body.length, head.length).setValues(body);
    makeStackedChart_(pv, body.length, procs.length);
  } else {
    // 集計対象がないときは白紙にせず、理由を書いておく
    makeStackedChart_(pv, 0, 0);
    pv.getRange(1, 1).setValue('工程の打刻がまだありません。');
    pv.getRange(2, 1).setValue('社員カード → 案件カード → 工程カード の順にかざすと、ここに集計とグラフが出ます。');
  }

  // --- 工程別(案件横断) ---
  var pc = {};
  sessions.forEach(function (s) { pc[s.procName] = (pc[s.procName] || 0) + s.net / H; });
  writeRanked_(ss, 'summary_工程', ['工程', '合計h'], pc);

  // --- 社員 × 工程 の1回あたり平均 ---
  // acc[工程名][社員名] = {sum, n}
  var acc = {};
  done.forEach(function (s) {
    if (!acc[s.procName]) acc[s.procName] = {};
    var a = acc[s.procName][s.empName];
    if (!a) { a = { sum: 0, n: 0 }; acc[s.procName][s.empName] = a; }
    a.sum += s.net / H;
    a.n++;
  });
  var erows = [];
  Object.keys(acc).forEach(function (pcName) {
    Object.keys(acc[pcName]).forEach(function (emName) {
      var a = acc[pcName][emName];
      erows.push([pcName, emName, a.n, round2_(a.sum / a.n)]);
    });
  });
  erows.sort(function (a, b) {
    if (a[0] === b[0]) return b[3] - a[3];
    return a[0] < b[0] ? -1 : 1;
  });

  var esh = sheet_(ss, 'summary_社員x工程');
  esh.clear();
  esh.getRange(1, 1, 1, 4).setValues([['工程', '社員', '回数', '1回あたり平均h']]).setFontWeight('bold');
  esh.setFrozenRows(1);
  if (erows.length) esh.getRange(2, 1, erows.length, 4).setValues(erows);
}

function total_(cell, proj, procs) {
  var m = cell[proj] || {};
  var t = 0;
  procs.forEach(function (c) { t += m[c] || 0; });
  return t;
}

function planMap_(ss) {
  var m = {};
  var sh = ss.getSheetByName('projects');
  if (!sh || sh.getLastRow() < 2) return m;
  sh.getRange(2, 1, sh.getLastRow() - 1, 4).getValues().forEach(function (r) {
    if (r[2]) m[r[2]] = Number(r[3]) || 0;
  });
  return m;
}

function writeRanked_(ss, name, header, obj) {
  var sh = sheet_(ss, name);
  sh.clear();
  sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold');
  sh.setFrozenRows(1);
  var rows = Object.keys(obj).map(function (k) { return [k, round2_(obj[k])]; });
  rows.sort(function (a, b) { return b[1] - a[1]; });
  if (rows.length) sh.getRange(2, 1, rows.length, 2).setValues(rows);
}

function makeStackedChart_(sh, nRows, nCols) {
  sh.getCharts().forEach(function (c) { sh.removeChart(c); });
  if (!nRows || !nCols) return;
  var chart = sh.newChart()
    .setChartType(Charts.ChartType.COLUMN)
    .addRange(sh.getRange(1, 1, nRows + 1, nCols + 1))   // 案件列 + 工程列(合計列は含めない)
    .setPosition(2, nCols + 6, 0, 0)
    .setOption('title', '案件別の工数(工程内訳)')
    .setOption('isStacked', true)
    .setOption('height', 420)
    .setOption('width', 720)
    .setOption('legend', { position: 'right' })
    .setOption('vAxis', { title: '時間(h)' })
    .build();
  sh.insertChart(chart);
}

function uniq_(a) {
  var seen = {};
  var out = [];
  a.forEach(function (v) {
    if (!seen[v]) { seen[v] = 1; out.push(v); }
  });
  return out;
}

function round2_(n) {
  return Math.round(n * 100) / 100;
}
