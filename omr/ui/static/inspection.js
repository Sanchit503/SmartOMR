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
  let wasMatching = new URL(location.href).searchParams.get("matching") === "1";
  let renderedIdentity = "";
  let renderedRoster = "";
  let assignmentDirty = false;
  let pollSequence = 0;
  const listItems = new Map();
  const viewer = byId("viewer");
  const pageImage = byId("page-image");
  const initialFilter = new URL(location.href).searchParams.get("page-filter");
  if ([...byId("page-filter").options].some((option) => option.value === initialFilter)) byId("page-filter").value = initialFilter;

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
    const key = JSON.stringify([selected, row, Boolean(inventory.identity_preview)]);
    if (key === renderedIdentity) return;
    renderedIdentity = key;
    const hasPreview = Boolean(inventory.identity_preview);
    const state = !hasPreview ? "Not read yet" : row?.status === "detected_with_caution"
      ? "Detected with observations" : row?.status === "needs_review" ? "Needs identity review"
      : row?.status ? names[row.status] || row.status.replaceAll("_", " ") : "Not available";
    text("identity-state", state);
    text("identity-roll", row?.literal_roll_no || (row ? "Not detected" : "Not read yet"));
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
    if (!observations.length) observations.push(row ? "No identity review flags" : "Roll detection pending");
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
      const identityLabel = page.assignment?.roll_no || identity?.literal_roll_no || (identity?.status === "needs_review" ? "identity review" : "");
      const sheetPage = page.assignment?.sheet_page || page.page_index;
      button.querySelector("small").textContent = `${sheetPage ? `Sheet page ${sheetPage}` : names[page.status]}${identityLabel ? ` | ${identityLabel}` : ""}`;
      button.querySelector("i").className = `status-dot ${page.status}`;
      button.setAttribute("aria-label", `Source page ${page.source_index}, ${names[page.status]}`);
      if (page.source_index === selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
      const identityFilter = identityMatchesFilter(identity, filter);
      const assignment = page.assignment || {};
      const matchingFilter = inventory.evaluated && (
        (filter === "matching_unmatched" && !assignment.roll_no && assignment.status === "needs_review") ||
        (filter === "matching_review" && assignment.roll_no && ["needs_review", "missing_pages", "rejected"].includes(assignment.status)) ||
        (filter === "matching_verified" && assignment.status === "verified")
        || (filter === "matching_auto" && assignment.status === "auto_matched")
        || (filter === "matching_approved" && assignment.status === "approved")
      );
      button.hidden = !(
        (!search || String(page.source_index).includes(search) || String(identityLabel).toUpperCase().includes(search.toUpperCase())) &&
        (filter === "all" || filter === page.status || (filter === "waiting" && ["pending", "processing"].includes(page.status)) || identityFilter === true || matchingFilter)
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
    if (byId("ownership-section")) {
      const assignment = page.assignment || {};
      byId("ownership-section").hidden = !inventory.evaluated;
      text("assigned-roll", assignment.roll_no || "Unmatched");
      text("assignment-status", String(assignment.status || "-").replaceAll("_", " "));
      const studentLink = byId("assigned-student-link");
      studentLink.hidden = !assignment.roll_no;
      if (assignment.roll_no) studentLink.href = `${base}/students/${encodeURIComponent(assignment.roll_no)}`;
      const assignmentForm = byId("assignment-form");
      assignmentForm.hidden = !inventory.can_assign || !page.aligned;
      assignmentForm.action = `${base}/pages/${selected}/assign`;
      byId("assignment-page").max = inventory.template_pages;
      const assignmentKey = JSON.stringify([selected, assignment.roll_no, assignment.sheet_page, assignment.status]);
      if (assignmentForm.dataset.source !== String(selected) ||
          (!assignmentDirty && assignmentForm.dataset.assignment !== assignmentKey)) {
        assignmentDirty = false;
        assignmentForm.dataset.source = String(selected);
        assignmentForm.dataset.assignment = assignmentKey;
        byId("assignment-roll").value = assignment.roll_no || "";
        byId("assignment-page").value = assignment.sheet_page || page.page_index || "";
        byId("assignment-note").value = "";
        byId("assignment-replace").checked = false;
        alertText("assignment-feedback", "");
      } else if (assignmentDirty && assignmentForm.dataset.assignment !== assignmentKey) {
        alertText("assignment-feedback", "Ownership changed in another view. Check the current assignment before saving your edits.");
      }
      byId("next-unresolved").hidden = !inventory.can_assign;
    }
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
    if (assignmentDirty && number !== selected && !confirm("Discard unsaved assignment edits and change page?")) return;
    selected = Math.max(1, Math.min(inventory.total, number));
    zoom = 1;
    if (!inventory.pages[selected - 1][view]) view = "original";
    const url = new URL(location.href); url.searchParams.set("page", selected);
    history.replaceState(null, "", url);
    renderList(); renderSelection(); fitImage();
    viewer.scrollTo(0, 0);
    const item = listItems.get(selected);
    if (item && !item.hidden) item.scrollIntoView({ block: "nearest", inline: "nearest" });
    poll(false);
  }

  async function poll(schedule = true) {
    const sequence = ++pollSequence;
    try {
      const source = selected;
      const response = await fetch(`${base}/inspection.json?source=${source}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Inventory unavailable (HTTP ${response.status})`);
      const updated = await response.json();
      if (source !== selected || sequence !== pollSequence) return;
      inventory = updated;
      selected = Math.max(1, Math.min(selected, inventory.total));
      text("total-count", inventory.total);
      text("aligned-count", inventory.counts.aligned);
      text("review-count", inventory.counts.needs_review);
      text("failed-count", inventory.counts.failed);
      const evaluating = ["queued", "running", "grading"].includes(inventory.run_status);
      const readingRolls = inventory.run_status === "identity_previewing";
      const inspectionStage = inventory.run_status.startsWith("inspect") ? inventory.stage : inventory.status;
      text("progress-label", evaluating || readingRolls ? inventory.stage || "Preparing matching" : inventory.evaluated ? "Matching complete" : inventory.status === "completed" ? `${inventory.processed} / ${inventory.total} inspected` : `${inventory.processed} / ${inventory.total} inspected - ${inspectionStage}`);
      const operation = inventory.operation_progress;
      const activeOperation = evaluating || readingRolls;
      byId("progress").max = activeOperation && operation?.total ? operation.total : inventory.total;
      if (activeOperation && !operation?.total) byId("progress").removeAttribute("value");
      else byId("progress").value = activeOperation ? operation.processed : inventory.processed;
      for (const id of ["step-inspect", "step-match", "step-review"]) byId(id)?.removeAttribute("aria-current");
      const step = inventory.two_step ? (inventory.evaluated ? "step-match" : "step-inspect")
        : inventory.evaluated && !activeOperation ? "step-review" : inventory.status === "completed" ? "step-match" : "step-inspect";
      byId(step)?.setAttribute("aria-current", "step");
      byId("inspect-form").hidden = !inventory.can_inspect;
      byId("inspect-form").querySelector("button").textContent = inventory.status === "completed" ? "Retry Failed Pages" : "Resume Inspection";
      byId("identity-preview-form").hidden = !inventory.can_identity_preview;
      byId("identity-preview-form").querySelector("button").textContent = "Read Rolls Only";
      byId("evaluate-form").hidden = !inventory.can_evaluate;
      byId("re-evaluate-form").hidden = !inventory.can_re_evaluate;
      byId("grade-link").hidden = !inventory.can_grade;
      byId("review-link").hidden = !inventory.evaluated;
      alertText("run-error", inventory.run_error || inventory.error);
      alertText("connection-error", "");
      const preview = inventory.identity_preview;
      const summary = inventory.review_summary;
      if (byId("matching-summary")) {
        byId("matching-summary").hidden = !summary;
        if (summary) text("matching-summary", `${summary.students} students; ${summary.verified} manually checked; ${summary.auto_matched || 0} auto matched; ${summary.approved || 0} approved for release; ${summary.pending_verification} pending verification; ${summary.needs_review} need review; ${summary.assigned_pages} assigned pages; ${summary.unmatched_pages} unmatched; ${summary.page_errors} unreadable.`);
      }
      identityRows = new Map((preview?.pages || []).map((row) => [Number(row.source_index), row]));
      const previewSummary = byId("identity-preview-summary");
      if (preview?.status === "completed") {
        const counts = preview.counts || {};
        previewSummary.hidden = false;
        previewSummary.classList.toggle("has-issues", Boolean(counts.needs_review || counts.unavailable));
        previewSummary.textContent = `${inventory.evaluated ? "Original OCR evidence" : "Roll evidence"}: ${counts.detected || 0} detected (${counts.detected_with_caution || 0} with observations); ${counts.needs_review || 0} need review, including ${counts.undetected || 0} with no literal roll; ${counts.unavailable || 0} unavailable.`;
      } else if (inventory.run_status === "identity_previewing") {
        previewSummary.hidden = false;
        previewSummary.textContent = inventory.stage || "Reading rolls from inspected pages";
      } else previewSummary.hidden = true;
      text("file-name", inventory.source_name);
      text("render-dpi", inventory.source_name.toLowerCase().endsWith(".pdf") ? `${inventory.dpi} DPI (PDF)` : "Native image");
      text("source-hash", inventory.source_sha256);
      text("manifest-hash", inventory.manifest_sha256);
      const roster = JSON.stringify(inventory.review_rolls || []);
      if (roster !== renderedRoster) {
        renderedRoster = roster;
        byId("review-rolls")?.replaceChildren(...(inventory.review_rolls || []).map((student) => {
          const option = document.createElement("option"); option.value = student.roll_no;
          option.label = student.name || student.roll_no; return option;
        }));
      }
      renderList(); renderSelection();
      if (wasMatching && inventory.run_status === "completed" && inventory.evaluated) {
        location.assign(`${base}/review`);
      }
      if (evaluating) wasMatching = true;
    } catch (error) {
      alertText("connection-error", `${error.message}. Retrying; displayed results may be out of date.`);
      byId("evaluate-form").hidden = true;
      byId("inspect-form").hidden = true;
    } finally {
      if (schedule) setTimeout(poll, inventory?.run_status === "completed" ? 10000 : 2500);
    }
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
  byId("assignment-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button"); button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST", headers: { Accept: "application/json" }, body: new URLSearchParams(new FormData(form)),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Assignment could not be saved");
      assignmentDirty = false;
      form.dataset.assignment = "";
      await poll(false);
      alertText("assignment-feedback", "Assignment saved. Student sheet awaits verification.");
    } catch (error) { alertText("assignment-feedback", error.message); }
    finally { button.disabled = false; }
  });
  byId("assignment-form")?.addEventListener("input", () => { assignmentDirty = true; });
  byId("assignment-form")?.addEventListener("change", () => { assignmentDirty = true; });
  window.addEventListener("focus", () => poll(false));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(false); });
  byId("next-unresolved")?.addEventListener("click", () => {
    const unresolved = inventory.pages.filter((page) => {
      const assignment = page.assignment || {};
      return !["verified", "approved", "auto_matched", "ignored"].includes(assignment.status);
    });
    const next = unresolved.find((page) => page.source_index > selected) || unresolved[0];
    if (next) select(next.source_index);
  });
  for (const form of root.querySelectorAll("form")) {
    form.addEventListener("submit", () => { form.querySelector("button").disabled = true; });
  }
  poll();
})();
