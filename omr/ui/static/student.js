(() => {
  "use strict";
  const root = document.getElementById("student-workspace");
  if (!root) return;
  const data = JSON.parse(document.getElementById("student-view-data").textContent);
  const byId = (id) => document.getElementById(id);
  const form = byId("student-decision-form");
  const search = byId("student-search");
  const filter = byId("student-filter");
  const items = [...root.querySelectorAll(".student-item")];
  const canvas = byId("sheet-canvas");
  const image = byId("sheet-image");
  const stateKey = `student-view:${location.pathname}`;
  let savedState = {};
  try { savedState = JSON.parse(sessionStorage.getItem(stateKey) || "{}"); } catch (_) { /* Ignore expired browser state. */ }
  let selected = Number(new URL(location.href).searchParams.get("sheet")) || savedState.page || 1;
  let mode = ["original", "aligned", "overlay", "sampling"].includes(savedState.mode) ? savedState.mode : "aligned";
  let zoom = Math.max(.5, Math.min(3, Number(savedState.zoom) || 1));
  let currentImage = "";
  let saving = false;
  let leaving = false;
  const forms = [...root.querySelectorAll("form")];
  const signature = (target) => JSON.stringify([...target.elements].filter((field) => field.name && !field.hasAttribute("data-sync-ignore"))
    .map((field) => [field.name, field.value, field.checked, [...(field.files || [])].map((file) => [file.name, file.size, file.lastModified])]));
  const signatures = forms.map(signature);
  const dirty = () => forms.some((target, index) => signature(target) !== signatures[index]);
  function remember() {
    sessionStorage.setItem(stateKey, JSON.stringify({ page: selected, mode, zoom, queueScroll: root.querySelector(".student-list").scrollTop }));
  }
  function updateUrl() {
    const url = new URL(location.href);
    url.searchParams.set("queue", filter.value);
    url.searchParams.set("q", search.value.trim());
    url.searchParams.set("sheet", selected);
    url.searchParams.delete("saved");
    url.searchParams.delete("finished");
    history.replaceState(null, "", url);
  }
  function navigate(url) {
    if (saving) return;
    if (dirty() && !confirm("Discard unsaved edits and open another sheet?")) return;
    leaving = true;
    remember();
    location.assign(url);
  }
  function filterQueue() {
    const query = search.value.trim().toLowerCase();
    const kind = filter.value;
    let visible = [];
    for (const item of items) {
      const status = item.dataset.status;
      const matches = kind === "all" || (kind === "unchecked" && status !== "verified")
        || (kind === "needs_review" && ["needs_review", "missing_pages", "rejected"].includes(status))
        || (kind === "missing_pages" && item.dataset.missing === "true")
        || (!["unchecked", "needs_review", "missing_pages"].includes(kind) && kind === status);
      item.hidden = !matches || !`${item.dataset.roll} ${item.dataset.name}`.toLowerCase().includes(query);
      const url = new URL(item.href);
      url.searchParams.set("queue", kind);
      url.searchParams.set("q", search.value.trim());
      item.href = url;
      if (!item.hidden) visible.push(item);
    }
    byId("student-queue-count").textContent = `${visible.length} / ${items.length}`;
    byId("student-queue-empty").hidden = visible.length > 0;
    const position = visible.findIndex((item) => item.dataset.roll === data.roll);
    byId("student-position").textContent = position < 0 ? "Selected outside filter" : `${position + 1} / ${visible.length}`;
    for (const [id, item] of [["student-prev", visible.filter((row) => row.dataset.roll < data.roll).at(-1)],
                               ["student-next", visible.find((row) => row.dataset.roll > data.roll)]]) {
      byId(id).disabled = !item;
      byId(id).onclick = () => item && navigate(item.href);
    }
    form.elements.queue.value = kind;
    form.elements.q.value = search.value.trim();
    updateUrl();
  }
  function fit() {
    if (!image.naturalWidth || image.hidden) return;
    const width = Math.max(100, canvas.clientWidth - 36);
    const height = Math.max(100, canvas.clientHeight - 36);
    const scale = Math.min(width / image.naturalWidth, height / image.naturalHeight) * zoom;
    image.style.width = `${Math.round(image.naturalWidth * scale)}px`;
    image.style.height = `${Math.round(image.naturalHeight * scale)}px`;
    byId("sheet-image-stage").style.width = `${Math.max(width, image.naturalWidth * scale)}px`;
    byId("sheet-zoom-value").textContent = `${Math.round(zoom * 100)}%`;
    byId("sheet-zoom-out").disabled = zoom <= .5;
    byId("sheet-zoom-in").disabled = zoom >= 3;
  }
  function empty(message) {
    image.hidden = true;
    byId("sheet-empty").hidden = false;
    byId("sheet-empty").textContent = message;
  }
  function showPage(number) {
    const pages = data.pages;
    selected = pages.some((page) => page.page === Number(number)) ? Number(number) : (pages[0]?.page || 1);
    const page = pages.find((entry) => entry.page === selected);
    const position = pages.findIndex((entry) => entry.page === selected);
    byId("sheet-jump").value = selected;
    byId("sheet-prev").disabled = position <= 0;
    byId("sheet-next").disabled = position < 0 || position >= pages.length - 1;
    byId("sheet-prev").onclick = () => showPage(pages[position - 1]?.page);
    byId("sheet-next").onclick = () => showPage(pages[position + 1]?.page);
    for (const button of root.querySelectorAll(".sheet-thumb")) button.setAttribute("aria-pressed", String(Number(button.dataset.page) === selected));
    for (const button of root.querySelectorAll("[data-mode]")) {
      button.disabled = !page?.[button.dataset.mode];
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    }
    const source = page?.source_index;
    byId("sheet-source-label").textContent = `Sheet page ${selected} | ${source ? `Source ${source}` : "No source selected"} | ${data.roll}`;
    const correction = byId("sheet-correct-link");
    correction.hidden = !source;
    if (source) correction.href = `/runs/${encodeURIComponent(data.runId)}/pages?page=${source}`;
    const evidence = page?.evidence || {};
    for (const [id, key] of [["sheet-detected-page", "detected_page"], ["sheet-literal-roll", "literal_roll"],
                             ["sheet-bubble-roll", "bubble_roll"], ["sheet-written-roll", "written_roll"],
                             ["sheet-read-confidence", "confidence"]]) {
      byId(id).textContent = evidence[key] || "Not recorded";
    }
    byId("sheet-identity-flags").replaceChildren(...(evidence.flags || []).map((flag) => {
      const item = document.createElement("li"); item.textContent = flag; return item;
    }));
    const url = page?.[mode];
    canvas.scrollTo(0, 0);
    if (!url) {
      currentImage = "";
      image.removeAttribute("src");
      empty(!page?.aligned ? `Sheet page ${selected} is missing or its image is unavailable.` : "This image view is unavailable for the selected page.");
    } else if (url !== currentImage) {
      currentImage = url;
      empty("Loading sheet page");
      image.alt = `${data.roll}, sheet page ${selected}, source ${source || "uploaded"}, ${mode}`;
      image.src = url;
    } else { image.hidden = false; byId("sheet-empty").hidden = true; fit(); }
    remember();
    updateUrl();
  }
  image.addEventListener("load", () => { image.hidden = false; byId("sheet-empty").hidden = true; fit(); });
  image.addEventListener("error", () => empty("The selected page image could not be loaded. Refresh to check its saved source."));
  for (const button of root.querySelectorAll(".sheet-thumb")) button.addEventListener("click", () => showPage(button.dataset.page));
  for (const button of root.querySelectorAll("[data-mode]")) button.addEventListener("click", () => { mode = button.dataset.mode; showPage(selected); });
  byId("sheet-jump").addEventListener("change", (event) => showPage(event.target.value));
  for (const [id, delta] of [["sheet-zoom-out", -.25], ["sheet-zoom-in", .25], ["sheet-fit", 0]]) {
    byId(id).addEventListener("click", () => { zoom = delta ? Math.max(.5, Math.min(3, zoom + delta)) : 1; fit(); remember(); });
  }
  new ResizeObserver(fit).observe(canvas);
  search.addEventListener("input", filterQueue);
  filter.addEventListener("change", filterQueue);
  for (const link of root.querySelectorAll("a:not([target='_blank'])")) link.addEventListener("click", (event) => {
    if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); navigate(link.href);
  });
  const verifyButtons = [...form.querySelectorAll("[data-decision='verify']")];
  function verificationEnabled() {
    for (const button of verifyButtons) button.disabled = saving || data.blocked || (data.missing.length > 0 && !form.elements.allow_missing?.checked);
  }
  form.elements.allow_missing?.addEventListener("change", verificationEnabled);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving) return;
    const button = event.submitter || verifyButtons[0];
    if (button.disabled) { window.dispatchEvent(new Event("review-submission-failed")); return; }
    const action = button.dataset.decision;
    if (action === "reject" && !confirm(`Reject the grouping for ${data.roll}?`)) {
      window.dispatchEvent(new Event("review-submission-failed")); return;
    }
    saving = true;
    const body = new URLSearchParams(new FormData(form));
    body.set("advance", button.value);
    for (const target of form.querySelectorAll("button")) target.disabled = true;
    const feedback = byId("student-save-feedback");
    feedback.hidden = false;
    feedback.classList.remove("save-error");
    feedback.textContent = "Saving review decision...";
    try {
      const response = await fetch(button.getAttribute("formaction") || form.action, {
        method: "POST", headers: { Accept: "application/json" }, body,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Review decision could not be saved");
      leaving = true;
      remember();
      location.assign(result.redirect_url);
    } catch (error) {
      feedback.classList.add("save-error");
      feedback.textContent = error.message;
      feedback.scrollIntoView({ block: "nearest" });
      saving = false;
      for (const target of form.querySelectorAll("button")) target.disabled = false;
      verificationEnabled();
      window.dispatchEvent(new Event("review-submission-failed"));
    }
  });
  window.addEventListener("beforeunload", (event) => {
    remember();
    if (!leaving && dirty()) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("review-view-refresh", () => { leaving = true; });
  filterQueue();
  showPage(selected);
  verificationEnabled();
  const list = root.querySelector(".student-list");
  if (savedState.queueScroll !== undefined) list.scrollTop = savedState.queueScroll;
  else list.querySelector("[aria-current='page']")?.scrollIntoView({ block: "nearest" });
})();
