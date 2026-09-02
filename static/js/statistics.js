document.querySelectorAll('.bar-chart[data-chart]').forEach((chart) => {
  let values = [];
  try { values = JSON.parse(chart.dataset.chart || '[]'); } catch (_) { return; }
  const max = Math.max(1, ...values.map((item) => Number(item.value) || 0));
  chart.replaceChildren(...values.map((item) => {
    const row = document.createElement('div'); row.className = 'bar-row';
    const label = document.createElement('span'); label.textContent = item.label;
    const track = document.createElement('div'); track.className = 'bar-track';
    const bar = document.createElement('div'); bar.className = 'bar-value'; bar.style.width = `${((Number(item.value) || 0) / max) * 100}%`;
    const value = document.createElement('strong'); value.textContent = item.value;
    track.append(bar); row.append(label, track, value); return row;
  }));
});
