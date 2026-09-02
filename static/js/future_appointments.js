(() => {
  const root = document.querySelector('[data-calendar]');
  if (!root) return;

  const main = document.querySelector('.calendar-main');
  const dialog = document.querySelector('#future-appointment-dialog');
  const config = JSON.parse(root.dataset.config);
  const coveragePlans = JSON.parse(root.dataset.coveragePlans || '[]');
  const gesyReferrals = JSON.parse(root.dataset.gesyReferrals || '[]');
  const appointments = JSON.parse(main.dataset.scheduled || '[]');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  const $ = id => document.getElementById(id);
  const minutes = value => {
    const match = String(value || '').match(/^(\d{2}):(\d{2})$/);
    return match ? Number(match[1]) * 60 + Number(match[2]) : Number.NaN;
  };
  const displayDate = value => {
    const [year, month, day] = String(value).split('-');
    return `${day}/${month}/${year}`;
  };
  const isoDate = value => {
    const match = String(value).trim().match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    return match ? `${match[3]}-${match[2]}-${match[1]}` : String(value).trim();
  };
  const endTime = (start, duration) => {
    const total = minutes(start) + Number(duration);
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  };
  const slotSpan = duration => Math.max(1, Math.round(Number(duration) / Number(config.step)));
  const post = async (url, body) => {
    const response = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify(body),
    });
    return {response, data: await response.json()};
  };

  let selected = {
    patient: root.dataset.selectedPatient || null,
    history: root.dataset.selectedHistory || null,
    name: '',
    historyLabel: '',
  };
  let editing = null;
  let editingSource = null;
  let previewBlock = null;
  let previewSlot = null;
  let previewHasConflict = false;

  const overlaps = (date, time, duration, excludeId = null) => {
    const proposedStart = minutes(time);
    const proposedEnd = proposedStart + Number(duration);
    return appointments.some(item =>
      item.future_appointment_id !== excludeId &&
      item.appointment_date === date &&
      ['scheduled', 'completed'].includes(item.status) &&
      minutes(item.start_time) < proposedEnd &&
      minutes(item.start_time) + Number(item.duration_minutes) > proposedStart
    );
  };

  function appointmentBlock({name, historyLabel, start, duration, status, preview = false, conflict = false}) {
    const block = document.createElement('span');
    const person = document.createElement('strong');
    const history = document.createElement('span');
    const time = document.createElement('small');
    block.className = `appointment-block slot-span-${slotSpan(duration)}`;
    if (preview) block.classList.add('appointment-preview');
    if (conflict) block.classList.add('preview-conflict');
    if (!preview) block.classList.add(`status-${status}`);
    person.textContent = name;
    history.textContent = historyLabel || 'Χωρίς περιγραφή';
    time.textContent = `${start}–${endTime(start, duration)}`;
    block.append(person, history, time);
    return block;
  }

  function clearPreview({restoreSource = false} = {}) {
    previewBlock?.remove();
    previewSlot?.classList.remove('selected-slot');
    previewBlock = null;
    previewSlot = null;
    previewHasConflict = false;
    if (restoreSource && editingSource) editingSource.classList.remove('editing-source');
  }

  function setPreviewWarning(message = '') {
    const warning = $('future-preview-warning');
    warning.textContent = message;
    warning.hidden = !message;
  }

  function renderPreview() {
    clearPreview();
    const date = isoDate($('future-date').value);
    const time = $('future-time').value;
    const duration = Number($('future-duration').value);
    const start = minutes(time);
    const calendarStart = minutes(config.calendar_start);
    const calendarEnd = minutes(config.calendar_end);
    const validTime = Number.isFinite(start) && start >= calendarStart &&
      start + duration <= calendarEnd && (start - calendarStart) % Number(config.step) === 0;
    previewSlot = document.querySelector(`.calendar-slot[data-date="${date}"][data-time="${time}"]`);
    const blocksTime = !editing || ['scheduled', 'completed'].includes($('future-status').value);
    previewHasConflict = validTime && blocksTime && overlaps(
      date, time, duration, editing?.future_appointment_id ?? null,
    );

    if (!validTime) {
      setPreviewWarning('Το ραντεβού πρέπει να είναι εντός του ωραρίου ημερολογίου.');
      $('save-future-appointment').disabled = true;
      return;
    }
    setPreviewWarning(previewHasConflict ? 'Δεν υπάρχει διαθέσιμο συνεχόμενο διάστημα σε αυτή την ώρα.' : '');
    $('save-future-appointment').disabled = previewHasConflict;
    if (!previewSlot) return;

    editingSource?.classList.add('editing-source');
    previewSlot.classList.add('selected-slot');
    previewBlock = appointmentBlock({
      name: selected.name,
      historyLabel: selected.historyLabel,
      start: time,
      duration,
      preview: true,
      conflict: previewHasConflict,
    });
    previewSlot.append(previewBlock);
  }

  function setDuration(value) {
    $('future-duration').value = String(value);
    document.querySelectorAll('.duration-btn').forEach(button => {
      const active = button.dataset.duration === String(value);
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function showPerson() {
    $('future-patient-name').textContent = selected.name;
    $('future-history-name').textContent = selected.historyLabel || 'Χωρίς περιγραφή';
  }

  function syncCoverage() {
    const plan = coveragePlans.find(item => String(item.coverage_plan_id) === $('future-coverage').value);
    const isGesy = plan?.coverage_type === 'GESY';
    $('future-referral-label').hidden = !isGesy;
    $('future-copayment-label').hidden = !(isGesy && $('future-status').value === 'completed');
    const referralSelect = $('future-referral');
    const previous = referralSelect.value;
    referralSelect.replaceChildren(new Option('— Επιλέξτε —', ''));
    if (isGesy) {
      gesyReferrals.filter(item => String(item.history_id) === String(selected.history)).forEach(item => {
        const limit = item.allowed_visits == null ? 'άγνωστο όριο' : `${item.used_visits}/${item.allowed_visits}`;
        referralSelect.add(new Option(`${item.referral_number || `#${item.gesy_referral_id}`} — ${limit}`, item.gesy_referral_id));
      });
      referralSelect.value = previous;
    }
  }

  function selectPatient(button) {
    document.querySelectorAll('.calendar-patient').forEach(item => item.classList.remove('selected'));
    document.querySelectorAll(`.calendar-patient[data-history="${button.dataset.history}"]`).forEach(item => item.classList.add('selected'));
    selected = {
      patient: button.dataset.patient,
      history: button.dataset.history,
      name: button.dataset.name,
      historyLabel: button.dataset.historyLabel,
    };
    syncCoverage();
    $('appointment-selection').textContent = `Νέο ραντεβού για: ${selected.name} — ${selected.historyLabel}`;
    document.querySelectorAll('.calendar-actions a').forEach(link => {
      const url = new URL(link.href);
      url.searchParams.set('patient_id', selected.patient);
      url.searchParams.set('history_id', selected.history);
      link.href = url.toString();
    });
    const searchForm = document.querySelector('.calendar-search');
    ['patient_id', 'history_id'].forEach(name => {
      let input = searchForm.elements[name];
      if (!input) {
        input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        searchForm.append(input);
      }
      input.value = name === 'patient_id' ? selected.patient : selected.history;
    });
  }

  function setDialogMode(mode) {
    const creating = mode === 'create';
    dialog.classList.toggle('mode-create', creating);
    dialog.classList.toggle('mode-edit', !creating);
    $('future-dialog-title').textContent = creating ? 'Νέο ραντεβού' : 'Ραντεβού';
    $('future-open-patient').hidden = creating;
    $('future-open-history').hidden = creating;
    $('cancel-future-appointment').hidden = creating || editing?.status === 'cancelled';
    $('close-future-appointment').textContent = creating ? 'Ακύρωση' : 'Κλείσιμο';
  }

  function openNew(slot) {
    if (!selected.history) {
      alert('Επιλέξτε πρώτα ενεργό ιστορικό.');
      return;
    }
    editing = null;
    editingSource = null;
    setDialogMode('create');
    showPerson();
    $('future-date').value = displayDate(slot.dataset.date);
    $('future-time').value = slot.dataset.time;
    $('future-summary-date').textContent = $('future-date').value;
    $('future-summary-time').textContent = $('future-time').value;
    setDuration(config.default_duration);
    $('future-status').value = 'scheduled';
    $('future-coverage').value = '';
    $('future-referral').value = '';
    $('future-copayment').value = '0';
    syncCoverage();
    $('future-notes').value = '';
    dialog.showModal();
    renderPreview();
  }

  function openExisting(item, sourceBlock) {
    editing = item;
    editingSource = sourceBlock;
    selected = {
      patient: String(item.patient_id),
      history: String(item.history_id),
      name: `${item.last_name} ${item.first_name}`,
      historyLabel: item.main_diagnosis || 'Χωρίς περιγραφή',
    };
    setDialogMode('edit');
    showPerson();
    $('future-date').value = displayDate(item.appointment_date);
    $('future-time').value = item.start_time;
    setDuration(item.duration_minutes);
    $('future-status').value = item.status;
    $('future-coverage').value = item.coverage_plan_id || '';
    syncCoverage();
    $('future-referral').value = item.gesy_referral_id || '';
    $('future-copayment').value = '0';
    $('future-notes').value = item.notes || '';
    $('future-open-patient').href = `/patients/${item.patient_id}`;
    $('future-open-history').href = `/histories/${item.history_id}`;
    dialog.showModal();
    renderPreview();
  }

  function closeDialog() {
    clearPreview({restoreSource: true});
    editingSource = null;
    dialog.close();
  }

  document.querySelectorAll('.calendar-patient').forEach(button => {
    button.addEventListener('click', () => selectPatient(button));
  });
  const preselected = document.querySelector(`.calendar-patient[data-history="${selected.history}"]`);
  if (preselected) selectPatient(preselected);

  document.querySelectorAll('.calendar-slot').forEach(slot => {
    slot.addEventListener('click', event => {
      if (event.target.closest('.appointment-block')) return;
      openNew(slot);
    });
  });

  appointments.forEach(item => {
    const slot = document.querySelector(`.calendar-slot[data-date="${item.appointment_date}"][data-time="${item.start_time}"]`);
    if (!slot) return;
    const block = appointmentBlock({
      name: `${item.last_name} ${item.first_name}`,
      historyLabel: item.main_diagnosis,
      start: item.start_time,
      duration: item.duration_minutes,
      status: item.status,
    });
    block.setAttribute('role', 'button');
    block.tabIndex = 0;
    block.addEventListener('click', event => {
      event.stopPropagation();
      openExisting(item, block);
    });
    block.addEventListener('keydown', event => {
      if (!['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      openExisting(item, block);
    });
    slot.append(block);
  });

  document.querySelectorAll('.duration-btn').forEach(button => {
    button.addEventListener('click', () => {
      setDuration(button.dataset.duration);
      renderPreview();
    });
  });
  ['future-date', 'future-time'].forEach(id => {
    $(id).addEventListener('input', renderPreview);
  });
  $('future-status').addEventListener('change', () => { syncCoverage(); renderPreview(); });
  $('future-coverage').addEventListener('change', syncCoverage);
  document.querySelector('[data-close-dialog]').addEventListener('click', closeDialog);
  $('close-future-appointment').addEventListener('click', closeDialog);
  dialog.addEventListener('close', () => {
    clearPreview({restoreSource: true});
    editingSource = null;
  });

  $('save-future-appointment').addEventListener('click', async () => {
    if (previewHasConflict) return;
    const body = {
      patient_id: selected.patient,
      history_id: selected.history,
      appointment_date: isoDate($('future-date').value),
      start_time: $('future-time').value,
      duration_minutes: Number($('future-duration').value),
      status: editing ? $('future-status').value : 'scheduled',
      coverage_plan_id: $('future-coverage').value,
      gesy_referral_id: $('future-referral').value || null,
      copayment: $('future-coverage').selectedOptions[0]?.dataset.type === 'GESY' &&
        $('future-status').value === 'completed' ? $('future-copayment').value : null,
      notes: $('future-notes').value,
    };
    const url = editing ? `/api/future-appointments/${editing.future_appointment_id}` : '/api/future-appointments';
    let result = await post(url, body);
    if (result.data.requires_confirmation && confirm(result.data.message)) {
      body.confirm_second = true;
      result = await post(url, body);
    }
    if (!result.response.ok) {
      alert(result.data.error || result.data.message);
      return;
    }
    location.reload();
  });

  $('cancel-future-appointment').addEventListener('click', async () => {
    if (!editing || !confirm('Να ακυρωθεί το ραντεβού;')) return;
    const result = await post(`/api/future-appointments/${editing.future_appointment_id}/cancel`, {});
    if (!result.response.ok) {
      alert(result.data.error);
      return;
    }
    location.reload();
  });
})();
