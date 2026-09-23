(() => {
  "use strict";
  const root = document.getElementById("inspection");
  const base = `/runs/${encodeURIComponent(root.dataset.runId)}`;
  const byId = (id) => document.getElementById(id);
  const names = { pending: "Waiting", processing: "Processing", aligned: "Aligned", needs_review: "Needs inspection", failed: "Failed" };
  let inventory = null;
  let selected = Number(new URL(location.href).searchParams.get("page")) || 1;
  let view = "original";
  let zoom = 1;
  let currentImage = "";
  const listItems = new Map();
  const viewer = byId("viewer");
  const pageImage = byId("page-image");

  function text(id, value) { byId(id).textContent = value; }
  function alertText(id, value) {
    const element = byId(id);
    element.hidden = !value;
    element.textContent = value || "";
  }
  function score(value) { return Number.isFinite(value) ? value.toFixed(3) : "-"; }

  function renderList() {
    const filter = byId("page-filter").value;
    const search = byId("page-search").value.trim();
    let visible = 0;
    for (const page of inventory.pages) {
      let button = listItems.get(page.source_index);
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "page-item";
        button.dataset.source = page.source_index;
        button.innerHTML = '<span class="page-symbol"><img src="/static/icons/file-text.svg" alt=""></span><span class="page-description"><strong></strong><small></small></span><i class="status-dot" aria-hidden="true"></i>';
        button.addEventListener("click", () => select(page.source_index));
        byId("page-list").append(button);
        listItems.set(page.source_index, button);
      }
      button.querySelector("strong").textContent = `Source ${String(page.source_index).padStart(3, "0")}`;
      button.querySelector("small").textContent = page.page_index ? `Sheet page ${page.page_index}` : names[page.status];
      button.querySelector("i").className = `status-dot ${page.status}`;
      button.setAttribute("aria-label", `Source page ${page.source_index}, ${names[page.status]}`);
      if (page.source_index === selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
      button.hidden = !(
        (!search || String(page.source_index).includes(search)) &&
        (filter === "all" || filter === page.status || (filter === "waiting" && ["pending", "processing"].includes(page.status)))
      );
      if (!button.hidden) visible++;
    }
    text("filtered-count", `${visible} / ${inventory.total}`);
    byId("no-pages").hidden = visible > 0;
  }

  function fitImage() {
    if (!pageImage.naturalWidth || pageImage.hidden) return;
    const padding = innerWidth <= 560 ? 28 : 44;
    const fit = Math.min((viewer.clientWidth - padding) / pageImage.naturalWidth,
                        (viewer.clientHeight - padding) / pageImage.naturalHeight);
    const width = Math.max(1, Math.floor(pageImage.naturalWidth * fit * zoom));
    pageImage.style.width = `${width}px`;
    pageImage.style.height = "auto";
    byId("image-stage").style.width = `${Math.max(viewer.clientWidth - padding, width)}px`;
    text("zoom-label", `${Math.round(zoom * 100)}%`);
    byId("zoom-out").disabled = zoom <= .5;
    byId("zoom-in").disabled = zoom >= 4;
  }

  function renderSelection() {
    if (!inventory) return;
    const page = inventory.pages[selected - 1];
    const quality = page.quality || {};
    byId("page-jump").value = selected;
    byId("page-jump").max = inventory.total;
    text("page-total", `/ ${inventory.total}`);
    byId("previous-page").disabled = selected <= 1;
    byId("next-page").disabled = selected >= inventory.total;
    text("selected-heading", `Source ${String(selected).padStart(3, "0")}`);
    text("page-status", names[page.status]);
    byId("page-status").className = `state-label ${page.status}`;
    text("source-number", `${selected} / ${inventory.total}`);
    text("template-number", page.page_index ? `${page.page_index} / ${inventory.template_pages}` : "Not detected");
    text("alignment-score", score(page.alignment_confidence));
    text("page-code-score", score(page.page_mark_confidence));
    text("quality-score", score(quality.score));
    text("identity-state", inventory.evaluated ? "See student review" : ["queued", "running"].includes(inventory.run_status) ? "Evaluation running" : "Not evaluated");
    for (const key of ["geometry", "local", "image"]) {
      const status = quality.metrics?.[`${key}_quality`]?.status;
      text(`${key}-status`, status === "ready" ? "Pass" : status ? status.replaceAll("_", " ") : "Not measured");
    }
    const observations = [...(quality.review_flags || []), ...(quality.warnings || [])];
    if (page.error) observations.unshift(page.error);
    if (!observations.length) observations.push(page.status === "aligned" ? "No alignment flags" : "Inspection pending");
    byId("observations").replaceChildren(...observations.map((value) => {
      const li = document.createElement("li"); li.textContent = value; return li;
    }));
    byId("quality-report").hidden = !page.report;
    if (page.report) byId("quality-report").href = `${base}/pages/${selected}/report`;
    for (const button of root.querySelectorAll("[data-view]")) {
      button.disabled = !page[button.dataset.view];
      button.setAttribute("aria-pressed", String(button.dataset.view === view));
    }
    const imageUrl = page[view] ? `${base}/pages/${selected}/${view}` : "";
    if (imageUrl !== currentImage) {
      currentImage = imageUrl;
      pageImage.hidden = true;
      byId("viewer-empty").hidden = false;
      text("viewer-empty", imageUrl ? "Loading page image" : page.status === "failed" ? "This view is unavailable" : "Waiting for page image");
      if (imageUrl) {
        pageImage.alt = `${view} image of source page ${selected}`;
        pageImage.src = imageUrl;
      } else pageImage.removeAttribute("src");
    } else if (!imageUrl) {
      text("viewer-empty", page.status === "failed" ? "This view is unavailable" : "Waiting for page image");
    }
    text("image-label", view.charAt(0).toUpperCase() + view.slice(1));
    text("source-label", `${inventory.source_name} / ${selected}`);
  }

  function select(number) {
    if (!inventory || !Number.isInteger(number)) return;
    selected = Math.max(1, Math.min(inventory.total, number));
    zoom = 1;
    if (!inventory.pages[selected - 1][view]) view = "original";
    const url = new URL(location.href); url.searchParams.set("page", selected);
    history.replaceState(null, "", url);
    renderList(); renderSelection(); fitImage();
    viewer.scrollTo(0, 0);
    const item = listItems.get(selected);
    if (item && !item.hidden) item.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  async function poll() {
    try {
      const response = await fetch(`${base}/inspection.json`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Inventory unavailable (HTTP ${response.status})`);
      inventory = await response.json();
      selected = Math.max(1, Math.min(selected, inventory.total));
      text("total-count", inventory.total);
      text("aligned-count", inventory.counts.aligned);
      text("review-count", inventory.counts.needs_review);
      text("failed-count", inventory.counts.failed);
      const evaluating = ["queued", "running"].includes(inventory.run_status);
      const inspectionStage = inventory.run_status.startsWith("inspect") ? inventory.stage : inventory.status;
      text("progress-label", evaluating ? inventory.stage || "Evaluation queued" : inventory.status === "completed" ? `${inventory.processed} / ${inventory.total} inspected` : `${inventory.processed} / ${inventory.total} inspected - ${inspectionStage}`);
      byId("progress").max = inventory.total;
      if (evaluating) byId("progress").removeAttribute("value");
      else byId("progress").value = inventory.processed;
      byId("inspect-form").hidden = !inventory.can_inspect;
      byId("inspect-form").querySelector("button").textContent = inventory.status === "completed" ? "Retry Failed Pages" : "Resume Inspection";
      byId("evaluate-form").hidden = !inventory.can_evaluate;
      byId("review-link").hidden = !inventory.evaluated;
      alertText("run-error", inventory.run_error || inventory.error);
      alertText("connection-error", "");
      text("file-name", inventory.source_name);
      text("render-dpi", inventory.source_name.toLowerCase().endsWith(".pdf") ? `${inventory.dpi} DPI (PDF)` : "Native image");
      text("source-hash", inventory.source_sha256);
      text("manifest-hash", inventory.manifest_sha256);
      renderList(); renderSelection();
    } catch (error) {
      alertText("connection-error", `${error.message}. Retrying; displayed results may be out of date.`);
      byId("evaluate-form").hidden = true;
      byId("inspect-form").hidden = true;
    } finally { setTimeout(poll, 2500); }
  }

  pageImage.addEventListener("load", () => {
    if (!currentImage) return;
    pageImage.hidden = false; byId("viewer-empty").hidden = true; fitImage();
  });
  pageImage.addEventListener("error", () => {
    pageImage.hidden = true; byId("viewer-empty").hidden = false;
    text("viewer-empty", "Page image could not be loaded. Reselect the page to retry.");
    currentImage = "";
  });
  new ResizeObserver(fitImage).observe(viewer);
  byId("page-search").addEventListener("input", () => inventory && renderList());
  byId("page-filter").addEventListener("change", () => inventory && renderList());
  byId("page-jump").addEventListener("change", (event) => select(Number(event.target.value)));
  byId("previous-page").addEventListener("click", () => select(selected - 1));
  byId("next-page").addEventListener("click", () => select(selected + 1));
  for (const button of root.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => { view = button.dataset.view; renderSelection(); });
  }
  byId("zoom-in").addEventListener("click", () => { zoom = Math.min(4, zoom + .25); fitImage(); });
  byId("zoom-out").addEventListener("click", () => { zoom = Math.max(.5, zoom - .25); fitImage(); });
  byId("zoom-fit").addEventListener("click", () => { zoom = 1; fitImage(); viewer.scrollTo(0, 0); });
  for (const form of root.querySelectorAll("form")) {
    form.addEventListener("submit", () => { form.querySelector("button").disabled = true; });
  }
  poll();
})();
