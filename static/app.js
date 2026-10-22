/* MCH PDF Organizer - front end
 *
 * Step 1  files are uploaded one at a time and converted on the server
 * Step 2  pages (thumbnails rendered locally with pdf.js) are arranged in `state.seq`
 * Step 3  the server builds the real combined PDF from `state.seq`; the preview shows
 *         that exact file, and Download serves it.
 *
 * The arrangement is kept in sessionStorage so a refresh doesn't lose the work while the
 * server still has the files.
 */
(function () {
  "use strict";

  const STORE_KEY = "mch-pdf-organizer-v1";
  const SUPPORTED = { ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image",
                      ".docx": "word", ".doc": "word", ".xlsx": "excel", ".xls": "excel" };
  // File type -> colour class (ft-<type>, colours in style.css) and badge label.
  // The colour shows the FORMAT only; it never changes with upload/conversion status.
  const FILE_TYPE_BY_EXT = {
    ".xls": "excel", ".xlsx": "excel", ".xlsm": "excel", ".xltx": "excel",
    ".doc": "word", ".docx": "word", ".docm": "word", ".dotx": "word",
    ".pdf": "pdf",
    ".gsheet": "sheets",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".bmp": "image", ".tif": "image", ".tiff": "image", ".heic": "image", ".heif": "image",
    ".avif": "image", ".svg": "image",
    ".ppt": "ppt", ".pptx": "ppt", ".pptm": "ppt", ".pps": "ppt", ".ppsx": "ppt",
  };
  const FILE_TYPE_LABEL = { excel: "EXCEL", word: "WORD", pdf: "PDF", sheets: "SHEETS",
                            image: "IMAGE", ppt: "PPT" };

  function fileType(name, mime) {
    const byExt = FILE_TYPE_BY_EXT[extOf(name || "")];      // extOf lower-cases: .PDF == .pdf
    if (byExt) return byExt;
    if (mime === "application/vnd.google-apps.spreadsheet") return "sheets";
    if (mime && mime.startsWith("image/")) return "image";
    return "other";
  }

  function fileTypeLabel(f) {
    return FILE_TYPE_LABEL[f.ftype] || (f.ext ? f.ext.slice(1).toUpperCase() : "FILE");
  }
  const DEFAULT_NAME = "MCH_Combined_Document";
  const ICON = {
    rotl: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v6h6"/></svg>',
    rotr: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>',
    out: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/></svg>',
    back: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/></svg>',
  };

  // Where the server API lives. Empty = same site (Render, local). On the Vercel-hosted
  // page this is the Render backend, set by the server in <body data-api-base="...">.
  const API = (document.body.dataset.apiBase || "").replace(/\/+$/, "");

  if (window.pdfjsLib) {
    pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.js";
  }

  const $ = (id) => document.getElementById(id);
  const el = {
    stepBtns: [1, 2, 3].map(n => $("step-btn-" + n)),
    panels: { 1: $("panel-upload"), 2: $("panel-organize"), 3: $("panel-export") },
    dropzone: $("dropzone"), fileInput: $("file-input"), btnChoose: $("btn-choose"),
    btnAddMore: $("btn-add-more"), btnAddMore2: $("btn-add-more-2"),
    fileBlock: $("file-block"), fileBody: $("file-table-body"),
    uploadSummary: $("upload-summary"), btnToOrganize: $("btn-to-organize"),
    docList: $("doc-list"), grid: $("page-grid"), gridEmpty: $("grid-empty"),
    organizeSummary: $("organize-summary"), btnToExport: $("btn-to-export"),
    btnBackUpload: $("btn-back-upload"), btnIncludeAll: $("btn-include-all"), btnResetOrder: $("btn-reset-order"),
    building: $("building"), exportReady: $("export-ready"),
    outPages: $("out-pages"), outDocs: $("out-docs"), outSize: $("out-size"),
    filename: $("output-filename"), btnDownload: $("btn-download"),
    btnBackOrganize: $("btn-back-organize"), btnStartOver: $("btn-start-over"),
    preview: $("preview"), sequenceList: $("sequence-list"),
    errorBanner: $("error-banner"), errorMessage: $("error-message"), errorClose: $("error-close"),
    confirmDialog: $("confirm-dialog"), confirmTitle: $("confirm-title"), confirmText: $("confirm-text"),
    confirmOk: $("confirm-ok"), confirmCancel: $("confirm-cancel"),
    moveDialog: $("move-dialog"), moveText: $("move-text"), moveInput: $("move-input"),
    moveForm: $("move-form"), moveCancel: $("move-cancel"),
  };

  let state = freshState();
  let maxUploadMb = 50;
  let uploading = false;
  let nextCid = 1;

  function freshState() {
    return { sid: null, step: 1, files: [], seq: [], customized: false,
             imageMode: "a4", filename: DEFAULT_NAME, output: null };
  }

  // ------------------------------------------------------------------ helpers

  class ApiError extends Error {
    constructor(status, code, message) { super(message); this.status = status; this.code = code; }
  }

  async function api(method, path, body) {
    let resp;
    try {
      resp = await fetch(path, {
        method, headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined, cache: "no-store",
      });
    } catch (e) {
      throw new ApiError(0, "network", "The server couldn't be reached. Check your connection and try again.");
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { /* not JSON */ }
    if (!resp.ok) {
      const err = (data && data.error) || {};
      throw new ApiError(resp.status, err.code || "http", err.message || `Request failed (${resp.status}).`);
    }
    return data;
  }

  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0) + " MB";
  }

  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function cleanFilename(raw) {
    let n = (raw || "").trim();
    if (n.toLowerCase().endsWith(".pdf")) n = n.slice(0, -4);
    n = n.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "").replace(/^[\s.]+|[\s.]+$/g, "");
    return n.slice(0, 150) || DEFAULT_NAME;
  }

  function showError(msg) {
    el.errorMessage.textContent = msg;
    el.errorBanner.hidden = false;
    el.errorBanner.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function clearError() { el.errorBanner.hidden = true; }

  function handleApiError(e) {
    if (e && e.code === "session_expired") {
      showError(e.message + " (Your arrangement can't be restored because the server no longer has the files.)");
      resetAll(true);
      return;
    }
    showError(e && e.message ? e.message : "Something went wrong.");
  }

  function fileByCid(cid) { return state.files.find(f => f.cid === cid); }
  function readyFiles() { return state.files.filter(f => f.status === "ready"); }
  function includedPages() { return state.seq.filter(p => !p.excluded && fileByCid(p.file)); }
  function busy() { return state.files.some(f => ["queued", "uploading", "converting"].includes(f.status)); }

  function confirmBox(title, text, okLabel) {
    return new Promise(resolve => {
      el.confirmTitle.textContent = title;
      el.confirmText.textContent = text;
      el.confirmOk.textContent = okLabel || "Continue";
      if (typeof el.confirmDialog.showModal !== "function") { resolve(window.confirm(title + "\n\n" + text)); return; }
      const done = (v) => { el.confirmDialog.close(); cleanup(); resolve(v); };
      const ok = () => done(true), cancel = () => done(false);
      const onClose = () => { cleanup(); resolve(false); };
      function cleanup() {
        el.confirmOk.removeEventListener("click", ok);
        el.confirmCancel.removeEventListener("click", cancel);
        el.confirmDialog.removeEventListener("cancel", onClose);
      }
      el.confirmOk.addEventListener("click", ok);
      el.confirmCancel.addEventListener("click", cancel);
      el.confirmDialog.addEventListener("cancel", onClose);
      el.confirmDialog.showModal();
    });
  }

  // ------------------------------------------------------------------ session & persistence

  async function ensureSession() {
    if (state.sid) return state.sid;
    const data = await api("POST", API + "/api/sessions");
    state.sid = data.session;
    maxUploadMb = data.max_upload_mb || maxUploadMb;
    save();
    return state.sid;
  }

  let restored = false;                    // don't overwrite saved work before restore() has read it
  function save() {
    if (!restored) return;
    try {
      const files = state.files.filter(f => f.status === "ready").map(f => {
        const c = Object.assign({}, f); delete c.file; return c;
      });
      sessionStorage.setItem(STORE_KEY, JSON.stringify({
        sid: state.sid, step: state.step, files, seq: state.seq, customized: state.customized,
        imageMode: state.imageMode, filename: state.filename,
      }));
    } catch (e) { /* storage unavailable - nothing to do */ }
  }

  async function restore() {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null"); } catch (e) { saved = null; }
    if (!saved || !saved.sid) return;
    let server;
    try { server = await api("GET", `${API}/api/sessions/${saved.sid}`); }
    catch (e) { try { sessionStorage.removeItem(STORE_KEY); } catch (_) {} return; }
    const onServer = new Map(server.files.map(m => [m.id, m]));
    state.sid = saved.sid;
    state.imageMode = saved.imageMode || "a4";
    state.filename = saved.filename || DEFAULT_NAME;
    state.customized = !!saved.customized;
    state.files = (saved.files || []).filter(f => onServer.has(f.id) && onServer.get(f.id).version === f.version);
    state.files.forEach(f => { f.ftype = fileType(f.name); delete f.color; });   // older saves had per-document colours
    const cids = new Set(state.files.map(f => f.cid));
    state.seq = (saved.seq || []).filter(p => cids.has(p.file));
    nextCid = Math.max(0, ...state.files.map(f => f.cid)) + 1;
    const step = state.files.length ? Math.min(saved.step || 1, includedPages().length ? 3 : 2) : 1;
    if (step === 3) goStep(3); else goStep(step);
  }

  // ------------------------------------------------------------------ adding files

  function addFiles(list) {
    clearError();
    const incoming = Array.from(list || []);
    if (!incoming.length) return;
    for (const file of incoming) {
      const ext = extOf(file.name);
      const entry = {
        cid: nextCid++, id: null, name: file.name, ext, kind: SUPPORTED[ext] || null, size: file.size,
        status: "queued", progress: 0, error: null, pages: 0, sizes: [], sheets: null, version: 0,
        ftype: fileType(file.name, file.type), file,
      };
      if (!entry.kind) {
        entry.status = "error";
        entry.error = "This file type isn't supported. Use Excel, Word, PDF, PNG or JPG files.";
      } else if (file.size === 0) {
        entry.status = "error"; entry.error = "The file is empty (0 bytes).";
      } else if (file.size > maxUploadMb * 1024 * 1024) {
        entry.status = "error"; entry.error = `The file is larger than the ${maxUploadMb} MB limit.`;
      }
      state.files.push(entry);
    }
    render();
    pump();
  }

  async function pump() {
    if (uploading) return;
    const entry = state.files.find(f => f.status === "queued");
    if (!entry) { render(); return; }
    uploading = true;
    try {
      await ensureSession();
      entry.status = "uploading"; entry.progress = 0; render();
      const meta = await uploadOne(entry);
      if (!state.files.includes(entry)) {          // removed while converting
        api("DELETE", `${API}/api/sessions/${state.sid}/files/${meta.id}`).catch(() => {});
      } else {
        applyMeta(entry, meta);
      }
    } catch (e) {
      if (e && e.code === "session_expired") { uploading = false; handleApiError(e); return; }
      entry.status = "error";
      entry.error = (e && e.message) || "Upload failed.";
    } finally {
      uploading = false;
      entry.file = null;
    }
    render();
    pump();
  }

  function uploadOne(entry) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append("file", entry.file, entry.name);
      fd.append("image_mode", state.imageMode);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API}/api/sessions/${state.sid}/files`);
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable) { entry.progress = Math.round(ev.loaded / ev.total * 100); renderFileStatus(entry); }
      };
      xhr.upload.onload = () => { entry.status = "converting"; renderFileStatus(entry); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
        else {
          const err = (data && data.error) || {};
          reject(new ApiError(xhr.status, err.code || "http",
            err.message || (xhr.status === 413 ? `The file is larger than the ${maxUploadMb} MB limit.` : `Upload failed (${xhr.status}).`)));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "network", "The upload was interrupted. Check your connection and try again."));
      xhr.send(fd);
    });
  }

  function applyMeta(entry, meta) {
    const firstTime = !entry.id;              // a re-conversion keeps the same server id
    entry.id = meta.id; entry.kind = meta.kind; entry.size = meta.size; entry.sha = meta.sha;
    entry.pages = meta.pages; entry.sizes = meta.sizes || []; entry.sheets = meta.sheets || null;
    entry.version = meta.version; entry.status = "ready"; entry.error = null; entry.notes = meta.notes || [];
    entry.imageMode = meta.image_mode;
    const fresh = [];
    for (let i = 0; i < meta.pages; i++) {
      fresh.push({ key: `${entry.cid}-${meta.version}-${i}`, file: entry.cid, page: i, rotate: 0, excluded: false });
    }
    if (firstTime) {
      state.seq.push(...fresh);                 // new documents go at the end; arrangement kept
    } else {
      // Re-converted (sheet choice / image size): its pages are replaced where they were
      const at = state.seq.findIndex(p => p.file === entry.cid);
      state.seq = state.seq.filter(p => p.file !== entry.cid);
      state.seq.splice(at < 0 ? state.seq.length : at, 0, ...fresh);
    }
    state.output = null;
  }

  async function removeFile(cid) {
    const entry = fileByCid(cid);
    if (!entry) return;
    state.files = state.files.filter(f => f !== entry);
    state.seq = state.seq.filter(p => p.file !== cid);
    state.output = null;
    if (entry.id && state.sid) api("DELETE", `${API}/api/sessions/${state.sid}/files/${entry.id}`).catch(() => {});
    render();
  }

  async function reconvert(entry, body) {
    clearError();
    entry.status = "converting"; render();
    try {
      const meta = await api("POST", `${API}/api/sessions/${state.sid}/files/${entry.id}/reconvert`, body);
      applyMeta(entry, meta);
    } catch (e) {
      entry.status = "ready";
      handleApiError(e);
    }
    render();
  }

  // ------------------------------------------------------------------ document order

  function moveFile(cid, delta) {
    const i = state.files.findIndex(f => f.cid === cid);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= state.files.length) return;
    const go = () => {
      [state.files[i], state.files[j]] = [state.files[j], state.files[i]];
      regroupByDocument();
      render();
    };
    if (state.customized) {
      confirmBox("Regroup pages by document?",
        "Moving a document puts all pages back into document order. Rotations and left-out pages are kept, but pages you moved between documents will be regrouped.",
        "Regroup pages").then(ok => { if (ok) go(); });
    } else go();
  }

  function regroupByDocument() {
    const order = new Map(state.files.map((f, i) => [f.cid, i]));
    // stable: pages keep their current relative order inside each document
    state.seq = state.seq
      .map((p, i) => ({ p, i }))
      .sort((a, b) => (order.get(a.p.file) - order.get(b.p.file)) || (a.i - b.i))
      .map(x => x.p);
    state.customized = false;
    state.output = null;
  }

  // ------------------------------------------------------------------ steps

  function canGo(n) {
    if (n === 1) return true;
    if (n === 2) return readyFiles().length > 0;
    return includedPages().length > 0 && !busy();
  }

  function goStep(n) {
    if (!canGo(n)) return;
    clearError();
    state.step = n;
    render();
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (n === 3) prepareExport();
  }

  // ------------------------------------------------------------------ rendering

  function render() {
    for (const [n, panel] of Object.entries(el.panels)) panel.hidden = Number(n) !== state.step;
    el.stepBtns.forEach((b, i) => {
      const n = i + 1;
      b.classList.toggle("active", n === state.step);
      b.classList.toggle("done", n < state.step);
      b.disabled = !canGo(n);
      if (n === state.step) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
    });
    renderFiles();
    if (state.step === 2) renderOrganize();
    save();
  }

  function statusHtml(f) {
    switch (f.status) {
      case "queued": return `<span class="status-badge pending">Waiting</span>`;
      case "uploading": return `<span class="status-badge pending"><span class="mini-spin"></span>Uploading ${f.progress || 0}%</span>`;
      case "converting": return `<span class="status-badge pending"><span class="mini-spin"></span>Converting&hellip;</span>`;
      case "error": return `<span class="status-badge error">Not added</span><span class="status-detail">${esc(f.error || "")}</span>`;
      default: {
        const dup = state.files.find(o => o !== f && o.status === "ready" && o.sha && o.sha === f.sha && state.files.indexOf(o) < state.files.indexOf(f));
        if (dup) return `<span class="status-badge warn">Ready &middot; duplicate</span><span class="status-detail">Same content as file #${state.files.indexOf(dup) + 1}. Remove one if this wasn't intended.</span>`;
        return `<span class="status-badge ok">Ready</span>`;
      }
    }
  }

  function renderFileStatus(f) {
    const cell = el.fileBody.querySelector(`[data-status="${f.cid}"]`);
    if (cell) cell.innerHTML = statusHtml(f);
  }

  function renderFiles() {
    el.fileBlock.hidden = state.files.length === 0;
    el.fileBody.innerHTML = "";
    state.files.forEach((f, i) => {
      const tr = document.createElement("tr");
      tr.className = "ft-" + f.ftype;
      const fmt = fileTypeLabel(f);
      let sheetHtml = "";
      if (f.kind === "excel" && f.status === "ready" && f.sheets) {
        const chosen = f.sheets.filter(s => s.selected).map(s => s.name);
        sheetHtml = `<div class="sheet-line">Sheets: ${esc(chosen.join(", "))}
          ${f.sheets.length > 1 ? `<button type="button" class="link" data-act="sheets">Choose sheets</button>` : ""}</div>
          <div class="sheet-picker" data-picker="${f.cid}" hidden>
            ${f.sheets.map((s, k) => `<label><input type="checkbox" value="${esc(s.name)}" ${s.selected ? "checked" : ""}>
              ${esc(s.name)}${s.hidden_in_file ? ' <span class="muted">(hidden in the file)</span>' : ""}${s.kind === "chart" ? ' <span class="muted">(chart)</span>' : ""}</label>`).join("")}
            <div class="sheet-picker-actions">
              <button type="button" class="btn btn-primary btn-sm" data-act="sheets-apply">Apply</button>
              <button type="button" class="btn btn-ghost btn-sm" data-act="sheets-cancel">Cancel</button>
            </div>
          </div>`;
      }
      tr.innerHTML = `
        <td class="col-order"><span class="order-badge">${i + 1}</span></td>
        <td class="col-name"><span class="doc-swatch"></span><span class="fmt-tag">${esc(fmt)}</span><span class="file-name">${esc(f.name)}</span>${sheetHtml}</td>
        <td class="col-size file-num">${fmtSize(f.size)}</td>
        <td class="col-count file-num">${f.status === "ready" ? f.pages : "&ndash;"}</td>
        <td class="col-status" data-status="${f.cid}">${statusHtml(f)}</td>
        <td class="col-actions"><div class="row-actions">
          ${f.status === "error" ? "" : `<button type="button" class="btn-icon" data-act="up" title="Move up" aria-label="Move ${esc(f.name)} up" ${i === 0 ? "disabled" : ""}>&uarr;</button>
          <button type="button" class="btn-icon" data-act="down" title="Move down" aria-label="Move ${esc(f.name)} down" ${i === state.files.length - 1 ? "disabled" : ""}>&darr;</button>`}
          <button type="button" class="btn-icon" data-act="remove" title="Remove" aria-label="Remove ${esc(f.name)}" ${f.status === "converting" || f.status === "uploading" ? "disabled" : ""}>&times;</button>
        </div></td>`;
      tr.dataset.cid = f.cid;
      el.fileBody.appendChild(tr);
    });

    const ready = readyFiles();
    const pages = ready.reduce((n, f) => n + f.pages, 0);
    const errors = state.files.filter(f => f.status === "error").length;
    let s;
    if (!state.files.length) s = "Add files to begin.";
    else if (busy()) s = `Converting files&hellip; ${ready.length} of ${state.files.length - errors} ready.`;
    else s = `<strong>${ready.length}</strong> file${ready.length === 1 ? "" : "s"} ready &middot; <strong>${pages}</strong> page${pages === 1 ? "" : "s"}`
      + (errors ? ` &middot; ${errors} not added` : "");
    el.uploadSummary.innerHTML = s;
    el.btnToOrganize.disabled = ready.length === 0;
  }

  // ---- organize

  let sortable = null;
  const thumbCache = new Map();          // key -> canvas
  const docCache = new Map();            // fid:version -> Promise<PDFDocumentProxy>

  function renderOrganize() {
    // documents
    el.docList.innerHTML = "";
    const ready = readyFiles();
    ready.forEach((f) => {
      const i = state.files.indexOf(f);
      const inc = state.seq.filter(p => p.file === f.cid && !p.excluded).length;
      const li = document.createElement("li");
      li.className = "ft-" + f.ftype;
      li.dataset.cid = f.cid;
      li.innerHTML = `<span class="doc-bar"></span><span class="doc-name">${esc(f.name)}</span>
        <span class="doc-pages">${inc} of ${f.pages} page${f.pages === 1 ? "" : "s"}</span>
        <span class="row-actions">
          <button type="button" class="btn-icon" data-act="up" title="Move document up" aria-label="Move ${esc(f.name)} up" ${i === 0 ? "disabled" : ""}>&uarr;</button>
          <button type="button" class="btn-icon" data-act="down" title="Move document down" aria-label="Move ${esc(f.name)} down" ${i === state.files.length - 1 ? "disabled" : ""}>&darr;</button>
        </span>`;
      el.docList.appendChild(li);
    });

    // pages
    const grid = el.grid;
    thumbObserver.disconnect();
    grid.innerHTML = "";
    let pos = 0;
    const visible = state.seq.filter(p => fileByCid(p.file));
    visible.forEach((p) => {
      const f = fileByCid(p.file);
      if (!p.excluded) pos++;
      const li = document.createElement("li");
      li.className = "page-card ft-" + f.ftype + (p.excluded ? " excluded" : "");
      li.dataset.key = p.key;
      li.tabIndex = 0;
      const size = f.sizes[p.page] || [595, 842];
      const sw = (p.rotate % 180) ? size[1] : size[0], sh = (p.rotate % 180) ? size[0] : size[1];
      li.setAttribute("aria-label", `${p.excluded ? "Left out" : "Page " + pos}: ${f.name}, page ${p.page + 1}`);
      li.innerHTML = `
        <div class="page-thumb" style="--ar:${(sw / sh).toFixed(4)}">
          <span class="thumb-ph"></span>
          ${p.excluded ? `<span class="excluded-tag">Left out</span>` : `<button type="button" class="page-pos" data-act="move" title="Move to position…">${pos}</button>`}
        </div>
        <div class="page-meta"><span class="page-src" title="${esc(f.name)}">${esc(f.name)}</span><span class="page-no">p.${p.page + 1}</span></div>
        <div class="page-actions">
          <button type="button" class="btn-icon" data-act="rotl" title="Rotate left" aria-label="Rotate left">${ICON.rotl}</button>
          <button type="button" class="btn-icon" data-act="rotr" title="Rotate right" aria-label="Rotate right">${ICON.rotr}</button>
          <button type="button" class="btn-icon" data-act="toggle" title="${p.excluded ? "Include this page" : "Leave this page out"}"
            aria-label="${p.excluded ? "Include this page" : "Leave this page out"}">${p.excluded ? ICON.back : ICON.out}</button>
        </div>`;
      grid.appendChild(li);
      const thumb = li.querySelector(".page-thumb");
      const ck = thumbKey(f, p);
      if (thumbCache.has(ck)) {
        thumb.querySelector(".thumb-ph").replaceWith(thumbCache.get(ck));
      } else {
        thumb.dataset.thumb = ck;
        thumbObserver.observe(thumb);
      }
    });
    el.gridEmpty.hidden = visible.length > 0;

    if (!sortable && window.Sortable) {
      sortable = Sortable.create(grid, {
        animation: 150, delay: 180, delayOnTouchOnly: true, touchStartThreshold: 6,
        filter: ".btn-icon, .page-pos", preventOnFilter: false,
        ghostClass: "sortable-ghost", chosenClass: "sortable-chosen",
        onEnd: (ev) => {
          if (ev.oldIndex === ev.newIndex) return;
          const order = Array.from(grid.children).map(li => li.dataset.key);
          const byKey = new Map(state.seq.map(p => [p.key, p]));
          state.seq = order.map(k => byKey.get(k)).filter(Boolean);
          state.customized = true;
          state.output = null;
          render();
        },
      });
    }

    const inc = includedPages().length;
    const excl = visible.length - inc;
    el.organizeSummary.innerHTML = `<strong>${inc}</strong> page${inc === 1 ? "" : "s"} in the final PDF`
      + (excl ? ` &middot; ${excl} left out` : "") + (busy() ? " &middot; converting more files&hellip;" : "");
    el.btnToExport.disabled = !canGo(3);
    el.btnIncludeAll.disabled = excl === 0;
  }

  function thumbKey(f, p) { return `${f.id}:${f.version}:${p.page}:${p.rotate}`; }

  function loadDoc(url, cacheKey) {
    if (!docCache.has(cacheKey)) {
      const task = pdfjsLib.getDocument({ url, isEvalSupported: false,
        standardFontDataUrl: "/static/vendor/standard_fonts/" });
      docCache.set(cacheKey, task.promise);
      task.promise.catch(() => docCache.delete(cacheKey));
    }
    return docCache.get(cacheKey);
  }

  // Render queue: at most two thumbnails at a time
  const queue = [];
  let active = 0;
  function enqueue(job) { queue.push(job); drain(); }
  function drain() {
    while (active < 2 && queue.length) {
      const job = queue.shift();
      active++;
      job().catch(() => {}).finally(() => { active--; drain(); });
    }
  }

  const thumbObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      thumbObserver.unobserve(e.target);
      const thumb = e.target;
      const key = thumb.dataset.thumb;
      enqueue(() => renderThumb(thumb, key));
    }
  }, { rootMargin: "300px" });

  async function renderThumb(thumb, key) {
    if (!window.pdfjsLib || !thumb.isConnected) return;
    if (thumbCache.has(key)) { const ph = thumb.querySelector(".thumb-ph"); if (ph) ph.replaceWith(thumbCache.get(key)); return; }
    const [fid, version, page, rot] = key.split(":");
    const f = state.files.find(x => x.id === fid);
    if (!f) return;
    const doc = await loadDoc(`${API}/api/sessions/${state.sid}/files/${fid}/pdf?v=${version}`, `${fid}:${version}`);
    const pg = await doc.getPage(Number(page) + 1);
    const rotation = (pg.rotate + Number(rot)) % 360;
    const base = pg.getViewport({ scale: 1, rotation });
    const box = Math.max(thumb.clientWidth, 120);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const scale = Math.min(box / base.width, (box * 1.15) / base.height) * dpr;
    const vp = pg.getViewport({ scale, rotation });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(vp.width); canvas.height = Math.ceil(vp.height);
    canvas.style.width = (vp.width / dpr) + "px";
    await pg.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport: vp }).promise;
    thumbCache.set(key, canvas);
    const ph = thumb.querySelector(".thumb-ph");
    if (ph && thumb.dataset.thumb === key && thumb.isConnected) ph.replaceWith(canvas);
  }

  // ---- page actions

  function pageAction(key, act) {
    const p = state.seq.find(x => x.key === key);
    if (!p) return;
    if (act === "rotl") p.rotate = (p.rotate + 270) % 360;
    else if (act === "rotr") p.rotate = (p.rotate + 90) % 360;
    else if (act === "toggle") p.excluded = !p.excluded;
    else if (act === "move") { openMove(p); return; }
    state.output = null;
    render();
    const card = el.grid.querySelector(`[data-key="${key}"] [data-act="${act}"]`);
    if (card) card.focus({ preventScroll: true });
  }

  let moving = null;
  function openMove(p) {
    const inc = includedPages();
    const cur = inc.indexOf(p) + 1;
    moving = p;
    el.moveText.textContent = `This is page ${cur} of ${inc.length}. Enter the position it should move to.`;
    el.moveInput.max = inc.length;
    el.moveInput.value = cur;
    if (typeof el.moveDialog.showModal === "function") {
      el.moveDialog.showModal();
      el.moveInput.select();
    } else {
      const v = window.prompt(el.moveText.textContent, String(cur));
      if (v != null) doMove(Number(v));
    }
  }

  function doMove(target) {
    const p = moving;
    moving = null;
    if (!p) return;
    const inc = includedPages();
    target = Math.round(target);
    if (!Number.isFinite(target) || target < 1) target = 1;
    if (target > inc.length) target = inc.length;
    const cur = inc.indexOf(p) + 1;
    if (target === cur) return;
    const without = state.seq.filter(x => x !== p);
    const incWithout = without.filter(x => !x.excluded && fileByCid(x.file));
    let at;
    if (target > incWithout.length) at = without.indexOf(incWithout[incWithout.length - 1]) + 1;
    else at = without.indexOf(incWithout[target - 1]);
    without.splice(at, 0, p);
    state.seq = without;
    state.customized = true;
    state.output = null;
    render();
    const card = el.grid.querySelector(`[data-key="${p.key}"]`);
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("flash");
    }
  }

  // ------------------------------------------------------------------ export

  function signature() {
    return JSON.stringify(includedPages().map(p => {
      const f = fileByCid(p.file);
      return [f.id, f.version, p.page, p.rotate];
    }));
  }

  let buildToken = 0;
  async function prepareExport() {
    el.filename.value = state.filename || DEFAULT_NAME;
    const sig = signature();
    if (state.output && state.output.sig === sig) { showOutput(); return; }
    const token = ++buildToken;
    el.exportReady.hidden = true;
    el.building.hidden = false;
    try {
      const pages = includedPages().map(p => ({ file: fileByCid(p.file).id, page: p.page, rotate: p.rotate }));
      const res = await api("POST", `${API}/api/sessions/${state.sid}/build`, { pages, filename: cleanFilename(el.filename.value) });
      if (token !== buildToken) return;
      state.output = { id: res.output, pages: res.pages, size: res.size, sig,
                       seq: includedPages().map(p => ({ file: p.file, page: p.page, rotate: p.rotate })) };
      showOutput();
    } catch (e) {
      if (token !== buildToken) return;
      el.building.hidden = true;
      handleApiError(e);
    }
  }

  function showOutput() {
    const o = state.output;
    el.building.hidden = true;
    el.exportReady.hidden = false;
    el.outPages.textContent = o.pages;
    const docs = new Set(o.seq.map(p => p.file));
    el.outDocs.textContent = `${docs.size} of ${readyFiles().length}`;
    el.outSize.textContent = fmtSize(o.size);

    el.sequenceList.innerHTML = o.seq.map(p => {
      const f = fileByCid(p.file);
      return `<li>${esc(f ? f.name : "?")} &mdash; p.${p.page + 1}${p.rotate ? ` <span class="rot">(rotated ${p.rotate}&deg;)</span>` : ""}</li>`;
    }).join("");

    // Preview: render the actual combined PDF
    el.preview.innerHTML = "";
    const url = `${API}/api/sessions/${state.sid}/outputs/${o.id}`;
    o.seq.forEach((p, i) => {
      const f = fileByCid(p.file);
      const div = document.createElement("div");
      div.className = "preview-page";
      const size = (f && f.sizes[p.page]) || [595, 842];
      const ar = (p.rotate % 180) ? size[0] / size[1] : size[1] / size[0];
      div.innerHTML = `<div class="pv-box"><div class="pv-ph" style="aspect-ratio:${(1 / ar).toFixed(4)}"></div></div>
        <span class="preview-cap"><b>Page ${i + 1}</b> &middot; ${esc(f ? f.name : "")} p.${p.page + 1}</span>`;
      div.dataset.index = i;
      el.preview.appendChild(div);
      previewObserver.observe(div);
    });
    previewUrl = url;
  }

  let previewUrl = null;
  const previewObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      previewObserver.unobserve(e.target);
      const div = e.target, url = previewUrl;
      enqueue(() => renderPreviewPage(div, url));
    }
  }, { rootMargin: "400px" });

  async function renderPreviewPage(div, url) {
    if (!window.pdfjsLib || !url) return;
    const doc = await loadDoc(url, url);
    const pg = await doc.getPage(Number(div.dataset.index) + 1);
    const base = pg.getViewport({ scale: 1 });
    const width = Math.min(div.clientWidth || 600, 620);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const vp = pg.getViewport({ scale: (width / base.width) * dpr });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(vp.width); canvas.height = Math.ceil(vp.height);
    canvas.style.width = (vp.width / dpr) + "px";
    await pg.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport: vp }).promise;
    const ph = div.querySelector(".pv-ph");
    if (ph && div.isConnected) ph.replaceWith(canvas);
  }

  async function download() {
    clearError();
    const o = state.output;
    if (!o) return;
    state.filename = cleanFilename(el.filename.value);
    el.filename.value = state.filename;
    save();
    try {
      await api("POST", `${API}/api/sessions/${state.sid}/outputs/${o.id}/name`, { filename: state.filename });
    } catch (e) { handleApiError(e); return; }
    const a = document.createElement("a");
    a.href = `${API}/api/sessions/${state.sid}/outputs/${o.id}?download=1`;
    a.download = state.filename + ".pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ------------------------------------------------------------------ reset

  function resetAll(serverGone) {
    const sid = state.sid;
    if (sid && !serverGone) api("DELETE", `${API}/api/sessions/${sid}`).catch(() => {});
    buildToken++;
    state = freshState();
    thumbCache.clear();
    docCache.forEach(p => p.then(d => d.destroy()).catch(() => {}));
    docCache.clear();
    queue.length = 0;
    previewUrl = null;
    el.preview.innerHTML = "";
    el.fileInput.value = "";
    try { sessionStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ }
    document.querySelector('input[name="image-mode"][value="a4"]').checked = true;
    render();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function startOver() {
    if (!state.files.length) { resetAll(); return; }
    const ok = await confirmBox("Start over?",
      "All added files, your page arrangement and the combined PDF will be cleared from this screen and deleted from the server.",
      "Start over");
    if (ok) { clearError(); resetAll(); }
  }

  // ------------------------------------------------------------------ events

  el.btnChoose.addEventListener("click", (e) => { e.stopPropagation(); el.fileInput.click(); });
  el.btnAddMore.addEventListener("click", () => el.fileInput.click());
  el.btnAddMore2.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.fileInput.click(); }
  });
  el.fileInput.addEventListener("change", () => { addFiles(el.fileInput.files); el.fileInput.value = ""; });

  ["dragenter", "dragover"].forEach(t => el.dropzone.addEventListener(t, (e) => {
    e.preventDefault(); el.dropzone.classList.add("drag-over");
  }));
  ["dragleave", "drop"].forEach(t => el.dropzone.addEventListener(t, (e) => {
    e.preventDefault(); el.dropzone.classList.remove("drag-over");
  }));
  el.dropzone.addEventListener("drop", (e) => addFiles(e.dataTransfer && e.dataTransfer.files));
  // Files dropped anywhere else on the page: don't let the browser open them
  window.addEventListener("dragover", (e) => { if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length && !el.dropzone.contains(e.target)) {
      e.preventDefault();
      addFiles(e.dataTransfer.files);
    }
  });

  document.querySelectorAll('input[name="image-mode"]').forEach(r => r.addEventListener("change", async () => {
    if (!r.checked) return;
    state.imageMode = r.value;
    save();
    for (const f of readyFiles().filter(x => x.kind === "image" && x.imageMode !== r.value)) {
      await reconvert(f, { image_mode: r.value });
    }
  }));

  el.fileBody.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const tr = btn.closest("tr");
    const cid = Number(tr.dataset.cid);
    const f = fileByCid(cid);
    const act = btn.dataset.act;
    if (act === "up") moveFile(cid, -1);
    else if (act === "down") moveFile(cid, +1);
    else if (act === "remove") removeFile(cid);
    else if (act === "sheets") tr.querySelector(".sheet-picker").hidden = false;
    else if (act === "sheets-cancel") render();
    else if (act === "sheets-apply") {
      const chosen = Array.from(tr.querySelectorAll(".sheet-picker input:checked")).map(i => i.value);
      if (!chosen.length) { showError("Choose at least one sheet to include."); return; }
      reconvert(f, { sheets: chosen });
    }
  });

  el.docList.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const cid = Number(btn.closest("li").dataset.cid);
    moveFile(cid, btn.dataset.act === "up" ? -1 : 1);
  });

  el.grid.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    pageAction(btn.closest(".page-card").dataset.key, btn.dataset.act);
  });
  el.grid.addEventListener("keydown", (e) => {
    const card = e.target.closest(".page-card");
    if (!card || e.target !== card) return;
    const i = state.seq.findIndex(p => p.key === card.dataset.key);
    let j = null;
    if (e.key === "ArrowLeft" && e.altKey) j = i - 1;
    if (e.key === "ArrowRight" && e.altKey) j = i + 1;
    if (j === null || j < 0 || j >= state.seq.length) return;
    e.preventDefault();
    const [p] = state.seq.splice(i, 1);
    state.seq.splice(j, 0, p);
    state.customized = true; state.output = null;
    render();
    el.grid.querySelector(`[data-key="${p.key}"]`).focus();
  });

  el.moveForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = Number(el.moveInput.value);
    el.moveDialog.close();
    doMove(v);
  });
  el.moveCancel.addEventListener("click", () => { moving = null; el.moveDialog.close(); });

  el.btnIncludeAll.addEventListener("click", () => {
    state.seq.forEach(p => { p.excluded = false; });
    state.output = null; render();
  });
  el.btnResetOrder.addEventListener("click", async () => {
    const ok = await confirmBox("Reset the page order?",
      "Pages go back to document order with no rotation, and every page is included again.", "Reset");
    if (!ok) return;
    const order = new Map(state.files.map((f, i) => [f.cid, i]));
    state.seq.forEach(p => { p.rotate = 0; p.excluded = false; });
    state.seq.sort((a, b) => (order.get(a.file) - order.get(b.file)) || (a.page - b.page));
    state.customized = false; state.output = null;
    render();
  });

  el.stepBtns.forEach((b, i) => b.addEventListener("click", () => goStep(i + 1)));
  el.btnToOrganize.addEventListener("click", () => goStep(2));
  el.btnBackUpload.addEventListener("click", () => goStep(1));
  el.btnToExport.addEventListener("click", () => goStep(3));
  el.btnBackOrganize.addEventListener("click", () => goStep(2));
  el.btnDownload.addEventListener("click", download);
  el.btnStartOver.addEventListener("click", startOver);
  el.filename.addEventListener("change", () => { state.filename = cleanFilename(el.filename.value); save(); });
  el.errorClose.addEventListener("click", clearError);

  [$("home-link"), $("logo-link")].forEach(a => a.addEventListener("click", (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
    startOver();
  }));
  $("logo-img").addEventListener("error", () => $("logo-link").classList.add("logo-failed"));

  window.addEventListener("beforeunload", (e) => {
    if (busy()) { e.preventDefault(); e.returnValue = ""; }
  });

  // ------------------------------------------------------------------ start
  if (!window.pdfjsLib || !window.Sortable) {
    showError("Part of the page didn't load (preview components). Please refresh the page.");
  }
  render();
  restore().catch(() => {}).then(() => { restored = true; render(); });
})();
