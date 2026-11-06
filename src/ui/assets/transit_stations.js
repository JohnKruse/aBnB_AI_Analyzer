// Viewport-sized OSM station queries; no tile key or paid listing API calls.
function setupBnbTransit(map, stations, savedOverlay = null) {
  const cacheKey = 'bnb-transit-stations-v1';
  const ttl = 7 * 86400000;
  const cooldownKey = cacheKey+'-cooldown';
  let cache = [], busy = false, timer, blockedUntil = 0;
  try { cache = JSON.parse(sessionStorage.getItem(cacheKey) || '[]').filter(x => x.at > Date.now()-ttl); } catch (_) {}
  try { blockedUntil = Number(sessionStorage.getItem(cooldownKey)) || 0; } catch (_) {}
  const style = document.createElement('style');
  style.textContent = '.bnb-station-icon{background:transparent;border:0}.bnb-station-badge{display:block;width:26px;height:26px;line-height:22px;text-align:center;border:2px solid white;border-radius:6px;background:#1456a0;color:white;font:bold 16px/22px sans-serif;box-shadow:0 1px 5px #0008}.bnb-station-badge.metro{background:#bd202e;border-radius:50%}.bnb-station-name{display:block;background:white;color:#172b4d;border:1px solid #9aaec6;font-weight:700;box-shadow:0 1px 3px #0003}.bnb-transit-labels .bnb-station-name{display:block}.bnb-transit-status{background:#fffffff2;padding:7px 10px;border-radius:5px;max-width:260px;font:12px/1.4 sans-serif;box-shadow:0 1px 5px #0003}';
  document.head.appendChild(style);
  const statusControl = L.control({position:'bottomleft'});
  let status;
  statusControl.onAdd = function () {
    status = L.DomUtil.create('div', 'bnb-transit-status');
    L.DomEvent.disableClickPropagation(status);
    L.DomEvent.disableScrollPropagation(status);
    return status;
  };
  statusControl.addTo(map);
  const text = message => { status.textContent = message; };
  const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function render(elements, source = '© OpenStreetMap contributors') {
    stations.clearLayers();
    const seen = [];
    for (const element of elements) {
      const tags = element.tags || {}, point = element.center || element;
      const lat = Number(point.lat), lon = Number(point.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat)>90 || Math.abs(lon)>180) continue;
      if (tags.disused === 'yes' || tags.abandoned === 'yes' || tags.construction === 'yes') continue;
      const name = tags.name || tags['name:en'] || 'Unnamed station';
      const metro = tags.station === 'subway' || tags.subway === 'yes' || /underground|metro|subway/i.test(tags.network || '');
      if (seen.some(p => p.name === name && p.metro === metro && map.distance([lat,lon],p.point)<120)) continue;
      seen.push({name, metro, point:[lat,lon]});
      const title = escapeHtml(name), mode = metro ? 'Tube / Metro' : 'Train';
      L.marker([lat,lon], {
        icon:L.divIcon({className:'bnb-station-icon', html:`<span aria-label="${mode} station">${savedOverlay?.icons?.[metro?'metro':'rail'] || (metro?'Ⓜ':'🚆')}</span>`, iconSize:[32,32],iconAnchor:[16,16]}),
        title:`${name} — ${mode}`, riseOnHover:true
      }).bindTooltip(title,{permanent:true,direction:'right',offset:[18,0],className:'bnb-station-name'})
        .bindPopup(`<b>${title}</b><br>${mode} station${tags.network?'<br>'+escapeHtml(tags.network):''}`)
        .addTo(stations);
    }
    text(`Train / Tube stations · ${seen.length} in loaded area. ${source}`);
  }
  function bbox() {
    const b = map.getBounds();
    return [b.getSouth(),b.getWest(),b.getNorth(),b.getEast()];
  }
  const contains = (outer, inner) => outer[0]<=inner[0] && outer[1]<=inner[1] && outer[2]>=inner[2] && outer[3]>=inner[3];
  async function update() {
    map.getContainer().classList.toggle('bnb-transit-labels',true);
    if (!map.hasLayer(stations)) { text('Train and Tube stations hidden. Enable them in the map layers menu.'); return; }
    const bounds = bbox();
    // London stations are already bundled with the project. Never let a stale
    // web-service cooldown suppress this available, offline station layer.
    if (savedOverlay && savedOverlay.elements.length && !(bounds[2]<savedOverlay.bounds[0] || bounds[0]>savedOverlay.bounds[2] || bounds[3]<savedOverlay.bounds[1] || bounds[1]>savedOverlay.bounds[3])) {
      const visible = savedOverlay.elements.filter(e => e.lat>=bounds[0]-.02 && e.lon>=bounds[1]-.02 && e.lat<=bounds[2]+.02 && e.lon<=bounds[3]+.02);
      render(visible,savedOverlay.source);
      return;
    }
    if (map.getZoom()<12 || bounds[2]-bounds[0]>.6 || bounds[3]-bounds[1]>1.2 || bounds[1]>bounds[3]) {
      text('No saved stations for this area. Zoom in to load train / metro stations.'); return;
    }
    const saved = cache.find(x => x.at>Date.now()-ttl && contains(x.bounds,bounds));
    if (saved) { render(saved.elements); return; }
    if (busy) return;
    if (Date.now()<blockedUntil) { text('Live stations are unavailable here. Retry after '+new Date(blockedUntil).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+'. Pan or zoom to retry.'); return; }
    // Pad and snap the viewport so small pans and Streamlit rerenders reuse data.
    const area = bounds.map((n,i)=>(i<2?Math.floor(n/.05)-1:Math.ceil(n/.05)+1)*.05);
    const query = `[out:json][timeout:20];nwr["railway"~"^(station|halt)$"](${area.join(',')});out center tags;`;
    busy = true;
    text('Loading train and Tube stations…');
    const controller = new AbortController(), timeout = setTimeout(()=>controller.abort(),25000);
    try {
      const response = await fetch('https://overpass-api.de/api/interpreter', {
        method:'POST',body:new URLSearchParams({data:query}),signal:controller.signal
      });
      if (!response.ok) {
        const delay = Number(response.headers.get('Retry-After'));
        blockedUntil = Date.now()+Math.max(60000,Number.isFinite(delay)?delay*1000:0);
        throw new Error(`Station service HTTP ${response.status}`);
      }
      const data = await response.json();
      if (!Array.isArray(data.elements) || data.remark) throw new Error('Incomplete station response');
      cache = [{bounds:area,elements:data.elements,at:Date.now()},...cache].slice(0,12);
      try { sessionStorage.setItem(cacheKey,JSON.stringify(cache)); } catch (_) {}
      if (map.hasLayer(stations)) render(data.elements);
      if (!contains(area,bbox())) schedule();
    } catch (_) {
      blockedUntil = Math.max(blockedUntil,Date.now()+60000);
      try { sessionStorage.setItem(cooldownKey,String(blockedUntil)); } catch (_) {}
      text('Stations could not load. The search map still works; pan or zoom again later to retry.');
    } finally { busy=false; clearTimeout(timeout); }
  }
  function schedule() { clearTimeout(timer); timer=setTimeout(update,1000); }
  map.on('moveend zoomend',schedule);
  map.on('overlayadd overlayremove',event=>{if(event.layer===stations) schedule();});
  map.whenReady(update);
}
