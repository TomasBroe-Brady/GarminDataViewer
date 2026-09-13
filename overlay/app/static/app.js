/* Training Overlay - plan vs actual.
   Deliberately dependency-free: no build step, edit and refresh. */

const $ = (sel) => document.querySelector(sel);

const fmt = {
  min(v) {
    if (v === null || v === undefined) return "-";
    const m = Math.round(v);
    if (m < 60) return `${m}m`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  },
  km(v) {
    return v === null || v === undefined ? "-" : `${v.toFixed(1)} km`;
  },
  pct(v) {
    return v === null || v === undefined ? "-" : `${v.toFixed(0)}%`;
  },
  date(iso) {
    const d = new Date(`${iso}T00:00:00`);
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  },
  num(v, digits = 0) {
    return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
  },
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function banner(msg, kind = "warn") {
  const el = $("#banner");
  if (!msg) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = msg;
  el.style.borderColor = kind === "bad" ? "var(--bad)" : "var(--warn)";
  el.style.color = kind === "bad" ? "var(--bad)" : "var(--warn)";
}

/* ------------------------------------------------------------- summary */

function renderSummary(s) {
  // Adherence reads green at >=90%, amber in the 70s-80s, red below 70.
  const adherenceClass =
    s.adherence_pct === null ? "" : s.adherence_pct >= 90 ? "ok" : s.adherence_pct >= 70 ? "warn" : "bad";

  // Volume: flag being meaningfully over or under the plan in either direction.
  let volumeClass = "";
  if (s.duration_pct !== null && s.duration_pct !== undefined) {
    volumeClass = s.duration_pct > 115 || s.duration_pct < 85 ? "warn" : "ok";
  }

  const tiles = [
    {
      label: "Adherence",
      value: s.adherence_pct === null ? "-" : `${s.adherence_pct}%`,
      sub: `${s.sessions_completed} of ${s.sessions_planned} planned`,
      cls: adherenceClass,
    },
    {
      label: "Missed",
      value: s.sessions_missed,
      sub: "planned but not recorded",
      cls: s.sessions_missed > 0 ? "bad" : "ok",
    },
    {
      label: "Unplanned",
      value: s.sessions_unplanned,
      sub: "recorded but not planned",
      cls: s.sessions_unplanned > 0 ? "warn" : "",
    },
    {
      label: "Time vs plan",
      value: s.duration_pct === null ? "-" : `${s.duration_pct}%`,
      sub: `${fmt.min(s.actual_duration_min)} done / ${fmt.min(s.planned_duration_min)} planned`,
      cls: volumeClass,
    },
    {
      label: "Distance vs plan",
      value: s.distance_pct === null ? "-" : `${s.distance_pct}%`,
      sub: `${fmt.km(s.actual_distance_km)} / ${fmt.km(s.planned_distance_km)}`,
      cls: "",
    },
  ];

  $("#summary").innerHTML = tiles
    .map(
      (t) => `
      <div class="stat">
        <div class="label">${esc(t.label)}</div>
        <div class="value ${t.cls}">${esc(t.value)}</div>
        <div class="sub">${esc(t.sub)}</div>
      </div>`
    )
    .join("");
}

/* --------------------------------------------------------------- weeks */

function deltaCell(delta, unit) {
  if (delta === null || delta === undefined) return "-";
  const cls = delta > 0 ? "over" : delta < 0 ? "under" : "";
  const sign = delta > 0 ? "+" : "";
  return `<span class="delta ${cls}">${sign}${delta.toFixed(unit === "km" ? 2 : 0)}${unit}</span>`;
}

function rowHtml(r) {
  const p = r.planned;
  const a = r.actual;

  const plannedCell = p
    ? `<strong>${esc(p.title || p.session_type)}</strong>
       <div class="muted small">${esc(p.session_type)}${p.planned_intensity ? ` &middot; ${esc(p.planned_intensity)}` : ""}</div>`
    : `<span class="muted">&mdash;</span>`;

  const plannedTarget = p
    ? [
        p.planned_duration_min ? fmt.min(p.planned_duration_min) : null,
        p.planned_distance_km ? fmt.km(p.planned_distance_km) : null,
      ]
        .filter(Boolean)
        .join(" &middot; ") || "-"
    : "-";

  const actualCell = a
    ? `<strong>${esc(a.name || a.activity_type || "Activity")}</strong>
       <div class="muted small">${esc(a.activity_type || "")}${a.avg_hr ? ` &middot; ${fmt.num(a.avg_hr)} bpm avg` : ""}</div>`
    : `<span class="muted">&mdash;</span>`;

  const actualTarget = a
    ? [a.duration_min ? fmt.min(a.duration_min) : null, a.distance_km ? fmt.km(a.distance_km) : null]
        .filter(Boolean)
        .join(" &middot; ") || "-"
    : "-";

  const reasons = r.reasons && r.reasons.length
    ? `<div class="reasons">${esc(r.reasons.join(" &middot; ").replace(/&middot;/g, "·"))}</div>`
    : "";

  return `
    <tr>
      <td><span class="chip ${r.status}">${r.status}</span>${r.manual ? ' <span class="muted small">manual</span>' : ""}</td>
      <td>${esc(fmt.date(r.day))}${r.day_offset ? `<div class="muted small">${r.day_offset > 0 ? "+" : ""}${r.day_offset}d</div>` : ""}</td>
      <td>${plannedCell}${reasons}</td>
      <td class="num hide-sm">${plannedTarget}</td>
      <td>${actualCell}</td>
      <td class="num hide-sm">${actualTarget}</td>
      <td class="num">${deltaCell(r.duration_delta_min, "m")}</td>
      <td class="num hide-sm">${deltaCell(r.distance_delta_km, "km")}</td>
    </tr>`;
}

function renderWeeks(weeks, context) {
  const host = $("#weeks");
  if (!weeks.length) {
    host.innerHTML = `<div class="empty">
      No sessions in this range.<br>
      Import a training log below, or widen the date range.
    </div>`;
    return;
  }

  host.innerHTML = weeks
    .map((w) => {
      const s = w.summary;

      // Average whatever recovery context we have for the week - it is the
      // "should I read anything into this week?" signal.
      const days = w.results.map((r) => r.day);
      const ctx = days
        .map((d) => context[d])
        .filter(Boolean);
      const avg = (key) => {
        const vals = ctx.map((c) => c[key]).filter((v) => v !== undefined && v !== null);
        return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
      };
      const sleep = avg("sleep_hours");
      const rhr = avg("resting_hr");
      const hrv = avg("hrv_overnight");

      const ctxBits = [
        sleep !== null ? `sleep ${sleep.toFixed(1)}h` : null,
        rhr !== null ? `RHR ${rhr.toFixed(0)}` : null,
        hrv !== null ? `HRV ${hrv.toFixed(0)}ms` : null,
      ].filter(Boolean);

      return `
      <div class="week">
        <div class="week-head">
          <strong>Week of ${esc(fmt.date(w.week_start))}</strong>
          <span>${s.sessions_completed}/${s.sessions_planned} sessions</span>
          <span>${fmt.min(s.actual_duration_min)} trained</span>
          ${s.sessions_unplanned ? `<span>${s.sessions_unplanned} unplanned</span>` : ""}
          ${ctxBits.length ? `<span>${esc(ctxBits.join(" · "))}</span>` : ""}
        </div>
        <table>
          <thead>
            <tr>
              <th>Status</th><th>Day</th>
              <th>Planned</th><th class="num hide-sm">Target</th>
              <th>Actual</th><th class="num hide-sm">Recorded</th>
              <th class="num">&Delta; time</th><th class="num hide-sm">&Delta; dist</th>
            </tr>
          </thead>
          <tbody>${w.results.map(rowHtml).join("")}</tbody>
        </table>
      </div>`;
    })
    .join("");
}

/* --------------------------------------------------------------- load */

async function load() {
  const start = $("#start").value;
  const end = $("#end").value;
  const qs = new URLSearchParams();
  if (start) qs.set("start", start);
  if (end) qs.set("end", end);

  $("#weeks").innerHTML = `<div class="empty">Loading…</div>`;

  try {
    const res = await fetch(`/api/overview?${qs}`);
    if (!res.ok) throw new Error(`API returned ${res.status}`);
    const data = await res.json();

    banner(
      data.influx_error
        ? `Garmin data unavailable: ${data.influx_error}. Showing your plan only.`
        : null
    );

    renderSummary(data.summary);
    renderWeeks(data.weeks, data.daily_context || {});

    if (!start) $("#start").value = data.range.start;
    if (!end) $("#end").value = data.range.end;
  } catch (err) {
    banner(`Could not load overview: ${err.message}`, "bad");
    $("#weeks").innerHTML = `<div class="empty">Failed to load.</div>`;
  }
}

/* ------------------------------------------------------------- import */

async function sendFile(dryRun) {
  const input = $("#file");
  if (!input.files.length) {
    banner("Choose a CSV or Excel file first.");
    return;
  }
  const body = new FormData();
  body.append("file", input.files[0]);

  const qs = new URLSearchParams({
    dry_run: String(dryRun),
    distance_in_miles: String($("#miles").checked),
  });

  const out = $("#import-out");
  out.hidden = false;
  out.textContent = dryRun ? "Previewing…" : "Importing…";

  try {
    const res = await fetch(`/api/import?${qs}`, { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `API returned ${res.status}`);
    out.textContent = JSON.stringify(data, null, 2);
    if (!dryRun) {
      banner(null);
      load();
    }
  } catch (err) {
    out.textContent = `Import failed: ${err.message}`;
  }
}

/* --------------------------------------------------------------- init */

$("#reload").addEventListener("click", load);
$("#preview").addEventListener("click", () => sendFile(true));
$("#upload").addEventListener("click", () => sendFile(false));
$("#start").addEventListener("change", load);
$("#end").addEventListener("change", load);

load();
