/* Dashboard front-end. No build step, no dependencies. */
(() => {
  "use strict";

  const state = {
    meta: null,
    teamId: null,
    metric: "total",
    limit: 10,
    side: "batting",
    board: "overall",
    report: null,
  };

  const $ = (id) => document.getElementById(id);
  const api = (path, params) => {
    const url = new URL(path, window.location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== null && v !== undefined) url.searchParams.set(k, v);
    });
    return fetch(url).then((r) => r.json());
  };

  const isRate = () => state.metric === "rate";
  const fmt = (value, digits) => {
    const d = digits === undefined ? (isRate() ? 3 : 2) : digits;
    const sign = value > 0 ? "+" : value < 0 ? "−" : "";
    return sign + Math.abs(value).toFixed(d);
  };
  const metricUnit = (side) => {
    if (state.metric === "rate") return side === "batting" ? "runs/PA" : "runs/9 IP";
    if (state.metric === "per_game") return "runs/game";
    return "runs";
  };
  const esc = (text) =>
    String(text).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------------ chart

  function niceTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    const raw = (max - min) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
    const ticks = [];
    for (let t = Math.ceil(min / step) * step; t <= max + 1e-9; t += step) {
      ticks.push(Math.abs(t) < 1e-9 ? 0 : t);
    }
    return ticks;
  }

  function barPath(x0, x1, y, h, r) {
    const radius = Math.min(r, h / 2, Math.abs(x1 - x0));
    if (x1 >= x0) {
      return `M${x0},${y} H${x1 - radius} Q${x1},${y} ${x1},${y + radius} ` +
             `V${y + h - radius} Q${x1},${y + h} ${x1 - radius},${y + h} H${x0} Z`;
    }
    return `M${x0},${y} H${x1 + radius} Q${x1},${y} ${x1},${y + radius} ` +
           `V${y + h - radius} Q${x1},${y + h} ${x1 + radius},${y + h} H${x0} Z`;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  const el = (name, attrs, text) => {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function drawChart(rows, side) {
    const svg = $("delta-chart");
    svg.textContent = "";
    if (!rows.length) {
      svg.setAttribute("height", 60);
      svg.appendChild(el("text", { x: 8, y: 34, class: "axis-label" },
        "No player clears the sample-size thresholds against this team."));
      return;
    }

    const nameGutter = 168;
    const padRight = 64;
    const padTop = 8;
    const axisHeight = 26;
    const rowHeight = 32;
    const barHeight = 20; // capped well under 24px
    const width = Math.max(svg.clientWidth || svg.parentNode.clientWidth || 640, 420);
    const height = padTop + rows.length * rowHeight + axisHeight;
    const plotLeft = nameGutter;
    const plotRight = width - padRight;

    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("height", height);

    const values = rows.map((r) => r.metric_value);
    const lo = Math.min(0, ...values);
    const hi = Math.max(0, ...values);
    const span = hi - lo || 1;
    const min = lo - span * 0.06;
    const max = hi + span * 0.06;
    const x = (v) => plotLeft + ((v - min) / (max - min)) * (plotRight - plotLeft);
    const zero = x(0);

    const axisY = padTop + rows.length * rowHeight;
    niceTicks(min, max, 5).forEach((tick) => {
      const tx = x(tick);
      svg.appendChild(el("line", {
        x1: tx, x2: tx, y1: padTop, y2: axisY,
        class: tick === 0 ? "zeroline" : "gridline",
      }));
      svg.appendChild(el("text", {
        x: tx, y: axisY + 16, "text-anchor": "middle", class: "axis-label",
      }, tick.toFixed(isRate() ? 2 : 1)));
    });

    rows.forEach((row, index) => {
      const y = padTop + index * rowHeight + (rowHeight - barHeight) / 2;
      const positive = row.metric_value >= 0;
      const tip = x(row.metric_value);
      const group = el("g", { class: "bar-row" });

      group.appendChild(el("text", {
        x: nameGutter - 12, y: y + barHeight / 2 + 4,
        "text-anchor": "end", class: "bar-label",
      }, row.player));

      group.appendChild(el("path", {
        d: barPath(zero, tip, y, barHeight, 4),
        fill: positive ? "var(--pos)" : "var(--neg)",
        class: "bar-shape",
      }));

      group.appendChild(el("text", {
        x: positive ? tip + 7 : tip - 7,
        y: y + barHeight / 2 + 4,
        "text-anchor": positive ? "start" : "end",
        class: "value-label",
      }, fmt(row.metric_value)));

      const hit = el("rect", {
        x: 0, y: padTop + index * rowHeight, width: width, height: rowHeight,
        class: "bar-hit",
      });
      hit.addEventListener("mousemove", (event) => showTooltip(event, row, side));
      hit.addEventListener("mouseleave", hideTooltip);
      hit.addEventListener("click", () => openPlayer(row));
      group.appendChild(hit);

      svg.appendChild(group);
    });
  }

  function showTooltip(event, row, side) {
    const tip = $("tooltip");
    tip.hidden = false;
    tip.innerHTML =
      `<div class="t-title">${esc(row.player)} <span class="badge">${esc(row.team_abbrev)}</span></div>` +
      `<div class="t-row">${fmt(row.metric_value)} ${metricUnit(side)} vs ${esc(row.opponent)}</div>` +
      `<div class="t-row">${row.games_vs} games · ${fmt(row.per_game_vs, 2)} per game vs them, ` +
      `${fmt(row.baseline_per_game, 2)} vs everyone else</div>` +
      `<div class="t-row">Best: ${esc(row.best_game.summary)} (${row.best_game.date})</div>`;
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let left = event.clientX + pad;
    if (left + rect.width > window.innerWidth - 8) left = event.clientX - rect.width - pad;
    let top = event.clientY + pad;
    if (top + rect.height > window.innerHeight - 8) top = event.clientY - rect.height - pad;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }

  const hideTooltip = () => { $("tooltip").hidden = true; };

  // ------------------------------------------------------------------ tables

  function renderTable(table, columns, rows, onRowClick) {
    const head = table.querySelector("thead");
    const body = table.querySelector("tbody");
    head.innerHTML =
      "<tr>" + columns.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("") + "</tr>";
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="${columns.length}" class="empty">Nothing clears the thresholds yet.</td></tr>`;
      return;
    }
    body.innerHTML = rows
      .map((row, i) =>
        `<tr data-index="${i}" class="${onRowClick ? "clickable" : ""}">` +
        columns.map((c) => `<td class="${c.num ? "num" : ""}">${c.cell(row)}</td>`).join("") +
        "</tr>")
      .join("");
    if (onRowClick) {
      body.querySelectorAll("tr").forEach((tr) => {
        const index = Number(tr.dataset.index);
        tr.addEventListener("click", () => onRowClick(rows[index]));
      });
    }
  }

  const signed = (value) =>
    `<span class="${value >= 0 ? "pos" : "neg"}">${fmt(value)}</span>`;

  function deltaColumns(side) {
    return [
      { label: "#", num: true, cell: (r) => r.rank },
      { label: "Player", cell: (r) => esc(r.player) + (r.season_best_vs_them ? ' <span class="badge">season best</span>' : "") },
      { label: "Team", cell: (r) => esc(r.team_abbrev) },
      { label: "G", num: true, cell: (r) => r.games_vs },
      { label: `Δ ${metricUnit(side)}`, num: true, cell: (r) => signed(r.metric_value) },
      { label: "Vs them /g", num: true, cell: (r) => fmt(r.per_game_vs, 2) },
      { label: "Baseline /g", num: true, cell: (r) => fmt(r.baseline_per_game, 2) },
      { label: "Best game vs them", cell: (r) => `${esc(r.best_game.summary)} <span class="badge">${esc(r.best_game.date)}</span>` },
    ];
  }

  const leaderboardColumns = () => [
    { label: "#", num: true, cell: (r) => r.rank },
    { label: "Player", cell: (r) => esc(r.player) },
    { label: "Role", cell: (r) => (r.side === "batting" ? "Batter" : "Pitcher") },
    { label: "Team", cell: (r) => esc(r.team_abbrev) },
    { label: "Owns", cell: (r) => esc(r.opponent) },
    { label: "G", num: true, cell: (r) => r.games_vs },
    { label: "Δ", num: true, cell: (r) => signed(r.metric_value) },
    { label: "Vs them /g", num: true, cell: (r) => fmt(r.per_game_vs, 2) },
    { label: "Baseline /g", num: true, cell: (r) => fmt(r.baseline_per_game, 2) },
  ];

  // ------------------------------------------------------------------ drawer

  function openPlayer(row) {
    hideTooltip();
    api("/api/player", { player_id: row.player_id, side: row.side, team_id: row.opp_team_id })
      .then((detail) => {
        const body = $("drawer-body");
        const rows = detail.games
          .map((g) => {
            const classes = [g.is_vs_selected ? "row-vs" : "", ].join(" ");
            const flag = g.is_season_best ? ' <span class="badge">season best</span>' : "";
            return `<tr class="${classes}"><td>${esc(g.date)}</td>` +
              `<td>${g.is_home ? "vs" : "@"} ${esc(g.opponent_abbrev)}</td>` +
              `<td>${esc(g.summary)}${flag}</td>` +
              `<td class="num ${g.score >= 0 ? "pos" : "neg"}">${fmt(g.score, 2)}</td></tr>`;
          })
          .join("");
        body.innerHTML =
          `<h2>${esc(detail.player)}</h2>` +
          `<p class="caption">${detail.side === "batting" ? "Batting" : "Pitching"} game log, ${detail.season}. ` +
          `Season total ${fmt(detail.season_score, 2)} runs above average over ${detail.games.length} games ` +
          `(${fmt(detail.season_per_game, 2)} per game). Rows against ${esc(row.opponent)} are highlighted.</p>` +
          `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Opp</th><th>Line</th>` +
          `<th class="num">Runs above avg</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        $("drawer").hidden = false;
      });
  }

  // ------------------------------------------------------------------ render

  function renderTiles(report) {
    const counts = report.best_game_counts;
    const tiles = [
      { label: `Batters whose best game was vs ${report.team}`, value: counts.batters,
        note: `of ${report.qualified.batters} qualified opponents' batters` },
      { label: `Pitchers whose best game was vs ${report.team}`, value: counts.pitchers,
        note: `of ${report.qualified.pitchers} qualified opponents' pitchers` },
      { label: "Combined", value: counts.total, note: "season-best games surrendered" },
      // Batters and pitchers are only on the same scale for total and
      // per-game; in rate mode one is runs/PA and the other runs/9 IP, so the
      // unit has to be on the tile or the two numbers invite comparison.
      { label: "Top batter delta", value: report.batters.length ? fmt(report.batters[0].metric_value) : "—",
        note: report.batters.length
          ? `${report.batters[0].player} · ${metricUnit("batting")}`
          : "no qualifiers" },
      { label: "Top pitcher delta", value: report.pitchers.length ? fmt(report.pitchers[0].metric_value) : "—",
        note: report.pitchers.length
          ? `${report.pitchers[0].player} · ${metricUnit("pitching")}`
          : "no qualifiers" },
    ];
    $("tiles").innerHTML = tiles
      .map((t) => `<div class="tile"><div class="label">${esc(t.label)}</div>` +
        `<div class="value">${esc(t.value)}</div><div class="note">${esc(t.note)}</div></div>`)
      .join("");
  }

  function renderReport() {
    const report = state.report;
    if (!report) return;
    const rows = state.side === "batting" ? report.batters : report.pitchers;
    const label = state.side === "batting" ? "batters" : "pitchers";

    renderTiles(report);
    $("chart-title").textContent =
      `Top ${rows.length} ${label} against ${report.team}`;
    $("chart-caption").textContent =
      `${report.metric_label} — performance against ${report.team} minus the same player's ` +
      `average against every other team, in ${metricUnit(state.side)}.`;
    drawChart(rows, state.side);
    renderTable($("delta-table"), deltaColumns(state.side), rows, openPlayer);

    const best = report.best_games[state.side];
    renderTable($("best-games-table"), [
      { label: "Player", cell: (r) => esc(r.player) },
      { label: "Team", cell: (r) => esc(r.team) },
      { label: "Date", cell: (r) => esc(r.game.date) },
      { label: "Line", cell: (r) => esc(r.game.summary) },
      { label: "Runs above avg", num: true, cell: (r) => signed(r.score) },
      { label: "Margin over their 2nd best", num: true, cell: (r) => fmt(r.margin_over_second_best) },
    ], best);
  }

  function loadTeam() {
    if (state.teamId === null) return;
    api("/api/team", { team_id: state.teamId, metric: state.metric, limit: state.limit })
      .then((report) => { state.report = report; renderReport(); });
  }

  function loadLeaderboard() {
    api("/api/leaderboard", { side: state.board, metric: state.metric, limit: state.limit })
      .then((payload) => {
        renderTable($("leaderboard-table"), leaderboardColumns(), payload.rows, openPlayer);
      });
  }

  function loadCounts() {
    api("/api/best-game-counts").then((payload) => {
      renderTable($("counts-table"), [
        { label: "Team", cell: (r) => esc(r.team) },
        { label: "Batters", num: true, cell: (r) => r.batters },
        { label: "Pitchers", num: true, cell: (r) => r.pitchers },
        { label: "Total season-best games allowed", num: true, cell: (r) => r.total },
      ], payload.rows, (row) => {
        state.teamId = row.team_id;
        $("team-select").value = String(row.team_id);
        loadTeam();
        $("team-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  // -------------------------------------------------------------------- init

  function wire() {
    $("team-select").addEventListener("change", (e) => {
      state.teamId = Number(e.target.value);
      loadTeam();
    });
    $("metric-select").addEventListener("change", (e) => {
      state.metric = e.target.value;
      loadTeam();
      loadLeaderboard();
    });
    $("limit-select").addEventListener("change", (e) => {
      state.limit = Number(e.target.value);
      loadTeam();
      loadLeaderboard();
    });
    document.querySelectorAll(".tab[data-side]").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab[data-side]").forEach((t) => {
          t.classList.toggle("is-active", t === tab);
          t.setAttribute("aria-selected", String(t === tab));
        });
        state.side = tab.dataset.side;
        renderReport();
      });
    });
    document.querySelectorAll(".tab[data-board]").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab[data-board]").forEach((t) => {
          t.classList.toggle("is-active", t === tab);
          t.setAttribute("aria-selected", String(t === tab));
        });
        state.board = tab.dataset.board;
        loadLeaderboard();
      });
    });
    $("drawer-close").addEventListener("click", () => { $("drawer").hidden = true; });
    $("drawer").addEventListener("click", (e) => {
      if (e.target === $("drawer")) $("drawer").hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") $("drawer").hidden = true;
    });
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(renderReport, 120);
    });
  }

  const showMessage = (html) => {
    document.querySelector("main").innerHTML =
      `<div class="panel"><p class="empty">${html}</p></div>`;
  };

  api("/api/meta").then((meta) => {
    state.meta = meta;
    if (!meta.teams || !meta.teams.length) {
      showMessage(
        "No data yet. Run <code>python -m mlbdelta ingest</code> " +
        "(or <code>python -m mlbdelta demo</code>) and reload."
      );
      return;
    }
    $("meta-line").innerHTML =
      `${meta.season} season · ${meta.counts.batting_lines.toLocaleString()} batting lines · ` +
      `${meta.counts.pitching_lines.toLocaleString()} pitching lines<br>` +
      `League baseline: ${meta.league.runs_per_nine.toFixed(2)} runs allowed per 9 IP`;
    const select = $("team-select");
    select.innerHTML = meta.teams
      .map((t) => `<option value="${t.team_id}">${esc(t.name)}</option>`)
      .join("");
    state.teamId = meta.teams[0].team_id;
    wire();
    loadTeam();
    loadLeaderboard();
    loadCounts();
  }).catch((error) => {
    // Without this the page just sits there blank if the server is unreachable.
    showMessage(`Could not reach the dashboard API: ${esc(error.message)}`);
  });
})();
