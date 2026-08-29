const statusBox = document.getElementById('save-status');
const toastBox = document.getElementById('toast');
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
const dirtyFormStates = new Map();
let undoBusy = false;
let pendingSaves = 0;
let suppressDirtyWarning = false;

function setStatus(text, cls = '') {
  if (!statusBox) return;
  statusBox.textContent = text;
  statusBox.className = `save-status ${cls}`;
}

function toast(text) {
  if (!toastBox) return;
  toastBox.textContent = text;
  toastBox.classList.add('show');
  window.setTimeout(() => toastBox.classList.remove('show'), 2600);
}

async function postJSON(url, data = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
    body: JSON.stringify(data),
    credentials: 'same-origin',
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Σφάλμα ${response.status}`);
  return body;
}

function valueOf(element) {
  if (element.type === 'checkbox') return element.checked ? 1 : 0;
  return element.value;
}

function syncElementMarkup(element) {
  if (element.type === 'checkbox') {
    element.toggleAttribute('checked', element.checked);
  } else if (element.tagName === 'TEXTAREA') {
    element.textContent = element.value;
  } else if (element.tagName !== 'SELECT') {
    element.setAttribute('value', element.value);
  }
}

function rememberOriginal(element) {
  element.dataset.original = String(valueOf(element) ?? '');
  element.classList.remove('field-error');
  syncElementMarkup(element);
}

function restoreOriginal(element, notify = true) {
  if (element.dataset.original === undefined) return false;
  const current = String(valueOf(element) ?? '');
  if (current === element.dataset.original) return false;
  if (element.type === 'checkbox') element.checked = element.dataset.original === '1';
  else element.value = element.dataset.original;
  syncElementMarkup(element);
  element.classList.remove('field-error');
  if (notify) toast('Η μη αποθηκευμένη αλλαγή ακυρώθηκε');
  return true;
}

function autosaveElementIsDirty(element) {
  return element.dataset.original !== undefined &&
    String(valueOf(element) ?? '') !== element.dataset.original;
}

async function saveElement(element) {
  const {table, pk, column} = element.dataset;
  if (!table || !pk || !column) return;
  pendingSaves += 1;
  setStatus('Αποθήκευση…', 'saving');
  element.disabled = true;
  try {
    await postJSON('/api/update', {table, pk, column, value: valueOf(element)});
    rememberOriginal(element);
    setStatus('Αποθηκεύτηκε', 'saved');
  } catch (error) {
    restoreOriginal(element, false);
    element.classList.add('field-error');
    setStatus('Δεν αποθηκεύτηκε', 'error');
    toast(error.message);
  } finally {
    pendingSaves = Math.max(0, pendingSaves - 1);
    element.disabled = false;
  }
}

function bindAutosave(element) {
  if (element.dataset.autosaveBound === 'true') return;
  element.dataset.autosaveBound = 'true';
  rememberOriginal(element);
  element.addEventListener('focus', () => {
    if (!autosaveElementIsDirty(element)) rememberOriginal(element);
  });
  const eventName = ['SELECT', 'INPUT'].includes(element.tagName) &&
    (element.tagName === 'SELECT' || element.type === 'checkbox' || element.type === 'date') ? 'change' : 'blur';
  element.addEventListener(eventName, () => saveElement(element));
}

async function ensurePayment(element) {
  if (element.dataset.payment) return element.dataset.payment;
  const body = await postJSON(`/api/payment/ensure/${element.dataset.appointment}`);
  const paymentId = body.payment_id;
  document.querySelectorAll(`.payment-input[data-appointment="${element.dataset.appointment}"]`)
    .forEach(input => { input.dataset.payment = paymentId; });
  return paymentId;
}

function bindPayment(element) {
  if (element.dataset.paymentBound === 'true') return;
  element.dataset.paymentBound = 'true';
  rememberOriginal(element);
  element.addEventListener('focus', () => {
    if (!autosaveElementIsDirty(element)) rememberOriginal(element);
  });
  element.addEventListener('blur', async () => {
    pendingSaves += 1;
    element.disabled = true;
    try {
      const paymentId = await ensurePayment(element);
      setStatus('Αποθήκευση…', 'saving');
      await postJSON('/api/update', {
        table: 'payments', pk: paymentId,
        column: element.dataset.column, value: element.value,
      });
      rememberOriginal(element);
      setStatus('Αποθηκεύτηκε', 'saved');
    } catch (error) {
      restoreOriginal(element, false);
      element.classList.add('field-error');
      setStatus('Δεν αποθηκεύτηκε', 'error');
      toast(error.message);
    } finally {
      pendingSaves = Math.max(0, pendingSaves - 1);
      element.disabled = false;
    }
  });
}

function initializeInteractiveElements(root = document) {
  root.querySelectorAll('.autosave').forEach(bindAutosave);
  root.querySelectorAll('.payment-input').forEach(bindPayment);
}

function formSnapshot(form) {
  return Array.from(form.elements)
    .filter(element => element.name && element.name !== 'csrf_token')
    .map(element => {
      const value = (element.type === 'checkbox' || element.type === 'radio')
        ? `${element.name}:${element.checked ? '1' : '0'}`
        : `${element.name}:${element.value}`;
      return value;
    })
    .join('\u001f');
}

function bindDirtyForm(form) {
  const state = {
    initial: formSnapshot(form),
    dirty: form.dataset.unsaved === 'true',
    forced: form.dataset.unsaved === 'true',
    submitted: false,
  };
  dirtyFormStates.set(form, state);
  const update = () => {
    state.dirty = state.forced || formSnapshot(form) !== state.initial;
  };
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  form.addEventListener('submit', () => {
    state.dirty = false;
    state.forced = false;
    state.submitted = true;
    suppressDirtyWarning = true;
  });
}

function hasUnsavedChanges() {
  if (suppressDirtyWarning) return false;
  if (pendingSaves > 0) return true;
  for (const state of dirtyFormStates.values()) {
    if (state.dirty && !state.submitted) return true;
  }
  return Array.from(document.querySelectorAll('.autosave, .payment-input'))
    .some(autosaveElementIsDirty);
}

function confirmInternalNavigation() {
  if (!hasUnsavedChanges()) return true;
  return window.confirm('Υπάρχουν αλλαγές που δεν έχουν αποθηκευτεί. Θέλετε να φύγετε χωρίς αποθήκευση;');
}

window.addEventListener('beforeunload', event => {
  if (!hasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = '';
});

document.addEventListener('click', event => {
  const link = event.target.closest('a[href]');
  if (!link || event.defaultPrevented || link.target === '_blank' || link.hasAttribute('download')) return;
  const target = new URL(link.href, window.location.href);
  if (target.origin !== window.location.origin) return;
  if (target.pathname === window.location.pathname && target.search === window.location.search && target.hash) return;
  if (!confirmInternalNavigation()) {
    event.preventDefault();
    event.stopImmediatePropagation();
    return;
  }
  suppressDirtyWarning = true;
}, true);

function bindAutocomplete(input) {
  if (input.dataset.autocompleteBound === 'true') return;
  input.dataset.autocompleteBound = 'true';
  const control = input.closest('.autocomplete-control');
  const menu = control?.querySelector('.autocomplete-menu');
  const hiddenId = control?.querySelector('[data-autocomplete-id]');
  if (!menu) return;

  let suggestions = [];
  let activeIndex = -1;
  let timer = null;
  let controller = null;

  const close = () => {
    menu.hidden = true;
    menu.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    suggestions = [];
    activeIndex = -1;
  };

  const activate = index => {
    if (!suggestions.length) return;
    activeIndex = (index + suggestions.length) % suggestions.length;
    menu.querySelectorAll('.autocomplete-option').forEach((option, optionIndex) => {
      option.classList.toggle('active', optionIndex === activeIndex);
      option.setAttribute('aria-selected', String(optionIndex === activeIndex));
    });
    const active = menu.children[activeIndex];
    if (active) {
      input.setAttribute('aria-activedescendant', active.id);
      active.scrollIntoView({block: 'nearest'});
    }
  };

  const choose = index => {
    const selected = suggestions[index];
    if (!selected) return;
    input.value = selected.value;
    if (hiddenId) hiddenId.value = selected.id ?? '';
    close();
    input.dispatchEvent(new Event('change', {bubbles: true}));
  };

  const render = items => {
    suggestions = items;
    activeIndex = -1;
    menu.replaceChildren();
    items.forEach((item, index) => {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'autocomplete-option';
      option.id = `${menu.id}-option-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');
      option.textContent = item.value;
      option.addEventListener('pointerdown', event => {
        event.preventDefault();
        choose(index);
      });
      menu.appendChild(option);
    });
    menu.hidden = items.length === 0;
    input.setAttribute('aria-expanded', String(items.length > 0));
  };

  const load = async () => {
    const query = input.value.trim();
    if (!query) {
      close();
      return;
    }
    controller?.abort();
    controller = new AbortController();
    const url = new URL('/api/autocomplete', window.location.origin);
    url.searchParams.set('field', input.dataset.autocomplete);
    url.searchParams.set('q', query);
    try {
      const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `Σφάλμα ${response.status}`);
      if (input.value.trim() === query) render(body.suggestions || []);
    } catch (error) {
      if (error.name !== 'AbortError') close();
    }
  };

  input.addEventListener('input', () => {
    if (hiddenId) hiddenId.value = '';
    window.clearTimeout(timer);
    timer = window.setTimeout(load, 160);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' && suggestions.length) {
      event.preventDefault();
      activate(activeIndex + 1);
    } else if (event.key === 'ArrowUp' && suggestions.length) {
      event.preventDefault();
      activate(activeIndex < 0 ? suggestions.length - 1 : activeIndex - 1);
    } else if (event.key === 'Enter' && activeIndex >= 0) {
      event.preventDefault();
      choose(activeIndex);
    } else if (event.key === 'Escape' && !menu.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  });
  input.addEventListener('blur', () => window.setTimeout(close, 120));
}

document.querySelectorAll('[data-autocomplete]').forEach(bindAutocomplete);
document.querySelectorAll('[data-dirty-form]').forEach(bindDirtyForm);
initializeInteractiveElements();

document.addEventListener('click', async event => {
  const activateButton = event.target.closest('.activate-history');
  const deactivateButton = event.target.closest('.deactivate-history');
  const button = activateButton || deactivateButton;
  if (!button) return;
  const activating = Boolean(activateButton);
  const question = activating
    ? 'Να ενεργοποιηθεί αυτό το ιστορικό;'
    : 'Να απενεργοποιηθεί αυτό το ιστορικό;';
  if (!window.confirm(question)) return;
  button.disabled = true;
  try {
    await postJSON(`/api/${activating ? 'activate' : 'deactivate'}/${button.dataset.history}`);
    suppressDirtyWarning = true;
    window.location.reload();
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
});

const newAppointment = document.getElementById('new-appointment');
if (newAppointment) newAppointment.addEventListener('click', async () => {
  if (!confirmInternalNavigation()) return;
  newAppointment.disabled = true;
  try {
    await postJSON(`/api/appointment/new/${newAppointment.dataset.history}`);
    suppressDirtyWarning = true;
    window.location.reload();
  } catch (error) {
    newAppointment.disabled = false;
    toast(error.message);
  }
});

const defaultAmount = document.getElementById('default-amount');
if (defaultAmount) defaultAmount.addEventListener('change', async () => {
  const original = defaultAmount.defaultValue;
  pendingSaves += 1;
  try {
    const result = await postJSON('/api/settings', {default_amount_due: defaultAmount.value});
    defaultAmount.value = result.value;
    defaultAmount.defaultValue = result.value;
    toast('Το προεπιλεγμένο ποσό αποθηκεύτηκε');
  } catch (error) {
    defaultAmount.value = original;
    toast(error.message);
  } finally {
    pendingSaves = Math.max(0, pendingSaves - 1);
  }
});

const backupButton = document.getElementById('create-backup');
if (backupButton) backupButton.addEventListener('click', async () => {
  backupButton.disabled = true;
  try {
    const result = await postJSON('/api/backup');
    const latest = document.getElementById('backup-latest');
    if (latest) latest.textContent = `${result.created_at} (${result.filename})`;
    toast('Το backup δημιουργήθηκε επιτυχώς');
  } catch (error) {
    toast(error.message);
  } finally {
    backupButton.disabled = false;
  }
});

async function performUndo() {
  if (undoBusy) return;
  undoBusy = true;
  try {
    const preview = await fetch('/api/undo/peek', {credentials: 'same-origin'});
    const previewBody = await preview.json().catch(() => ({}));
    if (!preview.ok) throw new Error(previewBody.error || 'Δεν υπάρχει αλλαγή για αναίρεση');
    if (!window.confirm(`Να αναιρεθεί η αλλαγή;\n\n${previewBody.description}`)) return;
    const result = await postJSON('/api/undo');
    toast(result.message || 'Η αλλαγή αναιρέθηκε');
    suppressDirtyWarning = true;
    window.setTimeout(() => window.location.reload(), 350);
  } catch (error) {
    toast(error.message);
  } finally {
    undoBusy = false;
  }
}

document.getElementById('undo-button')?.addEventListener('click', performUndo);

document.addEventListener('keydown', async event => {
  if (event.key !== 'Escape' || undoBusy || event.defaultPrevented) return;
  const active = document.activeElement;
  if (active && (active.classList?.contains('autosave') || active.classList?.contains('payment-input'))) {
    if (restoreOriginal(active)) {
      event.preventDefault();
      return;
    }
  }
  event.preventDefault();
  await performUndo();
});

const navToggle = document.getElementById('nav-toggle');
const mainNav = document.getElementById('main-nav');
navToggle?.addEventListener('click', () => {
  const open = mainNav?.classList.toggle('open') || false;
  navToggle.setAttribute('aria-expanded', String(open));
});

function parseListRows(html) {
  const template = document.createElement('template');
  template.innerHTML = `<table><tbody>${html}</tbody></table>`;
  return Array.from(template.content.querySelectorAll('tr[data-list-row]'));
}

function bindInfiniteList(body) {
  const status = body.closest('.panel')?.querySelector('[data-infinite-status]');
  const topSpacer = body.querySelector('[data-infinite-top]');
  const bottomSpacer = body.querySelector('[data-infinite-bottom]');
  const initialRows = Array.from(body.querySelectorAll(':scope > tr[data-list-row]'));
  if (!topSpacer || !bottomSpacer || !status) return;

  const maxBlocks = 5;
  const loadedBlocks = initialRows.length ? [{offset: 0, rows: initialRows, height: 0}] : [];
  const previousBlocks = [];
  const nextBlocks = [];
  let nextOffset = Number(body.dataset.nextOffset || initialRows.length);
  let hasMore = body.dataset.hasMore === 'true';
  let topHeight = 0;
  let bottomHeight = 0;
  let loading = false;
  let lastScrollY = window.scrollY;
  let scrollScheduled = false;

  const updateSpacer = (spacer, height) => {
    spacer.hidden = height <= 0;
    spacer.style.height = height > 0 ? `${Math.round(height)}px` : '';
  };

  const updateStatus = () => {
    status.classList.toggle('loading', loading);
    if (loading) status.textContent = 'Φόρτωση εγγραφών…';
    else if (nextBlocks.length || hasMore) status.textContent = 'Κυλήστε για αυτόματη φόρτωση περισσότερων εγγραφών.';
    else status.textContent = 'Όλες οι εγγραφές φορτώθηκαν.';
  };

  const blockHasDirtyState = block => block.rows.some(row =>
    Array.from(row.querySelectorAll('.autosave, .payment-input')).some(element =>
      element.disabled || autosaveElementIsDirty(element)
    )
  );

  const detachBlock = block => {
    if (blockHasDirtyState(block)) return false;
    block.height = block.rows.reduce(
      (height, row) => height + row.getBoundingClientRect().height, 0,
    ) || block.rows.length * 42;
    block.rows.forEach(row => row.remove());
    block.rows = [];
    return true;
  };

  const trimTop = () => {
    while (loadedBlocks.length > maxBlocks) {
      const block = loadedBlocks[0];
      if (!detachBlock(block)) break;
      loadedBlocks.shift();
      previousBlocks.push(block);
      topHeight += block.height;
      updateSpacer(topSpacer, topHeight);
    }
  };

  const trimBottom = () => {
    while (loadedBlocks.length > maxBlocks) {
      const block = loadedBlocks[loadedBlocks.length - 1];
      if (!detachBlock(block)) break;
      loadedBlocks.pop();
      nextBlocks.push(block);
      bottomHeight += block.height;
      updateSpacer(bottomSpacer, bottomHeight);
    }
  };

  const fetchBlock = async offset => {
    const url = new URL(window.location.href);
    url.searchParams.delete('page');
    url.searchParams.set('format', 'json');
    url.searchParams.set('offset', String(offset));
    const response = await fetch(url, {credentials: 'same-origin'});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `Σφάλμα ${response.status}`);
    return result;
  };

  const insertBlock = (block, html, atTop) => {
    const rows = parseListRows(html);
    block.rows = rows;
    if (atTop) topSpacer.after(...rows);
    else bottomSpacer.before(...rows);
    initializeInteractiveElements(body);
    return rows.length;
  };

  const loadNext = async () => {
    if (loading || (!nextBlocks.length && !hasMore)) return;
    loading = true;
    updateStatus();
    try {
      if (nextBlocks.length) {
        const block = nextBlocks[nextBlocks.length - 1];
        const result = await fetchBlock(block.offset);
        nextBlocks.pop();
        bottomHeight = Math.max(0, bottomHeight - block.height);
        updateSpacer(bottomSpacer, bottomHeight);
        insertBlock(block, result.html || '', false);
        loadedBlocks.push(block);
      } else {
        const offset = nextOffset;
        const result = await fetchBlock(offset);
        hasMore = Boolean(result.has_more);
        nextOffset = Number(result.next_offset ?? offset);
        if (result.count > 0) {
          const block = {offset, rows: [], height: 0};
          insertBlock(block, result.html || '', false);
          loadedBlocks.push(block);
        }
      }
      trimTop();
    } catch (error) {
      status.textContent = error.message;
      toast(error.message);
    } finally {
      loading = false;
      updateStatus();
      scheduleScrollCheck();
    }
  };

  const loadPrevious = async () => {
    if (loading || !previousBlocks.length) return;
    loading = true;
    updateStatus();
    try {
      const block = previousBlocks[previousBlocks.length - 1];
      const result = await fetchBlock(block.offset);
      previousBlocks.pop();
      topHeight = Math.max(0, topHeight - block.height);
      updateSpacer(topSpacer, topHeight);
      insertBlock(block, result.html || '', true);
      loadedBlocks.unshift(block);
      trimBottom();
    } catch (error) {
      status.textContent = error.message;
      toast(error.message);
    } finally {
      loading = false;
      updateStatus();
      scheduleScrollCheck();
    }
  };

  const checkScroll = () => {
    scrollScheduled = false;
    const currentY = window.scrollY;
    const direction = Math.sign(currentY - lastScrollY);
    lastScrollY = currentY;
    const firstRow = loadedBlocks[0]?.rows[0];
    const lastBlock = loadedBlocks[loadedBlocks.length - 1];
    const lastRow = lastBlock?.rows[lastBlock.rows.length - 1];

    if (direction < 0 && previousBlocks.length && firstRow && firstRow.getBoundingClientRect().top > -900) {
      loadPrevious();
      return;
    }
    if ((direction >= 0 || document.documentElement.scrollHeight <= window.innerHeight + 900) &&
        (nextBlocks.length || hasMore) && lastRow && lastRow.getBoundingClientRect().bottom < window.innerHeight + 900) {
      loadNext();
    }
  };

  function scheduleScrollCheck() {
    if (scrollScheduled) return;
    scrollScheduled = true;
    window.requestAnimationFrame(checkScroll);
  }

  window.addEventListener('scroll', scheduleScrollCheck, {passive: true});
  window.addEventListener('resize', scheduleScrollCheck);
  updateStatus();
  scheduleScrollCheck();
}

document.querySelectorAll('[data-infinite-list]').forEach(bindInfiniteList);
