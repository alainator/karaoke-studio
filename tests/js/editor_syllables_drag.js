;(function () {
  const ops = [];
  const ctx = new Proxy({}, {get: (t, k) => k in t ? t[k] : (t[k] = (...a) => { if (k === "fillText") ops.push(a[0]); }), set: (t, k, v) => { t[k] = v; return true; }});
  const wave = {clientWidth: 1200, clientHeight: 176, width: 0, height: 0, style: {}, getBoundingClientRect: () => ({left: 0, top: 0}), getContext: () => ctx, setPointerCapture() {}};
  elems["#wave"] = wave; elems["#pv"] = {currentTime: 10.0, paused: true, playbackRate: 1}; elems["#offr"] = {value: "0"};
  ed = {id: "x", j: {}, lines: [{words: [[10.0, 10.4, "these"], [10.5, 13.0, "flimbardies"], [13.6, 14.0, "go"]], syl: [null, [[10.5, 10.8, "flim"], [10.8, 11.1, "bar"], [11.1, 13.0, "dies"]], null]},
                                {words: [[15.0, 16.0, "next"]]}],
        cast: [], shown: {}, ripple: false, sel: null, dirty: false, spec: false, snap: false, onsets: null, view: {span: 12, start: 8, pinned: Date.now()}, peaks: null, skipped: []};
  drawWave();
  const out = {labels: ops.filter(x => ["flim", "bar", "dies", "syllables"].includes(x)), pieces: ed._pieces.length};
  initWave();
  const p = ed._pieces[2], X = t => (t - ed.view.start) / ed.view.span * 1200, y = (p.y0 + p.y1) / 2;
  wave.onpointerdown({button: 0, clientX: X(13.0), clientY: y, pointerId: 1});      // the held last piece's right edge
  wave.onpointermove({clientX: X(13.4), clientY: y});
  out.afterStretch = {word: ed.lines[0].words[1], last: ed.lines[0].syl[1][2]};
  wave.onpointermove({clientX: X(14.5), clientY: y});                               // can't pass the next word
  out.clamped = ed.lines[0].words[1][1];
  try { wave.onpointerup(); out.render = "ok"; } catch (e) { out.render = "renderEditor: " + e.message; }
  const q = ed._pieces.find(x => x.k === 1);
  wave.onpointerdown({button: 0, clientX: X(10.8) + 1, clientY: y, pointerId: 1});   // the boundary flim|bar
  wave.onpointermove({clientX: X(10.95) + 1, clientY: y});
  out.boundary = ed.lines[0].syl[1].slice(0, 2);
  try { wave.onpointerup(); } catch (e) { out.render2 = e.message; }
  out.text = lineEditText(ed.lines[0]);
  console.log(JSON.stringify(out));
})();
