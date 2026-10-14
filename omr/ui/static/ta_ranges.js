(() => {
  const form = document.getElementById('ta-range-form');
  const data = document.getElementById('ta-students');
  if (!form || !data) return;
  const students = JSON.parse(data.textContent);
  const first = document.getElementById('ta-from');
  const last = document.getElementById('ta-to');
  const question = document.getElementById('ta-question');
  const rows = document.getElementById('ta-selection-rows');
  const summary = document.getElementById('ta-selection-count');
  const add = document.getElementById('ta-add-range');
  const initiallyDisabled = add.disabled;
  const approved = new Set(['verified', 'approved', 'auto_matched']);
  function refresh() {
    const start = Number(first.value), end = Number(last.value);
    const valid = Number.isInteger(start) && Number.isInteger(end) && start >= 1 && end >= start && end <= students.length;
    const page = Number(question.selectedOptions[0]?.dataset.page);
    last.setCustomValidity(valid ? '' : 'Choose a valid student range.');
    add.disabled = initiallyDisabled || !valid;
    rows.replaceChildren();
    if (!valid) { summary.textContent = 'No valid range selected'; return; }
    const selected = students.slice(start - 1, end);
    let ready = 0;
    const fragment = document.createDocumentFragment();
    for (const student of selected) {
      const hasPage = student.pages.includes(page);
      const resolved = approved.has(student.status);
      if (hasPage && resolved) ready++;
      const row = document.createElement('tr');
      for (const value of [student.position, student.roll_no, student.name || '-', hasPage ? 'Available' : 'Missing', resolved ? 'Approved' : 'Awaiting approval']) {
        const cell = document.createElement('td');
        cell.textContent = String(value);
        row.append(cell);
      }
      fragment.append(row);
    }
    rows.append(fragment);
    summary.textContent = `${selected.length} selected / ${ready} approved with question page / ${selected.length - ready} held back`;
  }
  [first, last, question].forEach(input => input.addEventListener('input', refresh));
  document.addEventListener('invalid', event => {
    const details = event.target.closest('details');
    if (details) details.open = true;
  }, true);
  const mail = document.querySelector('form[action$="/send"]');
  if (mail) {
    const host = mail.elements.host, password = mail.elements.password;
    const link = document.getElementById('ta-app-password-link');
    function validateMail() {
      const gmail = host.value.trim().toLowerCase().replace(/\.$/, '') === 'smtp.gmail.com';
      link.hidden = !gmail;
      const normalised = password.value.replace(/\s/g, '');
      password.setCustomValidity(gmail && password.value && !/^[A-Za-z0-9]{16}$/.test(normalised)
        ? 'Gmail requires a 16-character Google app password, not your normal email password.' : '');
    }
    [host, password].forEach(input => input.addEventListener('input', validateMail));
    validateMail();
  }
  refresh();
})();
