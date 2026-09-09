/* Curation desk — no build step, no framework, no dependencies.
   One event listener on the document, one render pass per state change.

   The unit of work here is a release, matching the bot: a curator accepts or
   declines a single, an EP or an album, never a loose track. */

"use strict";

const KEY = new URLSearchParams(location.search).get("k") || "";

const T = {
  ru: {
    queue: "Очередь", catalogue: "Каталог", artists: "Артисты",
    playlists: "Плейлисты", station: "Станция",
    empty_queue: "Очередь пуста.", empty: "Пусто.", pick: "Выберите релиз слева.",
    publish: "Опубликовать релиз", decline: "Отклонить релиз",
    withdraw: "Снять с публикации", save: "Сохранить", undo: "Отменить",
    tags: "Теги", note: "Заметка куратора", checks: "Проверки",
    metadata: "Метаданные", tracks: "Треки", cover: "Обложка",
    title: "Название", artist: "Артист", album: "Релиз", year: "Год", kind: "Тип",
    add_tag: "добавить тег и Enter",
    note_ph: "Одно-два предложения. Слушатель видит это до нажатия «играть».",
    reason_ph: "Причина отказа — артист её увидит",
    search: "поиск по каталогу", search_artists: "поиск по артистам",
    bot_on: "бот работает", bot_off: "бот остановлен", bot_start: "Запустить",
    bot_stop: "Остановить", no_token: "нет токена — tonearm setup",
    approved: "Опубликовано", rejected: "Отклонено", saved: "Сохранено",
    hidden: "Снято", failed: "Не получилось", restored: "Возвращено в очередь",
    pending: "в очереди", catalogue_n: "в каталоге", artists_n: "артистов",
    unheard: "ни разу не показаны", listeners: "слушателей за неделю",
    plays_n: "прослушиваний за неделю", declined_n: "отклонено за неделю",
    published_n: "опубликовано за неделю", releases_n: "релизов",
    new_playlist: "Новый плейлист", playlist_title: "название плейлиста",
    publish_list: "Опубликовать", unpublish: "Скрыть", remove: "убрать",
    followers: "подписчиков", saves: "сохранений", shown: "показов",
    plays: "прослушиваний",
    drop_cover: "Перетащите картинку или нажмите",
    no_cover: "Без обложки публиковать нельзя",
    no_tracks: "В релизе нет треков",
    hint: "J/K листать · пробел играть · ⌘↵ опубликовать · ⌘⌫ отклонить · / поиск",
    kinds: { single: "сингл", ep: "EP", album: "альбом" },
    status: { pending: "ждёт", approved: "опубликован", rejected: "отклонён", hidden: "снят" }
  },
  en: {
    queue: "Queue", catalogue: "Catalogue", artists: "Artists",
    playlists: "Playlists", station: "Station",
    empty_queue: "The queue is empty.", empty: "Nothing here.",
    pick: "Choose a release on the left.",
    publish: "Publish release", decline: "Decline release",
    withdraw: "Withdraw", save: "Save", undo: "Undo",
    tags: "Tags", note: "Curator note", checks: "Checks",
    metadata: "Metadata", tracks: "Tracks", cover: "Artwork",
    title: "Title", artist: "Artist", album: "Release", year: "Year", kind: "Kind",
    add_tag: "add a tag, then Enter",
    note_ph: "One or two sentences. Listeners see this before they press play.",
    reason_ph: "Reason — the artist will see it",
    search: "search the catalogue", search_artists: "search artists",
    bot_on: "bot running", bot_off: "bot stopped", bot_start: "Start",
    bot_stop: "Stop", no_token: "no token — run tonearm setup",
    approved: "Published", rejected: "Declined", saved: "Saved",
    hidden: "Withdrawn", failed: "That did not work", restored: "Back in the queue",
    pending: "in the queue", catalogue_n: "in the catalogue", artists_n: "artists",
    unheard: "never shown", listeners: "listeners this week",
    plays_n: "plays this week", declined_n: "declined this week",
    published_n: "published this week", releases_n: "releases",
    new_playlist: "New playlist", playlist_title: "playlist name",
    publish_list: "Publish", unpublish: "Unpublish", remove: "remove",
    followers: "followers", saves: "saves", shown: "times shown",
    plays: "plays",
    drop_cover: "Drop an image here, or click",
    no_cover: "No artwork — it cannot be published",
    no_tracks: "This release has no tracks",
    hint: "J/K move · space play · ⌘↵ publish · ⌘⌫ decline · / search",
    kinds: { single: "single", ep: "EP", album: "album" },
    status: { pending: "waiting", approved: "published", rejected: "declined", hidden: "withdrawn" }
  }
};

let lang = "ru";
const t = (k) => (T[lang] && T[lang][k] !== undefined) ? T[lang][k] : (T.en[k] !== undefined ? T.en[k] : k);

const S = {
  view: "queue",
  state: null,
  queue: [], queueTotal: 0,
  release: null,
  catalogue: [], catalogueQuery: "", catalogueStatus: "approved",
  artists: [], artistQuery: "", artist: null,
  playlists: [], playlist: null,
  track: null,
  draftTags: [],
  busy: false,
  undo: null
};

/* ------------------------------------------------------------------ api */

async function api(path, body) {
  const url = path + (path.includes("?") ? "&" : "?") + "k=" + encodeURIComponent(KEY);
  const options = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

const media = (kind, id) => `/api/${kind}/${id}?k=${encodeURIComponent(KEY)}`;

/* --------------------------------------------------------------- helpers */

const esc = (value) => String(value === null || value === undefined ? "" : value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function hms(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const m = Math.floor(total / 60), s = total % 60;
  return m + ":" + String(s).padStart(2, "0");
}

let toastTimer = null;
function toast(message, actionLabel, onAction) {
  let element = document.querySelector(".toast");
  if (!element) {
    element = document.createElement("div");
    element.className = "toast";
    document.body.appendChild(element);
  }
  element.innerHTML = esc(message)
    + (actionLabel ? ` <button class="btn small" data-toast>${esc(actionLabel)}</button>` : "");
  element.classList.add("on");
  element.style.pointerEvents = actionLabel ? "auto" : "none";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("on"), actionLabel ? 8000 : 1800);
  element.onclick = (event) => {
    if (event.target.matches("[data-toast]") && onAction) {
      element.classList.remove("on");
      onAction();
    }
  };
}

const CHECK_LABELS = {
  low_bitrate: (v) => `битрейт ${v}k`,
  lossless_from_lossy: (v) => `lossless из lossy — срез ${Math.round(v / 100) / 10} кГц`,
  dull_top_end: (v) => `нет верха выше ${Math.round(v / 100) / 10} кГц`,
  clipping: (v) => `клиппинг ${v}%`,
  true_peak_over: (v) => `true peak +${v} dBTP`,
  very_loud: (v) => `${v} LUFS — очень громко`,
  very_quiet: (v) => `${v} LUFS — очень тихо`,
  over_compressed: (v) => `crest ${v}`,
  mono: () => "моно",
  unprobed: () => "не проанализирован"
};

function checksHtml(quality) {
  const keys = Object.keys(quality || {});
  if (!keys.length) return "";
  return keys.map((key) => {
    const label = CHECK_LABELS[key] ? CHECK_LABELS[key](quality[key]) : `${key} ${quality[key]}`;
    return `<span class="check">${esc(label)}</span>`;
  }).join("");
}

const kindLabel = (kind) => (t("kinds")[kind] || kind || "");

/* ------------------------------------------------------------- rendering */

function render() {
  const app = document.getElementById("app");
  app.className = "";
  app.innerHTML = `<div class="shell">${rail()}<div class="main">${main()}</div></div>`;
}

function rail() {
  const report = (S.state && S.state.report) || {};
  const bot = (S.state && S.state.bot) || {};
  const nav = [
    ["queue", t("queue"), report.pending || 0],
    ["catalogue", t("catalogue"), report.catalogue || 0],
    ["artists", t("artists"), report.artists || 0],
    ["playlists", t("playlists"), ""],
    ["station", t("station"), ""]
  ].map(([id, label, count]) =>
    `<button class="nav ${S.view === id ? "on" : ""}" data-view="${id}"><b>${esc(label)}</b><i>${esc(count)}</i></button>`
  ).join("");

  let botLine;
  if (!S.state || !S.state.has_token) {
    botLine = `<span><span class="dot err"></span>${esc(t("no_token"))}</span>`;
  } else if (bot.running) {
    botLine = `<span><span class="dot on"></span>${esc(t("bot_on"))}</span>
      <button class="btn small" data-bot="stop">${esc(t("bot_stop"))}</button>`;
  } else {
    botLine = `<span><span class="dot ${bot.error ? "err" : ""}"></span>${esc(bot.error || t("bot_off"))}</span>
      <button class="btn small" data-bot="start">${esc(t("bot_start"))}</button>`;
  }

  return `<div class="rail">
    <div class="wordmark">tonearm<small>${esc((S.state && S.state.station) || "")}</small></div>
    ${nav}
    <div class="rail-foot">${botLine}</div>
  </div>`;
}

function main() {
  if (S.view === "queue") return viewQueue();
  if (S.view === "catalogue") return viewCatalogue();
  if (S.view === "artists") return viewArtists();
  if (S.view === "playlists") return viewPlaylists();
  return viewStation();
}

function releaseRow(item, selected) {
  const bits = [kindLabel(item.kind)];
  if (item.year) bits.push(item.year);
  if (item.total) bits.push(item.total);
  return `<div class="row ${selected ? "on" : ""}" data-release="${item.id}">
    <div>
      <div class="t">${esc(item.title)}</div>
      <div class="a">${esc(item.artist)} · ${esc(bits.join(" · "))}</div>
    </div>
    <div class="d">${(item.blockers && item.blockers.length) ? `<span class="flag">!</span>` : ""}</div>
  </div>`;
}

function trackRow(item, selected) {
  return `<div class="row ${selected ? "on" : ""}" data-track="${item.id}">
    <div>
      <div class="t">${esc(item.title)}</div>
      <div class="a">${esc(item.artist)}</div>
    </div>
    <div class="d">${hms(item.duration)}</div>
  </div>`;
}

function viewQueue() {
  const list = S.queue.length
    ? S.queue.map((item) => releaseRow(item, S.release && S.release.id === item.id)).join("")
    : `<div class="empty">${esc(t("empty_queue"))}</div>`;
  return `<div class="head">
      <h1>${esc(t("queue"))}</h1><span class="count">${S.queueTotal}</span>
    </div>
    <div class="body">
      <div class="pane list">${list}</div>
      <div class="pane detail">${S.release ? releaseCard(S.release, true) : `<div class="empty">${esc(t("pick"))}</div>`}</div>
    </div>`;
}

function coverHtml(release) {
  const src = release.tracks && release.tracks.length && release.has_cover
    ? media("cover", release.tracks[0].id)
    : "";
  return `<div class="coverbox ${src ? "" : "missing"}" data-coverdrop="${release.id}">
    ${src ? `<img class="cover-large" src="${src}" alt="">` : ""}
    <span class="coverhint" ${src ? "hidden" : ""}>${esc(t("drop_cover"))}</span>
    <input type="file" accept="image/*" data-coverinput hidden>
  </div>`;
}

function releaseCard(release, queued) {
  const bits = [kindLabel(release.kind)];
  if (release.year) bits.push(release.year);
  if (release.duration) bits.push(hms(release.duration));
  bits.push(t("status")[release.status] || release.status);

  const blockers = (release.blockers || []).map(
    (b) => `<div class="blocker">${esc(t(b === "no_cover" ? "no_cover" : "no_tracks"))}</div>`
  ).join("");

  const tracks = (release.tracks || []).map((track) => `
    <div class="tk" data-tk="${track.id}">
      <div class="tk-head">
        <b>${track.track_no ? track.track_no + " · " : ""}${esc(track.title)}</b>
        <span>${hms(track.duration)}</span>
      </div>
      <div class="meta">${esc(track.technical || "")}</div>
      <div class="checks">${checksHtml(track.quality)}</div>
      <audio controls preload="none" src="${media("audio", track.id)}"></audio>
      <div class="tags">${(track.tags || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}</div>
    </div>`).join("");

  const actions = queued
    ? `<button class="btn primary" data-act="approve_release" ${release.blockers.length ? "disabled" : ""}>${esc(t("publish"))}</button>
       <button class="btn danger" data-act="reject_release">${esc(t("decline"))}</button>
       <button class="btn" data-act="save_release">${esc(t("save"))}</button>`
    : `<button class="btn" data-act="save_release">${esc(t("save"))}</button>`;

  return `<div class="card" data-release-card="${release.id}">
    ${coverHtml(release)}
    <h2>${esc(release.title)}</h2>
    <div class="by">${esc(release.artist)}</div>
    <div class="meta">${esc(bits.join(" · "))}</div>
    ${blockers}

    <div class="section"><label>${esc(t("note"))}</label>
      <textarea rows="3" data-rnote placeholder="${esc(t("note_ph"))}">${esc(release.note)}</textarea>
    </div>

    <div class="section"><label>${esc(t("metadata"))}</label>
      <div class="field"><span>${esc(t("album"))}</span><input type="text" data-rf="title" value="${esc(release.title)}"></div>
      <div class="field"><span>${esc(t("year"))}</span><input type="text" data-rf="year" value="${esc(release.year || "")}"></div>
    </div>

    <div class="section"><label>${esc(t("tracks"))} · ${(release.tracks || []).length}</label>
      ${tracks || `<div class="empty">${esc(t("empty"))}</div>`}
    </div>

    ${queued ? `<div class="section"><label>${esc(t("decline"))}</label>
      <input type="text" data-reason placeholder="${esc(t("reason_ph"))}"></div>` : ""}

    <div class="actions">${actions}</div>
    <div class="hint">${esc(t("hint"))}</div>
  </div>`;
}

function viewCatalogue() {
  const list = S.catalogue.length
    ? S.catalogue.map((item) => trackRow(item, S.track && S.track.id === item.id)).join("")
    : `<div class="empty">${esc(t("empty"))}</div>`;
  const tabs = ["approved", "rejected", "hidden"].map((status) =>
    `<button class="btn small ${S.catalogueStatus === status ? "primary" : ""}" data-status="${status}">${esc(t("status")[status])}</button>`
  ).join(" ");
  return `<div class="head">
      <h1>${esc(t("catalogue"))}</h1>
      <input class="search" data-search="catalogue" placeholder="${esc(t("search"))}" value="${esc(S.catalogueQuery)}">
      <span class="spacer"></span>${tabs}
    </div>
    <div class="body">
      <div class="pane list">${list}</div>
      <div class="pane detail">${S.track ? trackCard(S.track) : `<div class="empty">${esc(t("pick"))}</div>`}</div>
    </div>`;
}

function trackCard(track) {
  const chips = S.draftTags.map((tag) =>
    `<span class="tag" data-untag="${esc(tag)}">${esc(tag)} ×</span>`).join("");
  const suggested = ((S.state && S.state.tags) || [])
    .filter((tag) => !S.draftTags.includes(tag)).slice(0, 14)
    .map((tag) => `<span class="tag pick" data-tag="${esc(tag)}">${esc(tag)}</span>`).join("");
  return `<div class="card" data-card="${track.id}">
    <h2>${esc(track.title)}</h2>
    <div class="by">${esc(track.artist)} · ${hms(track.duration)}</div>
    <div class="meta">${esc([track.technical, track.album, track.year].filter(Boolean).join(" · "))}</div>
    <div class="meta">${track.plays} ${esc(t("plays"))} · ${track.likes} ${esc(t("saves"))} · ${track.exposures} ${esc(t("shown"))}</div>
    <audio controls preload="none" src="${media("audio", track.id)}"></audio>

    <div class="section"><label>${esc(t("checks"))}</label>
      <div class="checks">${checksHtml(track.quality) || `<span class="check ok">—</span>`}</div>
    </div>
    <div class="section"><label>${esc(t("tags"))}</label>
      <div class="tags">${chips || `<span class="check ok">—</span>`}</div>
      <input type="text" data-newtag placeholder="${esc(t("add_tag"))}">
      <div class="tags" style="margin-top:8px">${suggested}</div>
    </div>
    <div class="section"><label>${esc(t("note"))}</label>
      <textarea rows="3" data-note placeholder="${esc(t("note_ph"))}">${esc(track.note)}</textarea>
    </div>
    <div class="section"><label>${esc(t("metadata"))}</label>
      <div class="field"><span>${esc(t("title"))}</span><input type="text" data-f="title" value="${esc(track.title)}"></div>
      <div class="field"><span>${esc(t("artist"))}</span><input type="text" data-f="artist" value="${esc(track.artist)}"></div>
    </div>
    <div class="actions">
      <button class="btn" data-act="save">${esc(t("save"))}</button>
      ${track.status === "approved" ? `<button class="btn danger" data-act="hide">${esc(t("withdraw"))}</button>` : ""}
    </div>
  </div>`;
}

function viewArtists() {
  const list = S.artists.length
    ? S.artists.map((a) => `<div class="row ${S.artist && S.artist.id === a.id ? "on" : ""}" data-artist="${a.id}">
        <div><div class="t">${esc(a.name)}</div></div><div class="d">${a.tracks}</div></div>`).join("")
    : `<div class="empty">${esc(t("empty"))}</div>`;
  let detail = `<div class="empty">${esc(t("pick"))}</div>`;
  if (S.artist) {
    const s = S.artist.stats || {};
    detail = `<div class="card">
      <h2>${esc(S.artist.name)}</h2>
      <div class="meta">${s.tracks || 0} · ${s.plays || 0} ${esc(t("plays"))} · ${s.likes || 0} ${esc(t("saves"))} · ${s.followers || 0} ${esc(t("followers"))}</div>
      <div class="section">${S.artist.tracks.map((item) => trackRow(item, false)).join("")}</div>
    </div>`;
  }
  return `<div class="head">
      <h1>${esc(t("artists"))}</h1>
      <input class="search" data-search="artists" placeholder="${esc(t("search_artists"))}" value="${esc(S.artistQuery)}">
    </div>
    <div class="body"><div class="pane list">${list}</div><div class="pane detail">${detail}</div></div>`;
}

function viewPlaylists() {
  const list = S.playlists.length
    ? S.playlists.map((p) => `<div class="row ${S.playlist && S.playlist.id === p.id ? "on" : ""}" data-playlist="${p.id}">
        <div><div class="t">${esc(p.title)}</div><div class="a">${p.published ? "" : "—"} ${esc(p.description)}</div></div>
        <div class="d">${p.count}</div></div>`).join("")
    : `<div class="empty">${esc(t("empty"))}</div>`;
  let detail = `<div class="empty">${esc(t("pick"))}</div>`;
  if (S.playlist) {
    detail = `<div class="card">
      <h2>${esc(S.playlist.title)}</h2>
      <div class="note">${esc(S.playlist.description)}</div>
      <div class="actions">
        <button class="btn ${S.playlist.published ? "" : "primary"}" data-listpub="${S.playlist.published ? 0 : 1}">
          ${esc(S.playlist.published ? t("unpublish") : t("publish_list"))}</button>
      </div>
      <div class="section">${S.playlist.tracks.map((item) =>
        `<div class="row"><div><div class="t">${esc(item.title)}</div><div class="a">${esc(item.artist)}</div></div>
         <div class="d"><button class="btn small" data-listdel="${item.id}">${esc(t("remove"))}</button></div></div>`).join("")
        || `<div class="empty">${esc(t("empty"))}</div>`}</div>
    </div>`;
  }
  return `<div class="head">
      <h1>${esc(t("playlists"))}</h1>
      <input class="search" data-newlist placeholder="${esc(t("playlist_title"))}">
      <span class="spacer"></span>
      <button class="btn small" data-act="newlist">${esc(t("new_playlist"))}</button>
    </div>
    <div class="body"><div class="pane list">${list}</div><div class="pane detail">${detail}</div></div>`;
}

function viewStation() {
  const r = (S.state && S.state.report) || {};
  const cells = [
    [r.pending, t("pending")], [r.releases, t("releases_n")],
    [r.catalogue, t("catalogue_n")], [r.artists, t("artists_n")],
    [r.unheard, t("unheard")], [r.listeners, t("listeners")],
    [r.plays, t("plays_n")], [r.approved, t("published_n")], [r.rejected, t("declined_n")]
  ].map(([value, label]) => `<div class="stat"><b>${esc(value || 0)}</b><span>${esc(label)}</span></div>`).join("");
  const curators = ((S.state && S.state.curators) || []).join(", ") || "—";
  return `<div class="head"><h1>${esc(t("station"))}</h1></div>
    <div class="pane full">
      <div class="stats">${cells}</div>
      <div class="section" style="max-width:720px">
        <label>${lang === "ru" ? "Кураторы" : "Curators"}</label>
        <div class="meta">${esc(curators)}</div>
        <div class="meta">${lang === "ru"
          ? "Добавить: <code>tonearm curator add &lt;telegram id&gt;</code> · свой id — команда /whoami боту"
          : "Add with <code>tonearm curator add &lt;telegram id&gt;</code> · get yours by sending /whoami to the bot"}</div>
      </div>
    </div>`;
}

/* ---------------------------------------------------------------- loading */

async function refreshState() {
  S.state = await api("/api/state");
  if (S.state.lang) lang = S.state.lang;
}

async function loadQueue() {
  const data = await api("/api/queue");
  S.queue = data.items;
  S.queueTotal = data.total;
  if (S.release && !S.queue.some((item) => item.id === S.release.id)) S.release = null;
  if (!S.release && S.queue.length) await selectRelease(S.queue[0].id, false);
}

async function selectRelease(id, redraw = true) {
  S.release = await api(`/api/release/${id}`);
  if (redraw) render();
}

async function loadCatalogue() {
  const data = await api(`/api/catalogue?q=${encodeURIComponent(S.catalogueQuery)}&status=${S.catalogueStatus}`);
  S.catalogue = data.items;
}

async function loadArtists() {
  S.artists = (await api(`/api/artists?q=${encodeURIComponent(S.artistQuery)}`)).items;
}

async function loadPlaylists() {
  S.playlists = (await api("/api/playlists")).items;
}

async function selectTrack(id, redraw = true) {
  S.track = await api(`/api/track/${id}`);
  S.draftTags = (S.track.tags || []).slice();
  if (redraw) render();
}

async function go(view) {
  S.view = view;
  S.track = null;
  S.artist = null;
  S.playlist = null;
  try {
    if (view === "queue") await loadQueue();
    else if (view === "catalogue") await loadCatalogue();
    else if (view === "artists") await loadArtists();
    else if (view === "playlists") await loadPlaylists();
    else await refreshState();
  } catch (error) {
    toast(error.message);
  }
  render();
}

/* --------------------------------------------------------------- editing */

function fieldValue(selector) {
  const el = document.querySelector(selector);
  return el ? el.value.trim() : "";
}

async function saveRelease(silent) {
  if (!S.release) return;
  const payload = {
    title: fieldValue('[data-rf="title"]'),
    note: fieldValue("[data-rnote]")
  };
  const year = fieldValue('[data-rf="year"]');
  if (year) payload.year = year;
  const result = await api(`/api/edit_release/${S.release.id}`, payload);
  if (result.release) S.release = result.release;
  if (!silent) toast(t("saved"));
}

async function actRelease(action) {
  if (S.busy || !S.release) return;
  S.busy = true;
  const release = S.release;
  try {
    if (action === "approve_release") {
      await saveRelease(true);
      const result = await api(`/api/approve_release/${release.id}`, { note: fieldValue("[data-rnote]") });
      if (!result.ok) {
        toast(t(result.error === "no_cover" ? "no_cover" : "failed"));
        S.busy = false;
        return;
      }
      offerUndo(release, t("approved"));
    } else if (action === "reject_release") {
      await api(`/api/reject_release/${release.id}`, { reason: fieldValue("[data-reason]") });
      offerUndo(release, t("rejected"));
    } else if (action === "save_release") {
      await saveRelease(false);
      S.busy = false;
      render();
      return;
    }
  } catch (error) {
    toast(t("failed") + ": " + error.message);
    S.busy = false;
    return;
  }
  S.busy = false;
  try {
    await refreshState();
    S.release = null;
    await loadQueue();
  } catch (error) {
    toast(error.message);
  }
  render();
}

function offerUndo(release, message) {
  /* Every moderation decision is reversible, and the offer to reverse it is
     the reason a fast workflow is safe to hand someone. */
  toast(message, t("undo"), async () => {
    try {
      for (const track of release.tracks || []) {
        await api(`/api/restore/${track.id}`, {});
      }
      await api(`/api/edit_release/${release.id}`, {});
      await refreshState();
      await loadQueue();
      toast(t("restored"));
      render();
    } catch (error) {
      toast(error.message);
    }
  });
}

async function actTrack(action) {
  if (S.busy || !S.track) return;
  S.busy = true;
  try {
    if (action === "save") {
      const payload = {
        title: fieldValue('[data-f="title"]'),
        note: fieldValue("[data-note]"),
        tags: S.draftTags.slice()
      };
      const artist = fieldValue('[data-f="artist"]');
      if (artist && artist !== S.track.artist) payload.artist = artist;
      const result = await api(`/api/edit/${S.track.id}`, payload);
      if (result.track) S.track = result.track;
      toast(t("saved"));
    } else if (action === "hide") {
      await api(`/api/hide/${S.track.id}`, {});
      toast(t("hidden"));
      await loadCatalogue();
    }
  } catch (error) {
    toast(t("failed") + ": " + error.message);
  }
  S.busy = false;
  render();
}

async function uploadCover(releaseId, file) {
  if (!file) return;
  if (file.size > 3 * 1024 * 1024) {
    toast(t("failed") + ": > 3 MB");
    return;
  }
  const buffer = await file.arrayBuffer();
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  try {
    const result = await api(`/api/setcover/${releaseId}`, { data: btoa(binary) });
    if (!result.ok) {
      toast(t("failed") + ": " + (result.error || ""));
      return;
    }
    S.release = result.release;
    await loadQueue();
    toast(t("saved"));
    render();
  } catch (error) {
    toast(error.message);
  }
}

function move(delta) {
  if (S.view === "queue") {
    if (!S.queue.length) return;
    const index = S.release ? S.queue.findIndex((i) => i.id === S.release.id) : -1;
    const next = Math.max(0, Math.min(S.queue.length - 1, index + delta));
    if (next !== index || index < 0) selectRelease(S.queue[next].id).catch((e) => toast(e.message));
    return;
  }
  if (S.view === "catalogue" && S.catalogue.length) {
    const index = S.track ? S.catalogue.findIndex((i) => i.id === S.track.id) : -1;
    const next = Math.max(0, Math.min(S.catalogue.length - 1, index + delta));
    if (next !== index || index < 0) selectTrack(S.catalogue[next].id).catch((e) => toast(e.message));
  }
}

/* ---------------------------------------------------------------- events */

/* A stored cover id can outlive the file Telegram holds. Showing the drop
   placeholder is better than a broken image, and the capture phase is required
   because image load errors do not bubble. */
document.addEventListener("error", (event) => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement) || !image.matches(".cover-large")) return;
  const box = image.closest(".coverbox");
  image.remove();
  if (box) {
    box.classList.add("missing");
    const hint = box.querySelector(".coverhint");
    if (hint) hint.hidden = false;
  }
}, true);

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-coverinput]")) {
    const box = event.target.closest("[data-coverdrop]");
    uploadCover(Number(box.dataset.coverdrop), event.target.files[0]);
  }
});

document.addEventListener("dragover", (event) => {
  if (event.target.closest("[data-coverdrop]")) event.preventDefault();
});

document.addEventListener("drop", (event) => {
  const box = event.target.closest("[data-coverdrop]");
  if (!box) return;
  event.preventDefault();
  uploadCover(Number(box.dataset.coverdrop), event.dataTransfer.files[0]);
});

document.addEventListener("click", async (event) => {
  const target = event.target.closest(
    "[data-view],[data-release],[data-track],[data-artist],[data-playlist]," +
    "[data-act],[data-tag],[data-untag],[data-bot],[data-status],[data-listdel]," +
    "[data-listpub],[data-coverdrop]"
  );
  if (!target) return;
  try {
    if (target.dataset.view !== undefined) return void go(target.dataset.view);
    if (target.dataset.release) return void selectRelease(Number(target.dataset.release));
    if (target.dataset.track) return void selectTrack(Number(target.dataset.track));
    if (target.dataset.coverdrop && !event.target.matches("[data-coverinput]")) {
      return void target.querySelector("[data-coverinput]").click();
    }
    if (target.dataset.bot) {
      S.state.bot = await api(`/api/bot/${target.dataset.bot}`, {});
      return void render();
    }
    if (target.dataset.status) {
      S.catalogueStatus = target.dataset.status;
      S.track = null;
      await loadCatalogue();
      return void render();
    }
    if (target.dataset.tag) {
      if (!S.draftTags.includes(target.dataset.tag)) S.draftTags.push(target.dataset.tag);
      return void render();
    }
    if (target.dataset.untag) {
      S.draftTags = S.draftTags.filter((tag) => tag !== target.dataset.untag);
      return void render();
    }
    if (target.dataset.artist) {
      S.artist = await api(`/api/artist/${target.dataset.artist}`);
      return void render();
    }
    if (target.dataset.playlist) {
      S.playlist = await api(`/api/playlist/${target.dataset.playlist}`);
      return void render();
    }
    if (target.dataset.listdel && S.playlist) {
      S.playlist = (await api(`/api/playlist_edit/${S.playlist.id}`, { remove: Number(target.dataset.listdel) })).playlist;
      return void render();
    }
    if (target.dataset.listpub && S.playlist) {
      S.playlist = (await api(`/api/playlist_edit/${S.playlist.id}`, { published: target.dataset.listpub === "1" })).playlist;
      await loadPlaylists();
      return void render();
    }
    const act = target.dataset.act;
    if (act === "newlist") {
      const input = document.querySelector("[data-newlist]");
      const title = input ? input.value.trim() : "";
      if (!title) return;
      await api("/api/playlist_new", { title });
      await loadPlaylists();
      return void render();
    }
    if (act === "save" || act === "hide") return void actTrack(act);
    if (act) return void actRelease(act);
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("keydown", async (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  const command = event.metaKey || event.ctrlKey;

  /* Publishing and declining need a modifier. A bare letter is far too easy to
     hit by accident, and an accidental decline empties a queue three rows at a
     time before anyone notices. */
  if (command && S.view === "queue" && S.release) {
    if (event.key === "Enter") { event.preventDefault(); actRelease("approve_release"); return; }
    if (event.key === "Backspace") { event.preventDefault(); actRelease("reject_release"); return; }
  }

  if (typing) {
    if (event.key === "Escape") event.target.blur();
    if (event.key === "Enter" && event.target.matches("[data-newtag]")) {
      event.preventDefault();
      const value = event.target.value.trim().toLowerCase();
      if (value && !S.draftTags.includes(value)) S.draftTags.push(value);
      render();
      const next = document.querySelector("[data-newtag]");
      if (next) next.focus();
      return;
    }
    if (event.key === "Enter" && event.target.matches('[data-search="catalogue"]')) {
      S.catalogueQuery = event.target.value;
      S.track = null;
      try { await loadCatalogue(); } catch (error) { toast(error.message); }
      render();
      const field = document.querySelector('[data-search="catalogue"]');
      if (field) { field.value = S.catalogueQuery; field.focus(); }
    }
    if (event.key === "Enter" && event.target.matches('[data-search="artists"]')) {
      S.artistQuery = event.target.value;
      try { await loadArtists(); } catch (error) { toast(error.message); }
      render();
      const field = document.querySelector('[data-search="artists"]');
      if (field) { field.value = S.artistQuery; field.focus(); }
    }
    return;
  }

  if (event.key === "/") {
    event.preventDefault();
    const field = document.querySelector(".search");
    if (field) field.focus();
    return;
  }
  if (event.key === "j" || event.key === "ArrowDown") { event.preventDefault(); move(1); return; }
  if (event.key === "k" || event.key === "ArrowUp") { event.preventDefault(); move(-1); return; }
  if (event.key === " ") {
    const player = document.querySelector("audio");
    if (player) { event.preventDefault(); player.paused ? player.play() : player.pause(); }
  }
});

/* ------------------------------------------------------------------ boot */

(async function start() {
  try {
    await refreshState();
    await loadQueue();
  } catch (error) {
    document.getElementById("app").textContent = "не удалось подключиться: " + error.message;
    return;
  }
  render();

  // Background refresh. Never redraws while the curator is typing or
  // listening: a re-render would drop the caret and restart the player.
  setInterval(async () => {
    if (S.busy) return;
    const active = document.activeElement;
    if (active && /^(INPUT|TEXTAREA)$/.test(active.tagName)) return;
    const playing = [...document.querySelectorAll("audio")].some((a) => !a.paused);
    if (playing) return;
    try {
      const before = JSON.stringify(S.state && S.state.report);
      await refreshState();
      if (S.view === "queue") await loadQueue();
      if (JSON.stringify(S.state.report) !== before) render();
    } catch (_) { /* a poll failure is not worth interrupting the desk */ }
  }, 15000);
})();
