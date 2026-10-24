/* Hantek Bench — live scope UI */
(() => {
  const $ = (id) => document.getElementById(id);
  let plot = null;
  let buses = [];
  let live = false;
  let ws = null;
  let applyTimer = null;

  async function api(path, opts) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
    if (!r.ok) {
      const detail = data.detail;
      const msg = typeof detail === "string" ? detail : (data.error || text || r.statusText);
      throw new Error(msg);
    }
    return data;
  }

  let lastCursors = null;
  let lastTrigger = null;
  let yScaleEma = null; // {min,max} — keep plot from bouncing

  // Fixed-width numeric portals (figure spaces) so digits never shift neighbors
  const FIG = "\u2007";
  function pad(s, width) {
    const t = String(s);
    return t.length >= width ? t.slice(0, width) : FIG.repeat(width - t.length) + t;
  }
  function dash(width) {
    return pad("—", width);
  }

  function fmtV(v) {
    // portal: " -0.000" / "  2.160"  (7)
    if (v == null || Number.isNaN(Number(v))) return dash(7);
    const n = Number(v);
    const body = (Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(3));
    const signed = (n < 0 ? "-" : FIG) + body.replace("-", "");
    return pad(signed, 7);
  }

  function fmtPct(v) {
    // portal: " 48.3" (5) + unit outside
    if (v == null || Number.isNaN(Number(v))) return dash(5);
    return pad(Number(v).toFixed(1), 5);
  }

  function fmtPeriod(s) {
    // portal always "###.### us|ms| s" style → value 7 + unit 2 = stable
    if (s == null || Number.isNaN(Number(s))) return `${dash(7)} ${FIG}${FIG}`;
    const n = Number(s);
    if (n >= 1) return `${pad(n.toFixed(3), 7)} ${FIG}s`;
    if (n >= 1e-3) return `${pad((n * 1e3).toFixed(3), 7)} ms`;
    return `${pad((n * 1e6).toFixed(2), 7)} µs`;
  }

  function fmtFreq(hz) {
    // portal: value 7 + unit 3 ("Hz "|"kHz"|"MHz")
    if (hz == null || Number.isNaN(Number(hz))) return `${dash(7)} ${FIG}${FIG}${FIG}`;
    const n = Number(hz);
    if (n >= 1e6) return `${pad((n / 1e6).toFixed(3), 7)} MHz`;
    if (n >= 1e3) return `${pad((n / 1e3).toFixed(3), 7)} kHz`;
    return `${pad(n.toFixed(2), 7)} ${FIG}Hz`;
  }

  function fmt(v) {
    // legacy helper for decode/alerts
    if (v == null || Number.isNaN(v)) return "—";
    if (Math.abs(v) >= 100) return v.toFixed(1);
    if (Math.abs(v) >= 1) return v.toFixed(3);
    return v.toFixed(4);
  }

  function cell(label, valueHtml, cls) {
    return `<span class="mcell ${cls || ""}"><span class="mlab">${label}</span><span class="mval">${valueHtml}</span></span>`;
  }

  function drawCursors(u) {
    if (!$("showCursors")?.checked || !lastCursors) return;
    const { vmin, vmax, mid, trigger } = lastCursors;
    const ctx = u.ctx;
    const drawH = (yVal, color, dash, label) => {
      if (yVal == null) return;
      const y = u.valToPos(yVal, "y", true);
      if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash(dash || []);
      ctx.beginPath();
      ctx.moveTo(u.bbox.left, y);
      ctx.lineTo(u.bbox.left + u.bbox.width, y);
      ctx.stroke();
      if (label) {
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.font = "11px ui-monospace, Consolas, monospace";
        ctx.fillText(label, u.bbox.left + 6, y - 3);
      }
      ctx.restore();
    };
    drawH(vmax, "rgba(126,200,255,0.85)", [], `Vmax ${fmtV(vmax)}`);
    drawH(mid, "rgba(201,160,255,0.75)", [4, 4], `Mid ${fmtV(mid)}`);
    drawH(vmin, "rgba(126,200,255,0.85)", [], `Vmin ${fmtV(vmin)}`);
    drawH(trigger, "rgba(255,107,107,0.9)", [6, 3], `Trig ${fmtV(trigger)}`);
    // vertical trigger marker
    if (lastTrigger && lastTrigger.index != null && u.data[0]?.length) {
      const ti = Math.min(lastTrigger.index, u.data[0].length - 1);
      const tVal = u.data[0][ti];
      const x = u.valToPos(tVal, "x", true);
      ctx.save();
      ctx.strokeStyle = "rgba(255,107,107,0.55)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, u.bbox.top);
      ctx.lineTo(x, u.bbox.top + u.bbox.height);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,107,107,0.85)";
      ctx.font = "11px ui-monospace, Consolas, monospace";
      ctx.fillText(lastTrigger.locked ? "T" : "t", x + 3, u.bbox.top + 12);
      ctx.restore();
    }
  }

  function ensureLegendShell() {
    const el = $("cursorLegend");
    if (el.dataset.ready === "1") return el;
    el.dataset.ready = "1";
    el.innerHTML = `
      <span class="c-lock">LOCK</span>
      ${cell("f", dash(11), "c-freq")}
      ${cell("Duty", `${dash(5)} %`, "c-duty")}
      ${cell("T", dash(10), "c-per")}
      ${cell("Vmax", `${dash(7)} V`, "c-max")}
      ${cell("Mid", `${dash(7)} V`, "c-mid")}
      ${cell("Vmin", `${dash(7)} V`, "c-min")}
      ${cell("Trig", `${dash(7)} V`, "c-trig")}
      ${cell("Vpp", `${dash(7)} V`, "")}`;
    return el;
  }

  function updateCursorLegend(c) {
    const el = $("cursorLegend");
    if (!c || !$("showCursors")?.checked) {
      el.innerHTML = "";
      el.dataset.ready = "";
      return;
    }
    ensureLegendShell();
    const locked = !!lastTrigger?.locked;
    const lockEl = el.querySelector(".c-lock");
    lockEl.textContent = locked ? "LOCK" : "FREE";
    lockEl.classList.toggle("free", !locked);
    const put = (label, val) => {
      const node = [...el.querySelectorAll(".mcell")].find((n) => n.querySelector(".mlab")?.textContent === label);
      if (node) node.querySelector(".mval").textContent = val;
    };
    put("f", fmtFreq(c.frequency_hz));
    put("Duty", `${fmtPct(c.duty_high_pct)} %`);
    put("T", fmtPeriod(c.period_s));
    put("Vmax", `${fmtV(c.vmax)} V`);
    put("Mid", `${fmtV(c.mid)} V`);
    put("Vmin", `${fmtV(c.vmin)} V`);
    put("Trig", `${fmtV(c.trigger)} V`);
    put("Vpp", `${fmtV(c.vpp)} V`);
  }

  function ensureMeasShell(channels) {
    const root = $("meas");
    const ids = channels.join(",");
    if (root.dataset.chs === ids) return;
    root.dataset.chs = ids;
    root.innerHTML = channels.map((k) => `
      <div class="card" data-ch="${k}">
        <div class="mhead"><strong>${k}</strong><span class="mprobe" data-k="probe"></span></div>
        <div class="mgrid">
          ${cell("f", dash(11), "c-freq")}
          ${cell("Duty", `${dash(5)} %`, "c-duty")}
          ${cell("T", dash(10), "c-per")}
          ${cell("Vpp", `${dash(7)} V`, "")}
          ${cell("Vmax", `${dash(7)} V`, "c-max")}
          ${cell("Mid", `${dash(7)} V`, "c-mid")}
          ${cell("Vmin", `${dash(7)} V`, "c-min")}
          ${cell("Vavg", `${dash(7)} V`, "")}
          ${cell("VRMS", `${dash(7)} V`, "")}
          ${cell("tr", dash(10), "")}
          ${cell("tf", dash(10), "")}
        </div>
      </div>`).join("");
    requestAnimationFrame(fitPlot);
  }

  function renderMeasCards(lm) {
    if (!lm) return;
    const chans = ["CH1", "CH2"].filter((k) => lm[k]);
    ensureMeasShell(chans);
    chans.forEach((k) => {
      const c = lm[k];
      const card = $("meas").querySelector(`[data-ch="${k}"]`);
      if (!card) return;
      const pf = Number(c.probe_factor);
      card.querySelector(".mprobe").textContent = `×${Number.isFinite(pf) ? pf.toFixed(0) : "?"}`;
      const put = (label, val) => {
        const el = [...card.querySelectorAll(".mcell")].find((n) => n.querySelector(".mlab")?.textContent === label);
        if (el) el.querySelector(".mval").textContent = val;
      };
      put("f", fmtFreq(c.frequency_hz));
      put("Duty", `${fmtPct(c.duty_high_pct)} %`);
      put("T", fmtPeriod(c.period_s));
      put("Vpp", `${fmtV(c.vpp)} V`);
      put("Vmax", `${fmtV(c.vmax)} V`);
      put("Mid", `${fmtV(c.mid)} V`);
      put("Vmin", `${fmtV(c.vmin)} V`);
      put("Vavg", `${fmtV(c.vavg)} V`);
      put("VRMS", `${fmtV(c.vrms)} V`);
      put("tr", fmtPeriod(c.rise_time_s));
      put("tf", fmtPeriod(c.fall_time_s));
    });
  }

  function scopeEnabled() {
    return {
      ch1: !!($("ch1On") && $("ch1On").checked),
      ch2: !!($("ch2On") && $("ch2On").checked),
    };
  }

  function ensureOneScopeChannel(changedId) {
    const en = scopeEnabled();
    if (en.ch1 || en.ch2) return;
    // Keep at least one channel on
    const keep = changedId === "ch1On" ? "ch2On" : "ch1On";
    if ($(keep)) $(keep).checked = true;
  }

  function syncScopeChannelUi() {
    const en = scopeEnabled();
    const dim1 = !en.ch1;
    const dim2 = !en.ch2;
    $("ch1Range")?.closest("label")?.classList.toggle("dim", dim1);
    $("pf1")?.closest("label")?.classList.toggle("dim", dim1);
    $("ch2Range")?.closest("label")?.classList.toggle("dim", dim2);
    $("pf2")?.closest("label")?.classList.toggle("dim", dim2);
  }

  function autosetChannelsParam() {
    const v = ($("autoSetCh")?.value || "both").toLowerCase();
    if (v === "ch1") return "CH1";
    if (v === "ch2") return "CH2";
    return "CH1,CH2";
  }

  function renderFromLive(frame) {
    const en = scopeEnabled();
    const t = frame.t || [];
    const series = [];
    if (en.ch1) {
      series.push({ name: "CH1", probe_factor: Number($("pf1").value) || 1, y: frame.ch1 || [] });
    }
    if (en.ch2 && frame.ch2) {
      series.push({ name: "CH2", probe_factor: Number($("pf2").value) || 1, y: frame.ch2 });
    }
    if (!series.length) return;
    // Cursors follow an enabled channel (prefer trigger source / CH1)
    lastCursors = frame.cursors || null;
    lastTrigger = frame.trigger || null;
    if (lastCursors && frame.measure) {
      const src = (frame.trigger?.source || "CH1").toUpperCase();
      const pick = (en.ch1 && src === "CH1") || !en.ch2 ? "CH1" : "CH2";
      const m = frame.measure[pick] || frame.measure[en.ch1 ? "CH1" : "CH2"];
      if (m && m.vmin != null && m.vmax != null) {
        lastCursors = {
          ...lastCursors,
          vmin: m.vmin,
          vmax: m.vmax,
          vpp: m.vpp,
          mid: m.mid != null ? m.mid : 0.5 * (m.vmin + m.vmax),
        };
      }
    }
    updateCursorLegend(lastCursors);
    const lm = {};
    if (en.ch1 && frame.measure?.CH1) lm.CH1 = frame.measure.CH1;
    if (en.ch2 && frame.measure?.CH2) lm.CH2 = frame.measure.CH2;
    renderPlot({ t, series, measure: null, liveMeasure: lm, scopeKey: series.map((s) => s.name).join(",") });
    renderMeasCards(lm);
  }

  function plotSize() {
    const el = $("plot");
    const w = Math.max(200, el.clientWidth || 800);
    // Fill the flex slot exactly — no forced min height (that forced page scroll)
    const h = Math.max(120, el.clientHeight || 240);
    return { width: w, height: h };
  }

  function fitPlot() {
    if (!plot) return;
    const { width, height } = plotSize();
    if (plot.width !== width || plot.height !== height) {
      plot.setSize({ width, height });
    }
  }

  let scopePlotKey = "";

  function renderPlot(payload) {
    const el = $("plot");
    const t = payload.t || [];
    const series = payload.series || [];
    if (!t.length || !series.length) return;
    const data = [t, ...series.map((s) => s.y)];
    const key = payload.scopeKey || series.map((s) => s.name).join(",");
    if (plot && scopePlotKey && scopePlotKey !== key) {
      destroyPlot();
      yScaleEma = null;
    }
    const sz = plotSize();
    const opts = {
      width: sz.width,
      height: sz.height,
      series: [
        { label: "t" },
        ...series.map((s) => ({
          label: `${s.name} ×${s.probe_factor}`,
          stroke: s.name === "CH2" ? "#f0b429" : "#3ecf8e",
          width: 1.5,
        })),
      ],
      axes: [
        { stroke: "#8b9890", grid: { stroke: "#1c2420" }, ticks: { stroke: "#2a333c" },
          values: (u, vals) => vals.map((v) => (v >= 1e-3 ? (v * 1e3).toFixed(2) + " ms" : (v * 1e6).toFixed(0) + " µs")) },
        { stroke: "#8b9890", grid: { stroke: "#1c2420" }, ticks: { stroke: "#2a333c" } },
      ],
      scales: { x: { time: false } },
      // Legend lives in the CH cards below — keep plot height for waveform
      legend: { show: false },
      hooks: {
        draw: [drawCursors],
      },
    };
    if (plot) {
      try {
        fitPlot();
        plot.setData(data, false);
        if (payload.liveMeasure) {
          const tmax = t[t.length - 1] || 0.004;
          plot.setScale("x", { min: 0, max: tmax });
          if (lastCursors) {
            const padY = Math.max(0.08, (lastCursors.vpp || 1) * 0.18);
            const rawMin = lastCursors.vmin - padY;
            const rawMax = lastCursors.vmax + padY;
            if (!yScaleEma) yScaleEma = { min: rawMin, max: rawMax };
            else {
              yScaleEma.min = 0.9 * yScaleEma.min + 0.1 * rawMin;
              yScaleEma.max = 0.9 * yScaleEma.max + 0.1 * rawMax;
            }
            plot.setScale("y", { min: yScaleEma.min, max: yScaleEma.max });
          }
          scopePlotKey = key;
          return;
        }
      } catch (_) {
        plot.destroy();
        plot = null;
      }
    }
    if (!plot) plot = new uPlot(opts, data, el);
    scopePlotKey = key;

    if (payload.liveMeasure) return;
    const meas = payload.measure?.channels || [];
    ensureMeasShell(meas.map((c) => c.name));
    renderMeasCards(Object.fromEntries(meas.map((c) => [c.name, c])));
  }

  function liveCfg() {
    const en = scopeEnabled();
    // Hardware: 1ch = CH1 only; CH2 always needs dual interleaved capture
    const numChannels = en.ch2 ? 2 : 1;
    let trigSrc = "CH1";
    if (!en.ch1 && en.ch2) trigSrc = "CH2";
    return {
      rate_index: Number($("rateIndex").value),
      num_channels: numChannels,
      ch1_range: Number($("ch1Range").value),
      ch2_range: Number($("ch2Range").value),
      probe_ch1: Number($("pf1").value) || 1,
      probe_ch2: Number($("pf2").value) || 1,
      trigger_mode: $("trigMode").value,
      trigger_level: Number($("trigLvl").value) || 0,
      trigger_source: trigSrc,
      display_points: 4000,
      block_samples: 4096,
    };
  }

  function connectWs() {
    if (ws) try { ws.close(); } catch (_) {}
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/live`);
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "frame") renderFromLive(msg.data);
      if (msg.type === "status") updateLiveBadge(msg.data);
    };
    ws.onclose = () => { if (live) setTimeout(connectWs, 500); };
  }

  let lastProbe = null;

  function setRunStatus(text, cls) {
    const el = $("runStatus");
    if (!el) return;
    el.textContent = text;
    el.className = "status" + (cls ? ` ${cls}` : "");
  }

  function updateLiveBadge(st) {
    if (uiMode !== "scope" || !st) return;
    const vid = lastProbe?.devices?.[0];
    const usb = vid ? `${vid.vid}:${vid.pid}` : "scope";
    if (st.running) {
      setRunStatus(
        `OSC · ${usb} · ${st.fps ? st.fps.toFixed(0) : "—"} fps · ${st.rate_label || ""} · ${st.frames || 0} fr`,
        "ok",
      );
    } else {
      setRunStatus(
        st.error ? `OSC · ${usb} · err: ${st.error}` : `OSC · ${usb} · live off`,
        st.error ? "bad" : "",
      );
    }
  }

  async function startLive() {
    const d = await api("/api/live/start", { method: "POST", body: JSON.stringify(liveCfg()) });
    live = !!d.running;
    $("btnLive").textContent = live ? "Stop live" : "Start live";
    $("btnLive").classList.toggle("primary", !live);
    updateLiveBadge(d);
    if (live) connectWs();
    else if (ws) ws.close();
    if (d.error) throw new Error(d.error);
  }

  async function stopLive() {
    const d = await api("/api/live/stop", { method: "POST", body: "{}" });
    live = false;
    $("btnLive").textContent = "Start live";
    $("btnLive").classList.add("primary");
    updateLiveBadge(d);
    if (ws) ws.close();
  }

  function scheduleApply() {
    if (!live) return;
    clearTimeout(applyTimer);
    applyTimer = setTimeout(async () => {
      try {
        const d = await api("/api/live/config", { method: "POST", body: JSON.stringify(liveCfg()) });
        updateLiveBadge(d);
      } catch (e) {
        console.warn(e);
      }
    }, 250);
  }

  const LA_COLORS = ["#3ecf8e", "#f0b429", "#6ec6ff", "#e07a5f", "#b388ff", "#80cbc4", "#ff8a65", "#cfd8dc"];
  let uiMode = "scope";
  let laLive = false;
  let laWs = null;
  let laDecoders = {};
  let laApplyTimer = null;
  let laPlotKey = ""; // channel set the current uPlot was built for
  let laChanUserTouch = 0; // ignore status sync briefly after manual toggle

  function destroyPlot() {
    if (plot) {
      try { plot.destroy(); } catch (_) {}
      plot = null;
    }
    laPlotKey = "";
    scopePlotKey = "";
    const el = $("plot");
    if (el) el.innerHTML = "";
  }

  function ensureLaChanToggles() {
    const box = $("laChanToggles");
    if (!box || box.dataset.ready) return;
    box.innerHTML = Array.from({ length: 8 }, (_, i) => `
      <label class="chk"><input type="checkbox" class="la-ch" value="D${i}" checked /> D${i}</label>
    `).join("");
    box.dataset.ready = "1";
    box.querySelectorAll(".la-ch").forEach((el) => el.addEventListener("change", () => {
      laChanUserTouch = Date.now();
      // Keep at least one channel enabled
      const checked = document.querySelectorAll(".la-ch:checked");
      if (!checked.length) {
        el.checked = true;
        return;
      }
      scheduleLaApply();
    }));
  }

  function selectedLaChannels() {
    const boxes = document.querySelectorAll(".la-ch:checked");
    if (!boxes.length) return "D0";
    return Array.from(boxes).map((b) => b.value).join(",");
  }

  function setSelectedLaChannels(csv) {
    // Don't fight the user while they are clicking toggles
    if (Date.now() - laChanUserTouch < 2500) return;
    const set = new Set((csv || "").split(",").map((s) => s.trim()).filter(Boolean));
    document.querySelectorAll(".la-ch").forEach((el) => {
      el.checked = set.size ? set.has(el.value) : true;
    });
  }

  function laCfg() {
    return {
      samplerate: $("laRate").value,
      samples: $("laSamples").value,
      channels: selectedLaChannels(),
      trigger_mode: $("laTrigMode").value,
      trigger_channel: $("laTrigCh").value,
      display_points: 8000,
    };
  }

  function setUiMode(mode) {
    uiMode = mode === "la" ? "la" : "scope";
    const isLa = uiMode === "la";
    document.body.dataset.mode = uiMode;
    $("btnScope").classList.toggle("active", !isLa);
    $("btnLa").classList.toggle("active", isLa);
    $("btnLa").disabled = !isLa;
    if ($("plotTitle")) $("plotTitle").textContent = isLa ? "Logic analyzer" : "Scope";
    // Scope toolbar only in scope; LA toolbar + channel toggles only in LA
    if ($("scopeControls")) $("scopeControls").hidden = isLa;
    if ($("laControls")) $("laControls").hidden = !isLa;
    if ($("laChanToggles")) {
      $("laChanToggles").hidden = !isLa;
      if (isLa) ensureLaChanToggles();
    }
    document.querySelectorAll(".scope-side").forEach((el) => { el.hidden = isLa; });
    document.querySelectorAll(".la-side").forEach((el) => { el.hidden = !isLa; });
  }

  function renderLaFrame(frameOrSummary) {
    const seriesMap = frameOrSummary.series || {};
    const nameOrder = Array.isArray(frameOrSummary.channels) && typeof frameOrSummary.channels[0] === "string"
      ? frameOrSummary.channels
      : Object.keys(seriesMap);
    const measureMap = frameOrSummary.measure || {};
    const chans = nameOrder.map((n) => measureMap[n] || { name: n }).filter((c) => c && c.name);
    const summary = {
      t: frameOrSummary.t || [],
      series: seriesMap,
      channels: chans.length ? chans : (frameOrSummary.channels || []),
      n_samples: frameOrSummary.n_samples,
      samplerate_hz: frameOrSummary.samplerate_hz,
      duration_s: frameOrSummary.duration_s,
      path: frameOrSummary.path,
    };
    // Legacy summarize_la_csv shape (channels already metric objects, series present)
    if (!nameOrder.length && Array.isArray(frameOrSummary.channels) && frameOrSummary.channels[0]?.name) {
      summary.channels = frameOrSummary.channels;
    }

    const box = $("laChanSummary");
    if (box) {
      box.innerHTML = (summary.channels || []).map((c) => `
        <div class="item"><b>${c.name}</b> · ${c.last ? "HIGH" : "LOW"}
        · ${c.frequency_hz != null ? (c.frequency_hz >= 1e3 ? (c.frequency_hz / 1e3).toFixed(2) + " kHz" : c.frequency_hz.toFixed(1) + " Hz") : "—"}
        · duty ${c.duty_high_pct != null ? c.duty_high_pct.toFixed(1) : (c.high_pct != null ? c.high_pct : "—")}% · edges ${c.edges ?? "—"}${c.stuck ? " · stuck" : ""}</div>
      `).join("") || `<div class="item muted">No channels</div>`;
    }

    const t = summary.t || [];
    const names = nameOrder.length ? nameOrder : Object.keys(seriesMap);
    if (!t.length || !names.length) return;
    // Stable colors by channel index (D0→color0) even when subset enabled
    const colorOf = (name) => {
      const m = /^D(\d+)$/i.exec(name);
      const i = m ? Number(m[1]) : names.indexOf(name);
      return LA_COLORS[((i % LA_COLORS.length) + LA_COLORS.length) % LA_COLORS.length];
    };
    const stacked = names.map((name, i) => {
      const base = (names.length - 1 - i) * 1.35;
      return { name, y: (seriesMap[name] || []).map((v) => base + (v ? 1 : 0.08)) };
    });
    lastCursors = null;
    yScaleEma = null;
    const el = $("plot");
    const data = [t, ...stacked.map((s) => s.y)];
    const key = names.join(",");
    const needRebuild = !plot || laPlotKey !== key || plot.series.length !== stacked.length + 1;
    if (needRebuild) {
      destroyPlot();
      const sz = plotSize();
      plot = new uPlot({
        width: sz.width,
        height: sz.height,
        series: [
          { label: "t" },
          ...stacked.map((s) => ({
            label: s.name,
            stroke: colorOf(s.name),
            width: 1.4,
            paths: uPlot.paths.stepped({ align: 1 }),
          })),
        ],
        axes: [
          { stroke: "#8b9890", grid: { stroke: "#1c2420" }, ticks: { stroke: "#2a333c" },
            values: (u, vals) => vals.map((v) => (v >= 1e-3 ? (v * 1e3).toFixed(2) + " ms" : (v * 1e6).toFixed(0) + " µs")) },
          { show: false },
        ],
        scales: { x: { time: false }, y: { min: -0.2, max: Math.max(1.2, names.length * 1.35) } },
        legend: { show: false },
      }, data, el);
      laPlotKey = key;
    } else {
      try {
        fitPlot();
        plot.setData(data, false);
        const tmax = t[t.length - 1] || 0.001;
        plot.setScale("x", { min: 0, max: tmax });
        plot.setScale("y", { min: -0.2, max: Math.max(1.2, names.length * 1.35) });
      } catch (_) {
        destroyPlot();
        return renderLaFrame(frameOrSummary);
      }
    }

    $("meas").innerHTML = (summary.channels || []).map((c) => `
      <div class="card"><div class="mhead"><strong>${c.name}</strong><span class="mprobe">${c.stuck ? "idle" : "active"}</span></div>
      <div class="mgrid">
        ${cell("lvl", c.last ? "HIGH" : "LOW")}
        ${cell("f", fmtFreq(c.frequency_hz))}
        ${cell("duty", (c.duty_high_pct != null ? pad(Number(c.duty_high_pct).toFixed(1), 5) : dash(5)) + "%")}
        ${cell("pw", fmtPeriod(c.pulse_width_s))}
        ${cell("edges", pad(String(c.edges ?? "—"), 6))}
      </div></div>`).join("");
    const sr = summary.samplerate_hz;
    $("cursorLegend").textContent = summary.duration_s != null
      ? `${summary.n_samples} sa · ${sr >= 1e6 ? (sr / 1e6).toFixed(2) + " MHz" : (sr / 1e3).toFixed(0) + " kHz"} · ${(summary.duration_s * 1e3).toFixed(2)} ms`
      : `${summary.n_samples || "—"} samples`;
  }

  function updateLaLiveBadge(st) {
    if (uiMode !== "la" || !st) return;
    const vid = lastProbe?.devices?.[0];
    const usb = vid ? `${vid.vid}:${vid.pid}` : "la";
    if (st.running) {
      const bps = st.stream_bps ? `${(st.stream_bps / 1e6).toFixed(1)} MB/s` : "";
      setRunStatus(
        `LA · ${usb} · ${st.fps ? st.fps.toFixed(0) : "—"} fps · ${st.rate_label || ""} · ${bps} · ${st.frames || 0} fr`,
        "ok",
      );
    } else {
      setRunStatus(
        st.error ? `LA · ${usb} · err: ${st.error}` : `LA · ${usb} · live off`,
        st.error ? "bad" : "",
      );
    }
  }

  function connectLaWs() {
    if (laWs) try { laWs.close(); } catch (_) {}
    const proto = location.protocol === "https:" ? "wss" : "ws";
    laWs = new WebSocket(`${proto}://${location.host}/ws/la`);
    laWs.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "frame") renderLaFrame(msg.data);
      if (msg.type === "status") updateLaLiveBadge(msg.data);
    };
    laWs.onclose = () => { if (laLive) setTimeout(connectLaWs, 500); };
  }

  async function startLaLive() {
    if (live) await stopLive();
    const d = await api("/api/la/live/start", { method: "POST", body: JSON.stringify(laCfg()) });
    laLive = !!d.running;
    $("btnLaLive").textContent = laLive ? "Stop LA live" : "Start LA live";
    $("btnLaLive").classList.toggle("primary", !laLive);
    updateLaLiveBadge(d);
    if (laLive) connectLaWs();
    else if (laWs) laWs.close();
    if (d.error) throw new Error(d.error);
  }

  async function stopLaLive() {
    const d = await api("/api/la/live/stop", { method: "POST", body: "{}" });
    laLive = false;
    $("btnLaLive").textContent = "Start LA live";
    $("btnLaLive").classList.add("primary");
    updateLaLiveBadge(d);
    if (laWs) laWs.close();
  }

  function scheduleLaApply() {
    if (!laLive) return;
    clearTimeout(laApplyTimer);
    laApplyTimer = setTimeout(async () => {
      try {
        // Channel/rate change rebuilds the stream — clear stale plot first
        destroyPlot();
        const d = await api("/api/la/live/config", { method: "POST", body: JSON.stringify(laCfg()) });
        updateLaLiveBadge(d);
        laLive = !!d.running;
        $("btnLaLive").textContent = laLive ? "Stop LA live" : "Start LA live";
        $("btnLaLive").classList.toggle("primary", !laLive);
        if (laLive) connectLaWs();
        else if (laWs) try { laWs.close(); } catch (_) {}
        if (d.error) throw new Error(d.error);
      } catch (e) {
        console.warn(e);
        setRunStatus(`LA · err: ${e.message || e}`, "bad");
      }
    }, 200);
  }

  async function refreshStatus() {
    const s = await api("/api/status");
    const pr = s.probe;
    lastProbe = pr;
    $("modeHint").textContent = s.mode_ui.toggle_hint;
    setUiMode(pr.mode === "la" ? "la" : "scope");
    if (!pr.ok && pr.mode === "none") {
      setRunStatus("USB · no device", "bad");
    }
    if (s.la_decoders) {
      laDecoders = s.la_decoders;
      const sel = $("laProto");
      if (sel && !sel.dataset.ready) {
        sel.innerHTML = Object.keys(laDecoders).map((k) => `<option value="${k}">${k}</option>`).join("");
        sel.dataset.ready = "1";
        sel.onchange = () => {
          const p = laDecoders[sel.value];
          $("laProtoHint").textContent = p ? p.hint : "";
          if (p && p.protocol && p.protocol.includes(":")) {
            const map = p.protocol.split(":").slice(1).join(":");
            $("laMap").placeholder = map;
          }
        };
        sel.onchange();
      }
    }
    if (uiMode === "la") {
      updateLaLiveBadge(s.la_live);
      laLive = !!(s.la_live && s.la_live.running);
      $("btnLaLive").textContent = laLive ? "Stop LA live" : "Start LA live";
      if (s.la_live) {
        if (s.la_live.samplerate) $("laRate").value = s.la_live.samplerate;
        if (s.la_live.samples) $("laSamples").value = s.la_live.samples;
        if (s.la_live.trigger_mode) $("laTrigMode").value = s.la_live.trigger_mode;
        if (s.la_live.trigger_channel) $("laTrigCh").value = s.la_live.trigger_channel;
        if (s.la_live.channels) setSelectedLaChannels(s.la_live.channels);
      }
    } else {
      updateLiveBadge(s.live);
      live = !!(s.live && s.live.running);
      $("btnLive").textContent = live ? "Stop live" : "Start live";
    }

    const sel = $("calFreq");
    if (sel && !sel.options.length) {
      (s.cal_steps_hz || []).forEach((hz) => {
        const o = document.createElement("option");
        o.value = hz;
        o.textContent = hz >= 1000 ? `${hz / 1000} kHz` : `${hz} Hz`;
        if (hz === 1000) o.selected = true;
        sel.appendChild(o);
      });
    }
    return pr;
  }

  async function refreshBuses() {
    buses = await api("/api/buses");
    const sel = $("busSelect");
    if (sel) {
      sel.innerHTML = "";
      buses.forEach((b) => {
        const o = document.createElement("option");
        o.value = b.id;
        o.textContent = b.name;
        if (!(b.modes || []).includes("scope")) o.disabled = true;
        sel.appendChild(o);
      });
    }
    $("busCatalog").innerHTML = buses.map((b) => `
      <div class="item"><b>${b.name}</b> · ${(b.modes || []).join("/") || "n/a"}
      <div>${b.hint}</div></div>`).join("");
    updateBusHint();
  }

  function updateBusHint() {
    const b = buses.find((x) => x.id === $("busSelect").value);
    $("busHint").textContent = b ? b.hint : "";
  }

  async function refreshProbes() {
    const list = await api("/api/probes");
    $("probeList").innerHTML = list.length
      ? list.map((p) => `<div class="item"><b>${p.name}</b> · ${p.model || "?"} · ×${p.attenuation} · ${p.channel_default}</div>`).join("")
      : `<div class="item muted">No profiles yet</div>`;
  }

  function syncControlsFromLive(st) {
    if (!st) return;
    if (st.rate_index != null) $("rateIndex").value = String(st.rate_index);
    if (st.ch1_range != null) $("ch1Range").value = String(st.ch1_range);
    if (st.ch2_range != null) $("ch2Range").value = String(st.ch2_range);
    if (st.trigger_mode) $("trigMode").value = st.trigger_mode;
    if (st.trigger_level != null) $("trigLvl").value = String(Number(st.trigger_level).toFixed(2));
    if (st.probe_ch1 != null) $("pf1").value = String(st.probe_ch1);
    if (st.probe_ch2 != null) $("pf2").value = String(st.probe_ch2);
  }

  $("btnLive").onclick = async () => {
    try {
      if (live) await stopLive();
      else await startLive();
    } catch (e) {
      $("meas").innerHTML = `<div class="card" style="color:#d4544a">${e.message}</div>`;
    }
  };

  $("btnAutoSet").onclick = async () => {
    const btn = $("btnAutoSet");
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Auto-set…";
    try {
      if (!live) await startLive();
      const chans = autosetChannelsParam();
      // Ensure channels used by autoset are visible
      if (chans.includes("CH1") && $("ch1On")) $("ch1On").checked = true;
      if (chans.includes("CH2") && $("ch2On")) $("ch2On").checked = true;
      syncScopeChannelUi();
      const r = await api("/api/live/autoset", {
        method: "POST",
        body: JSON.stringify({
          probe_ch1: Number($("pf1").value) || 1,
          probe_ch2: Number($("pf2").value) || 1,
          channels: chans,
          max_passes: 3,
        }),
      });
      live = !!(r.live && r.live.running);
      $("btnLive").textContent = live ? "Stop live" : "Start live";
      updateLiveBadge(r.live);
      syncControlsFromLive(r.live);
      if (live) connectWs();
      const why = (r.suggestion?.reasons || []).join(" · ");
      if (why) console.info("autoset", r);
      if (!r.ok) throw new Error(r.error || "autoset failed");
      btn.textContent = "Auto-set ✓";
      setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch (e) {
      btn.textContent = prev;
      alert(e.message || String(e));
    } finally {
      btn.disabled = false;
      if (btn.textContent === "Auto-set…") btn.textContent = prev;
    }
  };

  $("btnCapture").onclick = async () => {
    try {
      if (live) await stopLive();
      const en = scopeEnabled();
      const channels = [en.ch1 && "CH1", en.ch2 && "CH2"].filter(Boolean).join(",") || "CH1";
      const payload = await api("/api/capture", {
        method: "POST",
        body: JSON.stringify({
          channels,
          samplerate: "1m",
          samples: "20k",
          probe_factor_ch1: Number($("pf1").value) || 1,
          probe_factor_ch2: Number($("pf2").value) || 1,
        }),
      });
      // Prefer showing note that dual sigrok may be wrong — still show
      const series = (payload.series || []).filter((s) =>
        (s.name === "CH1" && en.ch1) || (s.name === "CH2" && en.ch2)
      );
      renderPlot({ ...payload, series, scopeKey: series.map((s) => s.name).join(",") });
    } catch (e) {
      $("meas").innerHTML = `<div class="card" style="color:#d4544a">${e.message}</div>`;
    }
  };

  ["rateIndex", "ch1Range", "ch2Range", "pf1", "pf2", "trigMode", "trigLvl"].forEach((id) => {
    $(id).addEventListener("change", scheduleApply);
  });
  ["ch1On", "ch2On"].forEach((id) => {
    $(id)?.addEventListener("change", () => {
      ensureOneScopeChannel(id);
      syncScopeChannelUi();
      yScaleEma = null;
      scopePlotKey = "";
      scheduleApply();
    });
  });
  syncScopeChannelUi();
  $("showCursors").addEventListener("change", () => {
    updateCursorLegend(lastCursors);
    if (plot) plot.redraw();
  });

  $("btnDualCal").onclick = async () => {
    $("btnDualCal").disabled = true;
    $("dualCalOut").textContent = "Live dual-probe compare on cal pin…";
    try {
      const r = await api("/api/dual-cal-check", {
        method: "POST",
        body: JSON.stringify({
          pf_ch1: Number($("pf1").value) || 1,
          pf_ch2: Number($("pf2").value) || 10,
        }),
      });
      $("dualCalOut").textContent = JSON.stringify(r, null, 2);
      live = !!(r.live && r.live.running);
      $("btnLive").textContent = live ? "Stop live" : "Start live";
      updateLiveBadge(r.live);
      if (live) connectWs();
    } catch (e) {
      $("dualCalOut").textContent = String(e.message || e);
    } finally {
      $("btnDualCal").disabled = false;
    }
  };

  $("btnCalFreq").onclick = async () => {
    $("btnCalFreq").disabled = true;
    $("calFreqOut").textContent = "Setting cal frequency (pause → 0xE6 → resume)…";
    try {
      // Keep live running — server pauses sampling around the USB command
      if (!live) await startLive();
      const r = await api("/api/calfreq", {
        method: "POST",
        body: JSON.stringify({ freq_hz: Number($("calFreq").value) }),
      });
      live = !!(r.live && r.live.running);
      $("btnLive").textContent = live ? "Stop live" : "Start live";
      updateLiveBadge(r.live);
      if (live) connectWs();
      const v = r.verified;
      const line = v
        ? `${r.ok ? "OK" : "FAIL"}: target ${v.target_hz} Hz → measured ${v.measured_hz != null ? Number(v.measured_hz).toFixed(1) : "—"} Hz`
        : (r.ok ? "SETCALFREQ sent" : (r.error || "failed"));
      $("calFreqOut").textContent = line + "\n" + JSON.stringify(r, null, 2);
    } catch (e) {
      $("calFreqOut").textContent = String(e.message || e);
    } finally {
      $("btnCalFreq").disabled = false;
    }
  };

  $("btnProbeCal").onclick = async () => {
    $("btnProbeCal").disabled = true;
    $("probeCalOut").textContent = "Running… tip on cal…";
    try {
      if (live) await stopLive();
      const r = await api("/api/probe-cal", {
        method: "POST",
        body: JSON.stringify({
          name: $("probeName").value || "probe",
          model: $("probeModel").value || "",
          attenuation: Number($("probeAtt").value) || 1,
          channel: $("probeCh").value,
          notes: "bench auto-cal",
        }),
      });
      $("probeCalOut").textContent = JSON.stringify(r, null, 2);
      await refreshProbes();
    } catch (e) {
      $("probeCalOut").textContent = String(e.message || e);
    } finally {
      $("btnProbeCal").disabled = false;
    }
  };

  $("busSelect").onchange = updateBusHint;
  $("btnDecode").onclick = async () => {
    $("btnDecode").disabled = true;
    $("decodeOut").textContent = "Capturing + decoding…";
    try {
      if (live) await stopLive();
      const baudRaw = $("baud").value.trim();
      const r = await api("/api/decode", {
        method: "POST",
        body: JSON.stringify({
          bus: $("busSelect").value,
          wiring: $("wiring").value,
          channel: $("decCh").value,
          baud: baudRaw ? Number(baudRaw) : null,
          samplerate: "2m",
          samples: "50k",
          probe_factor_ch1: Number($("pf1").value) || 1,
          probe_factor_ch2: Number($("pf2").value) || 1,
        }),
      });
      if (r.waveform) renderPlot(r.waveform);
      const d = r.decode || {};
      $("decodeOut").textContent =
        `bus=${d.bus} baud=${d.baud_est ?? "?"} thr=${fmt(d.threshold_v)} V\n` +
        `text: ${d.text || ""}\n` +
        `hex: ${(d.hex_bytes || []).slice(0, 64).join(" ")}\n` +
        (d.notes || []).join("\n");
    } catch (e) {
      $("decodeOut").textContent = String(e.message || e);
    } finally {
      $("btnDecode").disabled = false;
    }
  };

  $("btnDoctor").onclick = async () => {
    try {
      const r = await api("/api/doctor");
      alert(r.ok ? "Doctor OK" : "Doctor found issues — see console");
      console.log(r);
    } catch (e) {
      alert(e.message);
    }
  };

  $("btnLaLive").onclick = async () => {
    try {
      if (laLive) await stopLaLive();
      else await startLaLive();
    } catch (e) {
      $("laOut").textContent = String(e.message || e);
    }
  };

  $("btnLaAutoSet").onclick = async () => {
    const btn = $("btnLaAutoSet");
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Auto-set…";
    try {
      if (live) await stopLive();
      const r = await api("/api/la/live/autoset", { method: "POST", body: "{}" });
      laLive = !!(r.live && r.live.running);
      $("btnLaLive").textContent = laLive ? "Stop LA live" : "Start LA live";
      updateLaLiveBadge(r.live);
      if (r.suggestion) {
        if (r.suggestion.samplerate) $("laRate").value = r.suggestion.samplerate;
        if (r.suggestion.samples) $("laSamples").value = r.suggestion.samples;
        if (r.suggestion.trigger_mode) $("laTrigMode").value = r.suggestion.trigger_mode;
        if (r.suggestion.trigger_channel) $("laTrigCh").value = r.suggestion.trigger_channel;
        if (r.suggestion.channels) setSelectedLaChannels(r.suggestion.channels);
      }
      if (r.summary) renderLaFrame(r.summary);
      if (laLive) connectLaWs();
      if (!r.ok) throw new Error(r.error || "LA autoset found no activity");
      btn.textContent = "Auto-set ✓";
      setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch (e) {
      btn.textContent = prev;
      alert(e.message || String(e));
    } finally {
      btn.disabled = false;
      if (btn.textContent === "Auto-set…") btn.textContent = prev;
    }
  };

  $("btnLaSingle").onclick = async () => {
    try {
      const r = await api("/api/la/live/single", { method: "POST", body: "{}" });
      if (!r.ok) throw new Error(r.error || "single failed");
      if (r.summary) renderLaFrame(r.summary);
      $("laOut").textContent = r.summary?.path || "ok";
    } catch (e) {
      $("laOut").textContent = String(e.message || e);
    }
  };

  ["laRate", "laSamples", "laTrigMode", "laTrigCh"].forEach((id) => {
    $(id).addEventListener("change", scheduleLaApply);
  });

  $("btnLaDecode").onclick = async () => {
    $("btnLaDecode").disabled = true;
    $("laOut").textContent = "Decoding…";
    try {
      if (laLive) await stopLaLive();
      if (live) await stopLive();
      const map = $("laMap").value.trim();
      const r = await api("/api/la/decode", {
        method: "POST",
        body: JSON.stringify({
          protocol: $("laProto").value,
          samples: $("laSamples").value,
          samplerate: $("laRate").value,
          channels: selectedLaChannels(),
          channel_map: map || null,
          trigger: $("laTrigMode").value === "off" ? null : `${$("laTrigCh").value}=${({ rising: "r", falling: "f", either: "e", high: "1", low: "0" })[$("laTrigMode").value] || "r"}`,
          wait_trigger: $("laTrigMode").value !== "off",
        }),
      });
      const ann = (r.annotations || []).slice(0, 80).join("\n");
      $("laOut").textContent = ann || (r.text || "") || JSON.stringify(r, null, 2);
    } catch (e) {
      $("laOut").textContent = String(e.message || e);
    } finally {
      $("btnLaDecode").disabled = false;
    }
  };

  $("btnLaMeasure").onclick = async () => {
    $("btnLaMeasure").disabled = true;
    $("laOut").textContent = "Measuring…";
    try {
      if (laLive) await stopLaLive();
      const r = await api("/api/la/measure", {
        method: "POST",
        body: JSON.stringify({
          samples: $("laSamples").value,
          samplerate: $("laRate").value,
          channels: selectedLaChannels(),
        }),
      });
      if (r.measure) renderLaFrame(r.measure);
      $("laOut").textContent = JSON.stringify(r.measure?.channels || r, null, 2);
    } catch (e) {
      $("laOut").textContent = String(e.message || e);
    } finally {
      $("btnLaMeasure").disabled = false;
    }
  };

  window.addEventListener("resize", fitPlot);
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      requestAnimationFrame(fitPlot);
    });
    ro.observe($("plot"));
    const panel = document.querySelector(".plot-panel");
    if (panel) ro.observe(panel);
  }

  (async () => {
    const pr = await refreshStatus();
    await refreshBuses();
    await refreshProbes();
    setInterval(refreshStatus, 5000);
    requestAnimationFrame(fitPlot);
    if (pr && pr.mode === "scope") {
      try { await startLive(); } catch (e) { console.warn("live autostart", e); }
    } else if (pr && pr.mode === "la") {
      try { await startLaLive(); } catch (e) { console.warn("la live autostart", e); }
    }
  })();
})();
