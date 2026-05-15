const $ = (id) => document.getElementById(id);
const songsEl = $("songs");
const countEl = $("count");
const searchEl = $("search");
const dropEl = $("drop");
const pickerEl = $("picker");
const uploadStatusEl = $("uploadStatus");
const hostInfoEl = $("hostInfo");

const nowSongEl = $("nowSong");
const nowStateEl = $("nowState");
const trackBoxEl = $("trackBox");
const barFillEl = $("barFill");
const barPosEl = $("barPos");
const barDurEl = $("barDur");
const volEl = $("vol");
const volValEl = $("volVal");

const btnPlay = $("btnPlay");
const btnPause = $("btnPause");
const btnResume = $("btnResume");
const btnStop = $("btnStop");
const btnSoundcheck = $("btnSoundcheck");
const btnAutoAssign = $("btnAutoAssign");

let allSongs = [];
let currentSong = null;
let isHost = false;
let lastVolUserChange = 0;

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function fmtTime(t) {
  t = Math.max(0, Math.floor(t || 0));
  const m = Math.floor(t / 60);
  const s = t % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok || data.result === "error") {
    throw new Error(data.message || `HTTP ${r.status}`);
  }
  return data;
}

async function loadSongs() {
  try {
    const data = await api("/api/songs");
    allSongs = data.songs || [];
    hostInfoEl.textContent = data.instance ? `host: ${data.instance}` : "";
    renderLibrary();
  } catch (e) {
    songsEl.innerHTML = `<li class="empty">failed to load: ${e.message}</li>`;
  }
}

function renderLibrary() {
  const q = (searchEl.value || "").toLowerCase().trim();
  const list = q
    ? allSongs.filter((s) => s.name.toLowerCase().includes(q))
    : allSongs;
  countEl.textContent = `(${list.length}${q ? ` of ${allSongs.length}` : ""})`;
  if (!list.length) {
    songsEl.innerHTML = `<li class="empty">${
      allSongs.length ? "no matches" : "library is empty, drop files above"
    }</li>`;
    return;
  }
  songsEl.innerHTML = list
    .map(
      (s) => `<li${s.name === currentSong ? ' class="active"' : ""}>
        <span class="name">${escapeHtml(s.name)}</span>
        <span class="meta">
          <span class="size">${fmtSize(s.size)}</span>
          <span class="row-buttons">
            ${isHost ? `<button data-act="load" data-name="${escapeHtml(s.name)}">Load</button>` : ""}
            <button class="danger" data-act="del" data-name="${escapeHtml(s.name)}">Delete</button>
          </span>
        </span>
      </li>`
    )
    .join("");
}

songsEl.addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const act = btn.dataset.act;
  const name = btn.dataset.name;
  if (!act || !name) return;
  if (act === "load") {
    btn.disabled = true;
    try {
      await api("/api/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: name }),
      });
      await refreshAll();
    } catch (err) {
      flash(`load failed: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
    }
  } else if (act === "del") {
    if (!confirm(`Delete ${name}?`)) return;
    btn.disabled = true;
    try {
      await api(`/api/songs/${encodeURIComponent(name)}`, { method: "DELETE" });
      await loadSongs();
      flash(`deleted ${name}`, "ok");
    } catch (err) {
      flash(`delete failed: ${err.message}`, "err");
      btn.disabled = false;
    }
  }
});

function flash(text, cls) {
  const li = document.createElement("li");
  li.className = cls || "";
  li.textContent = text;
  uploadStatusEl.prepend(li);
  setTimeout(() => li.remove(), 6000);
}

/* uploads */
async function uploadFiles(files) {
  if (!files || !files.length) return;
  for (const f of files) {
    const li = document.createElement("li");
    li.className = "pending";
    li.textContent = `uploading ${f.name}...`;
    uploadStatusEl.prepend(li);
    try {
      const fd = new FormData();
      fd.append("file", f, f.name);
      const data = await api("/api/upload", { method: "POST", body: fd });
      li.className = "ok";
      li.textContent = `\u2713 ${data.name} (${fmtSize(data.size)})`;
    } catch (e) {
      li.className = "err";
      li.textContent = `\u2717 ${f.name}: ${e.message}`;
    }
  }
  await loadSongs();
}

dropEl.addEventListener("click", () => pickerEl.click());
pickerEl.addEventListener("change", () => {
  uploadFiles(pickerEl.files);
  pickerEl.value = "";
});
["dragenter", "dragover"].forEach((ev) =>
  dropEl.addEventListener(ev, (e) => { e.preventDefault(); dropEl.classList.add("hover"); })
);
["dragleave", "drop"].forEach((ev) =>
  dropEl.addEventListener(ev, (e) => { e.preventDefault(); dropEl.classList.remove("hover"); })
);
dropEl.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));

searchEl.addEventListener("input", renderLibrary);
$("refresh").addEventListener("click", loadSongs);

/* control buttons */
async function ctrl(path, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(path, { method: "POST" });
    await refreshStatus();
  } catch (e) {
    flash(`${path}: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

btnPlay.addEventListener("click", () => ctrl("/api/play", btnPlay));
btnPause.addEventListener("click", () => ctrl("/api/pause", btnPause));
btnResume.addEventListener("click", () => ctrl("/api/resume", btnResume));
btnStop.addEventListener("click", () => ctrl("/api/stop", btnStop));
btnAutoAssign.addEventListener("click", () => ctrl("/api/auto_assign", btnAutoAssign));
btnSoundcheck.addEventListener("click", async () => {
  btnSoundcheck.disabled = true;
  try {
    await api("/api/soundcheck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration: 8, bpm: 120 }),
    });
  } catch (e) { flash(e.message, "err"); }
  btnSoundcheck.disabled = false;
});

let volTimer = null;
volEl.addEventListener("input", () => {
  const v = parseFloat(volEl.value);
  volValEl.textContent = v.toFixed(2);
  lastVolUserChange = Date.now();
  clearTimeout(volTimer);
  volTimer = setTimeout(async () => {
    try {
      await api("/api/volume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: v }),
      });
    } catch (e) { flash(`volume: ${e.message}`, "err"); }
  }, 200);
});

/* status polling */
async function refreshStatus() {
  let s;
  try {
    s = await api("/api/status");
  } catch (e) {
    nowStateEl.textContent = `status error: ${e.message}`;
    return;
  }
  isHost = s.role === "host";
  for (const b of [btnPlay, btnPause, btnResume, btnStop, btnSoundcheck, btnAutoAssign, volEl]) {
    b.disabled = !isHost;
  }
  if (!isHost) {
    nowSongEl.textContent = "client mode";
    nowStateEl.textContent = "this instance is a band client. host controls live on the host's webui.";
    trackBoxEl.textContent = "";
    return;
  }
  const newSong = s.song || null;
  if (newSong !== currentSong) {
    currentSong = newSong;
    renderLibrary();
  }
  nowSongEl.textContent = currentSong || "no song loaded";
  let state = "idle";
  if (s.playing) state = "playing";
  else if (s.paused) state = "paused";
  else if (currentSong) state = "loaded";
  const members = s.members || [];
  nowStateEl.textContent = `${state} \u00b7 ${members.length} bandmate${members.length === 1 ? "" : "s"}: ${members.join(", ")}`;

  const dur = Number(s.duration || 0);
  const pos = Number(s.position || 0);
  barFillEl.style.width = dur > 0 ? `${Math.min(100, (pos / dur) * 100)}%` : "0%";
  barPosEl.textContent = fmtTime(pos);
  barDurEl.textContent = fmtTime(dur);

  if (Date.now() - lastVolUserChange > 1500 && typeof s.gain === "number") {
    volEl.value = s.gain;
    volValEl.textContent = Number(s.gain).toFixed(2);
  }

  if (s.tracks && s.tracks.length) {
    const lines = [];
    const assigned = s.assignments || {};
    const hostTracks = s.host_tracks || [];
    const nameOf = (i) => (s.tracks[i] && s.tracks[i].name) || `track ${i}`;
    if (hostTracks.length) lines.push(`${s.instance || "host"}: ${hostTracks.map(nameOf).join(", ")}`);
    for (const [who, idxs] of Object.entries(assigned)) {
      lines.push(`${who}: ${(idxs || []).map(nameOf).join(", ") || "(none)"}`);
    }
    if (!lines.length) lines.push("no track assignments yet, click Auto-assign or use voice");
    trackBoxEl.classList.remove("dim");
    trackBoxEl.textContent = lines.join("\n");
  } else {
    trackBoxEl.classList.add("dim");
    trackBoxEl.textContent = "no song loaded";
  }
}

async function refreshAll() {
  await Promise.all([loadSongs(), refreshStatus()]);
}

refreshAll();
setInterval(refreshStatus, 1000);
