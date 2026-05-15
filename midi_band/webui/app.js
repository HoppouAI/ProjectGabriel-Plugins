const $ = (id) => document.getElementById(id);
const songsEl = $("songs");
const countEl = $("count");
const searchEl = $("search");
const dropEl = $("drop");
const pickerEl = $("picker");
const statusEl = $("uploadStatus");
const hostInfoEl = $("hostInfo");

let allSongs = [];

async function loadSongs() {
  try {
    const r = await fetch("/api/songs");
    const data = await r.json();
    allSongs = data.songs || [];
    hostInfoEl.textContent = data.instance ? `host: ${data.instance}` : "";
    render();
  } catch (e) {
    songsEl.innerHTML = `<li class="empty">failed to load: ${e}</li>`;
  }
}

function render() {
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
      (s) => `<li>
        <span class="name">${escapeHtml(s.name)}</span>
        <span class="size">${formatSize(s.size)}</span>
      </li>`
    )
    .join("");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

async function uploadFiles(files) {
  if (!files || !files.length) return;
  for (const f of files) {
    const li = document.createElement("li");
    li.className = "pending";
    li.textContent = `uploading ${f.name}...`;
    statusEl.prepend(li);
    try {
      const fd = new FormData();
      fd.append("file", f, f.name);
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await r.json();
      if (data.result === "ok") {
        li.className = "ok";
        li.textContent = `\u2713 ${data.name} (${formatSize(data.size)})`;
      } else {
        li.className = "err";
        li.textContent = `\u2717 ${f.name}: ${data.message || "failed"}`;
      }
    } catch (e) {
      li.className = "err";
      li.textContent = `\u2717 ${f.name}: ${e}`;
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
  dropEl.addEventListener(ev, (e) => {
    e.preventDefault();
    dropEl.classList.add("hover");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dropEl.addEventListener(ev, (e) => {
    e.preventDefault();
    dropEl.classList.remove("hover");
  })
);
dropEl.addEventListener("drop", (e) => {
  uploadFiles(e.dataTransfer.files);
});

searchEl.addEventListener("input", render);
$("refresh").addEventListener("click", loadSongs);

loadSongs();
