(() => {
  const dataElement = document.getElementById('ta-email-editor-data');
  const mail = document.querySelector('form[action$="/send"]');
  if (!dataElement || !mail) return;
  const data = JSON.parse(dataElement.textContent);
  const subject = document.getElementById('ta-email-subject');
  const message = document.getElementById('ta-email-message');
  const packet = document.getElementById('ta-email-preview-packet');
  const status = document.getElementById('ta-email-preview-status');
  const send = document.getElementById('ta-send-packets');
  const initiallyDisabled = send.disabled;
  let target = message;
  let timer, controller, sequence = 0;
  function selectPreviewPacket() {
    const chosen = mail.elements.packet.value;
    const eligible = data.packets.filter(p => chosen === 'all' ? p.status !== 'sent' : p.id === chosen);
    const previous = packet.value;
    packet.replaceChildren(...eligible.map(p => new Option(p.label, p.id)));
    if (eligible.some(p => p.id === previous)) packet.value = previous;
    packet.disabled = eligible.length <= 1;
  }
  function clearPreview() {
    for (const suffix of ['to', 'subject', 'attachment', 'message']) {
      document.getElementById('ta-email-preview-' + suffix).textContent = '';
    }
  }
  async function preview() {
    const request = ++sequence;
    controller?.abort();
    controller = new AbortController();
    send.disabled = true;
    clearPreview();
    if (!packet.value) { status.textContent = 'No packet selected'; return; }
    const fields = new URLSearchParams();
    for (const name of ['output', 'packet', 'delivery', 'test_recipient', 'sender', 'subject_template', 'message_template']) {
      fields.set(name, mail.elements[name].value);
    }
    fields.set('preview_packet', packet.value);
    status.textContent = 'Updating preview';
    try {
      const response = await fetch(mail.action.replace(/\/send$/, '/preview-email'), {
        method: 'POST', body: fields, headers: {Accept: 'application/json'}, signal: controller.signal,
      });
      const result = await response.json();
      if (request !== sequence) return;
      if (!response.ok) throw new Error(result.error || 'Preview unavailable');
      subject.setCustomValidity('');
      document.getElementById('ta-email-preview-to').textContent = result.recipient || 'Not set';
      document.getElementById('ta-email-preview-subject').textContent = result.subject;
      document.getElementById('ta-email-preview-message').textContent = result.message;
      document.getElementById('ta-email-preview-attachment').textContent = result.attachment;
      status.textContent = result.selected_count > 1 ? `${result.selected_count} packets selected; previewing this packet` : 'Preview ready';
      send.disabled = initiallyDisabled || !result.recipient;
    } catch (error) {
      if (error.name === 'AbortError' || request !== sequence) return;
      status.textContent = error.message;
    }
  }
  function schedule() {
    clearTimeout(timer);
    // Invalidate any response already in flight as soon as the draft changes.
    sequence++;
    controller?.abort();
    send.disabled = true;
    clearPreview();
    timer = setTimeout(preview, 250);
  }
  [subject, message].forEach(input => {
    input.addEventListener('focus', () => { target = input; });
    input.addEventListener('input', schedule);
  });
  for (const name of ['delivery', 'test_recipient', 'sender']) mail.elements[name].addEventListener('input', schedule);
  mail.elements.packet.addEventListener('change', () => { selectPreviewPacket(); schedule(); });
  packet.addEventListener('change', schedule);
  document.getElementById('ta-insert-email-field').addEventListener('change', event => {
    const token = event.target.value;
    if (!token) return;
    target.setRangeText(token, target.selectionStart, target.selectionEnd, 'end');
    target.focus();
    target.dispatchEvent(new Event('input', {bubbles: true}));
    event.target.value = '';
  });
  document.getElementById('ta-reset-email-template').addEventListener('click', () => {
    subject.value = data.defaults.subject;
    message.value = data.defaults.message;
    schedule();
  });
  selectPreviewPacket();
  preview();
})();
