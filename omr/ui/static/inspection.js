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
  let identityRows = new Map();
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
  function identityRow(sourceIndex) { return identityRows.get(Number(sourceIndex)) || null; }
  function identityDetected(row) { return row && ["detected", "detected_with_caution"].includes(row.status); }
  function identityMatchesFilter(row, filter) {
    if (filter === "identity_review") return row?.status === "needs_review";
    if (filter === "identity_undetected") return row && !row.literal_roll_no && row.status !== "unavailable";
    if (filter === "identity_detected") return identityDetected(row);
    if (filter === "identity_unavailable") return row?.status === "unavailable";
    return null;
  }

  function selectedRead(row) {
    const identity = row?.identity || {};
    const read = identity.write_in_roll_read || identity;
    const program = String(row?.program || read.program || "").toUpperCase();
    const result = read.ocr_results?.[program] || {};
    const cells = result.cells || {};
    const entries = Array.isArray(cells.raw?.cells) ? cells.raw.cells : [];
    const paths = Array.isArray(read.cell_crop_paths?.[program])
      ? read.cell_crop_paths[program]
      : Array.isArray(result.cell_crop_paths) ? result.cell_crop_paths : [];
    return { entries, paths };
  }

  function identityAsset(path) {
    return `${base}/identity-asset?path=${encodeURIComponent(path)}`;
  }

  function renderIdentity(row) {
    const hasPreview = inventory.identity_preview?.status === "completed";
    const state = !hasPreview ? "Not previewed" : row?.status === "detected_with_caution"
      ? "Detected with observations" : row?.status ? names[row.status] || row.status.replaceAll("_", " ") : "Not available";
    text("identity-state", state);
    text("identity-roll", row?.literal_roll_no || (hasPreview ? "Not detected" : "Not previewed"));
    text("identity-program", row?.program || "-");
    text("identity-confidence", row?.confidence || "-");
    text("identity-roster", row ? (row.roster_member ? "Yes" : row.literal_roll_no ? "No" : "-") : "-");
    text("identity-kind", row?.identity_kind || "-");

    const container = byId("identity-cells");
    container.replaceChildren();
    const read = selectedRead(row);
    const rollDigits = String(row?.literal_roll_no || "").replace(/\D/g, "");
    const count = Math.max(read.entries.length, read.paths.length);
    for (let index = 0; index < count; index++) {
      const entry = read.entries[index] || {};
      const raw = entry.raw || {};
      const digit = String(entry.text ?? rollDigits[index] ?? "?");
      const probability = Number(raw.top_probability ?? entry.confidence);
      const figure = document.createElement("figure");
      figure.className = "identity-cell";
      if (read.paths[index]) {
        const img = document.createElement("img");
        img.src = identityAsset(read.paths[index]);
        img.alt = `Digit ${index + 1} crop`;
        figure.append(img);
      }
      const label = document.createElement("strong");
      label.textContent = `${index + 1}: ${digit}`;
      figure.append(label);
      const detail = document.createElement("small");
      detail.textContent = Number.isFinite(probability) ? `P ${probability.toFixed(3)}` : "P -";
      figure.append(detail);
      container.append(figure);
    }

    const observations = [];
    if (row?.reason) observations.push(row.reason);
    for (const flag of row?.review_flags || []) observations.push(String(flag));
    const identity = row?.identity || {};
    const readPayload = identity.write_in_roll_read || identity;
    for (const flag of [...(identity.evidence_flags || []), ...(readPayload.evidence_flags || [])]) {
      if (!flag || typeof flag !== "object") continue;
      observations.push(`${flag.code || "IDENTITY"}: ${flag.message || "Identity evidence requires review"}`);
    }
    if (!observations.length) observations.push(hasPreview ? "No identity review flags" : "Run Preview Roll Detection to inspect identity evidence.");
    byId("identity-observations").replaceChildren(...observations.map((value) => {
      const li = document.createElement("li"); li.textContent = value; return li;
    }));
  }

  function renderList() {
    const filter = byId("page-filter").value;
    const search = byId("page-search").value.trim();
    let visible = 0;
    for (const page of inventory.pages) {
      const identity = identityRow(page.source_index);
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
      const identityLabel = identity?.literal_roll_no || (identity?.status === "needs_review" ? "identity review" : "");
      button.querySelector("small").textContent = `${page.page_index ? `Sheet page ${page.page_index}` : names[page.status]}${identityLabel ? ` | ${identityLabel}` : ""}`;
      button.querySelector("i").className = `status-dot ${page.status}`;
      button.setAttribute("aria-label", `Source page ${page.source_index}, ${names[page.status]}`);
      if (page.source_index === selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
      const identityFilter = identityMatchesFilter(identity, filter);
      button.hidden = !(
        (!search || String(page.source_index).includes(search) || String(identity?.literal_roll_no || "").includes(search.toUpperCase())) &&
        (filter === "all" || filter === page.status || (filter === "waiting" && ["pending", "processing"].includes(page.status)) || identityFilter === true)
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
    const pageIndexLabel = page.page_index ? `${page.page_index} / ${inventory.template_pages}` : "Not detected";
    text("template-number", page.page_index_source === "manual" ? `${pageIndexLabel} (manual)` : pageIndexLabel);
    text("alignment-score", score(page.alignment_confidence));
    text("page-code-score", score(page.page_mark_confidence));
    text("quality-score", score(quality.score));
    renderIdentity(identityRow(page.source_index));
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
    const pageIndexForm = byId("page-index-form");
    const pageIndexInput = byId("manual-page-index");
    pageIndexForm.action = `${base}/pages/${selected}/page-index`;
    pageIndexForm.hidden = inventory.evaluated || ["queued", "running", "inspecting"].includes(inventory.run_status);
    pageIndexInput.max = inventory.template_pages;
    if (pageIndexForm.dataset.source !== String(selected)) {
      pageIndexForm.dataset.source = String(selected);
      pageIndexInput.value = page.page_index || "";
    }
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
      byId("identity-preview-form").hidden = !inventory.can_identity_preview;
      byId("identity-preview-form").querySelector("button").textContent = inventory.identity_preview?.status === "completed" ? "Refresh Roll Preview" : "Preview Roll Detection";
      byId("evaluate-form").hidden = !inventory.can_evaluate;
      byId("re-evaluate-form").hidden = !inventory.can_re_evaluate;
      byId("review-link").hidden = !inventory.evaluated;
      alertText("run-error", inventory.run_error || inventory.error);
      alertText("connection-error", "");
      const preview = inventory.identity_preview;
      identityRows = new Map((preview?.pages || []).map((row) => [Number(row.source_index), row]));
      const previewSummary = byId("identity-preview-summary");
      if (preview?.status === "completed") {
        const counts = preview.counts || {};
        previewSummary.hidden = false;
        previewSummary.textContent = `Roll preview: ${counts.detected || 0} literal rolls detected (${counts.detected_with_caution || 0} with quality observations); ${counts.needs_review || 0} identity reads need review, including ${counts.undetected || 0} with no literal roll; ${counts.unavailable || 0} unavailable. Use the Roll preview filters and select a source page to inspect its evidence before matching sheets.`;
      } else if (inventory.run_status === "identity_previewing") {
        previewSummary.hidden = false;
        previewSummary.textContent = inventory.stage || "Reading rolls from inspected pages";
      } else previewSummary.hidden = true;
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
