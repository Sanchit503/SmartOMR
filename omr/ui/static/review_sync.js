(() => {
  "use strict";
  const status = document.getElementById("review-sync");
  if (!status) return;
  const forms = [...document.querySelectorAll("form")];
  const signature = (form) => JSON.stringify([...form.elements].filter((field) => field.name && !field.hasAttribute("data-sync-ignore")).map((field) =>
    [field.name, field.value, field.checked, [...(field.files || [])].map((file) => [file.name, file.size, file.lastModified])]));
  const originals = forms.map(signature);
  const dirty = () => forms.some((form, index) => signature(form) !== originals[index]);
  let busy = false;
  let changed = false;
  let submitting = false;
  const scrollKey = `review-scroll:${location.pathname}`;
  const savedScroll = sessionStorage.getItem(scrollKey);
  if (savedScroll !== null) {
    sessionStorage.removeItem(scrollKey);
    requestAnimationFrame(() => window.scrollTo(0, Number(savedScroll)));
  }
  function refresh() {
    sessionStorage.setItem(scrollKey, String(window.scrollY));
    window.dispatchEvent(new Event("review-view-refresh"));
    location.reload();
  }
  status.querySelector("button").addEventListener("click", () => {
    if (!dirty() || confirm("Discard unsaved edits and load the latest review state?")) refresh();
  });
  for (const form of forms) form.addEventListener("submit", () => { submitting = true; });
  window.addEventListener("review-submission-failed", () => { queueMicrotask(() => { submitting = false; }); });
  async function check() {
    if (busy || submitting || document.hidden) return;
    busy = true;
    try {
      const response = await fetch(status.dataset.endpoint, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      changed = state.revision !== status.dataset.revision;
      const editing = document.activeElement?.closest("form");
      if (changed && !dirty() && !editing) { refresh(); return; }
      status.hidden = !changed;
      status.querySelector("span").textContent = "Review state changed in another view. Your unsaved edits are preserved.";
    } catch (error) {
      status.hidden = false;
      status.querySelector("span").textContent = "Review updates are unavailable. Retrying; displayed results may be out of date.";
    } finally { busy = false; }
  }
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", check);
  window.addEventListener("pageshow", check);
  setInterval(check, 3000);
  check();
})();
