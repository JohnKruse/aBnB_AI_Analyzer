// Lay out labels in screen pixels so the same rules work at every zoom.
function layoutBnbStationLabels(stations, width, height) {
  const maxLabels = 12;
  const iconRadius = 16;
  const gap = 4;
  const margin = 3;
  const overlaps = (a, b, padding = 0) =>
    a.left < b.right + padding && a.right > b.left - padding &&
    a.top < b.bottom + padding && a.bottom > b.top - padding;
  const inside = box => box.left >= margin && box.right <= width - margin &&
    box.top >= margin && box.bottom <= height - margin;
  const icons = stations.map(({x, y}) => ({
    left: x - iconRadius, right: x + iconRadius,
    top: y - iconRadius, bottom: y + iconRadius,
  }));
  // Measure first, then write styles, to avoid forcing a layout for each station.
  const measured = stations.map(station => ({
    ...station, labelWidth: station.label.offsetWidth, labelHeight: station.label.offsetHeight,
    distance: Math.hypot(station.x - width / 2, station.y - height / 2),
  })).sort((a, b) => a.distance - b.distance);
  const placed = [];
  for (const {x, y, label, labelWidth, labelHeight} of measured) {
    const boxAt = (left, top) => ({left, top, right: left + labelWidth, bottom: top + labelHeight});
    const top = Math.max(margin, Math.min(y - labelHeight / 2, height - margin - labelHeight));
    const sides = [boxAt(x + iconRadius + gap, top), boxAt(x - iconRadius - gap - labelWidth, top)];
    const visible = x >= 0 && x <= width && y >= 0 && y <= height;
    const automatic = visible && placed.length < maxLabels && sides.find(box =>
      inside(box) && !icons.some(icon => overlaps(box, icon, 2)) &&
      !placed.some(other => overlaps(box, other, 18))
    );
    if (automatic) placed.push(automatic);
    // Give hover/focus labels a readable position even beside a map edge.
    const fallback = boxAt(
      Math.max(margin, Math.min(x - labelWidth / 2, width - margin - labelWidth)),
      y >= height / 2 ? y - iconRadius - gap - labelHeight : y + iconRadius + gap,
    );
    const box = automatic || sides.find(inside) || fallback;
    label.style.left = (box.left - x + iconRadius) + 'px';
    label.style.top = (box.top - y + iconRadius) + 'px';
    label.classList.toggle('station-label-visible', Boolean(automatic));
  }
}
