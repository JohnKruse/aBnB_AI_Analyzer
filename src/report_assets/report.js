(() => {
'use strict';
const $ = id => document.getElementById(id);
const report = JSON.parse($('report-data').textContent);
const state = report.state;
state.notes ||= {}; state.filters ||= {}; state.favorites ||= [];
const favorites = new Set(state.favorites);
const rows = report.rows;
const currency = report.trip.currency || 'EUR';
const number = value => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
const text = value => value === null || value === undefined ? '' : String(value);
const esc = value => text(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const nested = value => { if (typeof value !== 'string') return value; try { return JSON.parse(value); } catch { return null; } };
const money = value => { const n = number(value); if (n === null) return 'Unknown'; try { return new Intl.NumberFormat(undefined,{style:'currency',currency,maximumFractionDigits:0}).format(n); } catch { return `${currency} ${n.toFixed(0)}`; } };
const countText = value => number(value) === null ? 'Unknown' : text(value);
const cost = row => row.quote_status_ui === 'current' && number(row.verified_total_price) > 0 ? number(row.verified_total_price) : null;
const fit = row => row.personal_fit_status === 'scored' ? number(row.personal_fit) : null;
const dateText = value => { if (!value) return 'Unknown'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? text(value) : d.toLocaleString(); };
const knownLocation = row => number(row.latitude) !== null && number(row.longitude) !== null && Math.abs(number(row.latitude)) <= 85.051129 && Math.abs(number(row.longitude)) <= 180;
const filters = ['query','max-price','capacity','beds','bedrooms','rating','home','bath','ac','washer','limit','favorites-only','cancellation','unknown-prices','unavailable'];
const sortColumns = [
  {key:'select',label:'Select'}, {key:'link',label:'Airbnb'},
  {key:'cost',label:'Stay cost',value:cost},
  {key:'rating',label:'Airbnb ★',value:r=>number(r.Airbnb_rating)},
  {key:'fit',label:'AI fit / 5',value:fit},
  {key:'name',label:'Property',value:r=>text(r.name || r.title)},
  {key:'bedrooms',label:'Bedrooms',value:r=>number(r.bedroom_count)},
  {key:'beds',label:'Beds',value:r=>number(r.bed_count)},
  {key:'guests',label:'Guests',value:r=>number(r.person_capacity)},
  {key:'home',label:'Home type',value:r=>text(r.accommodation_type || 'Unknown')},
  {key:'bath',label:'Bathroom',value:r=>text(r.bathroom_privacy || 'Unknown')},
  {key:'favorite',label:'Favorite'}
];
let sort = state.sort || {key:"fit",direction:-1};
let shown = [];
$('report-title').textContent = report.title;
document.title = report.title;
$('created').textContent = `Created ${dateText(report.created_at)}`;
const trip = report.trip;
let nights = '';
if (trip.check_in && trip.check_out) { const n = (Date.parse(trip.check_out)-Date.parse(trip.check_in))/86400000; if (n > 0) nights = ` · ${n} nights`; }
$('trip').textContent = `${trip.location || report.search_name} · ${trip.check_in || 'Check-in unknown'} → ${trip.check_out || 'Check-out unknown'}${nights} · ${trip.adults ?? 0} adults · ${trip.children ?? 0} children · ${trip.infants ?? 0} infants · ${trip.pets ?? 0} pets · ${currency}`;
const latest = field => rows.map(r=>text(r[field])).filter(Boolean).sort().at(-1);
$('provenance').textContent = `Profile: ${report.profile.name || 'Default'} · Model: ${report.profile.model_name || 'Unknown'} · Reasoning: ${report.profile.reasoning_effort || 'Provider default'} · Latest details: ${dateText(latest('details_fetched_at'))} · Latest evaluation: ${dateText(latest('personal_fit_evaluated_at') || latest('personal_fit_updated_at'))}`;
const anyHome = document.createElement('option'); anyHome.value = ''; anyHome.textContent = 'Any';
$('home').replaceChildren(anyHome);
for (const type of [...new Set(rows.map(r=>r.accommodation_type || 'Unknown'))].sort()) {
  const option = document.createElement('option'); option.value = type; option.textContent = type; $('home').append(option);
}
if (trip.processing_max_total && !Object.hasOwn(state.filters,'max-price')) $('max-price').value = trip.processing_max_total;
for (const id of filters) {
  const el = $(id);
  if (Object.hasOwn(state.filters,id)) { if (el.type === 'checkbox') el.checked = Boolean(state.filters[id]); else el.value = state.filters[id]; }
  el.addEventListener('input',()=>{ rememberFilters(); render(); });
}
function rememberFilters() { for (const id of filters) state.filters[id] = $(id).type === 'checkbox' ? $(id).checked : $(id).value; }
function passes(row) {
  const query = $('query').value.trim().toLocaleLowerCase();
  if (query && ![row.name,row.title,row.description,row.reviews_text,row.AI_review_summary].map(text).join(' ').toLocaleLowerCase().includes(query)) return false;
  const price = cost(row);
  if (price === null && !$('unknown-prices').checked) return false;
  if (price !== null && $('max-price').value !== '' && price > Number($('max-price').value)) return false;
  if (!$('unavailable').checked && (['unavailable','not_returned'].includes(row.availability_state) || row.quote_status_ui === 'unavailable')) return false;
  for (const [id,field] of [['capacity','person_capacity'],['beds','bed_count'],['bedrooms','bedroom_count'],['rating','Airbnb_rating']]) {
    const minimum = Number($(id).value); if (minimum > 0 && (number(row[field]) === null || number(row[field]) < minimum)) return false;
  }
  if ($('home').value && (row.accommodation_type || 'Unknown') !== $('home').value) return false;
  const bath = $('bath').value;
  const bathState = nested(row.bathroom_evidence)?.state;
  if (bath === 'exclusive' && !['ensuite_exclusive','external_exclusive'].includes(bathState)) return false;
  if (bath === 'ensuite' && bathState !== 'ensuite_exclusive') return false;
  if (bath === 'shared' && bathState !== 'shared') return false;
  for (const [id,field] of [['ac','ac_state'],['washer','washer_state']]) {
    const mode = $(id).value;
    if (mode === 'present' && row[field] !== 'present') return false;
    if (mode === 'not_absent' && !['present','unknown',null,undefined].includes(row[field])) return false;
  }
  const cancellation = $('cancellation').value;
  if (cancellation === 'conflict' ? !nested(row.cancellation_evidence)?.conflict : (cancellation && (row.cancellation_state || 'unknown') !== cancellation)) return false;
  return !$('favorites-only').checked || favorites.has(row.room_id);
}
function renderHeaders() {
  $('columns').innerHTML = sortColumns.map(col => `<th scope="col"${col.value ? ` aria-sort="${sort.key === col.key ? (sort.direction === 1 ? 'ascending':'descending'):'none'}"` : ''}>${col.value ? `<button data-sort="${col.key}">${esc(col.label)}${sort.key === col.key ? (sort.direction === 1 ? ' ↑':' ↓'):''}</button>` : `<span>${esc(col.label)}</span>`}</th>`).join('');
  $('columns').querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click',()=>{
    const key = button.dataset.sort;
    sort = {key,direction:sort.key === key ? -sort.direction : (['rating','fit'].includes(key) ? -1:1)};
    state.sort = sort; render();
  }));
}
function render() {
  let matches = rows.filter(passes);
  const col = sortColumns.find(c=>c.key === sort.key);
  if (col?.value) matches.sort((a,b)=>{
    const av=col.value(a),bv=col.value(b);
    if (av === null && bv === null) return 0;
    if (av === null) return 1; if (bv === null) return -1;
    return (typeof av === 'number' ? av-bv : av.localeCompare(bv)) * sort.direction;
  });
  const limit = Number($('limit').value);
  shown = limit > 0 ? matches.slice(0,limit) : matches;
  if (!shown.some(row=>row.room_id === state.selected)) state.selected = null;
  renderHeaders();
  $('count').textContent = `${shown.length} shown · ${matches.length} matching · ${rows.length} in report · ${favorites.size} favorites`;
  $('rows').innerHTML = shown.map(row=>{
    const rid=esc(row.room_id), selected=state.selected === row.room_id;
    return `<tr data-id="${rid}" class="${selected ? 'selected':''}"><td><input type="checkbox" data-select="${rid}" aria-label="Select ${esc(row.name || row.title || row.room_id)}" ${selected ? 'checked':''}></td><td><a href="${esc(row.airbnb_url)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${esc(row.name || row.room_id)} on Airbnb">↗</a></td><td title="${esc(row.quote_status_ui)}">${cost(row) === null ? 'Unknown / stale' : esc(money(cost(row)))}</td><td>${number(row.Airbnb_rating)?.toFixed(2) ?? '—'}</td><td>${fit(row)?.toFixed(2) ?? '—'}</td><td class="title">${esc(row.name || row.title || row.room_id)}</td><td>${esc(countText(row.bedroom_count))}</td><td>${esc(countText(row.bed_count))}</td><td>${esc(countText(row.person_capacity))}</td><td>${esc(row.accommodation_type || 'Unknown')}</td><td>${esc(row.bathroom_privacy || 'Unknown')}</td><td><button class="star" data-favorite="${rid}" aria-label="Favorite ${esc(row.name || row.room_id)}" aria-pressed="${favorites.has(row.room_id)}">${favorites.has(row.room_id) ? '★':'☆'}</button></td></tr>`;
  }).join('') || '<tr><td colspan="12" class="empty">No properties match. Adjust the filters above.</td></tr>';
  $('rows').querySelectorAll('[data-select]').forEach(el=>el.addEventListener('change',()=>select(el.checked ? el.dataset.select : null)));
  $('rows').querySelectorAll('[data-favorite]').forEach(el=>el.addEventListener('click',()=>toggleFavorite(el.dataset.favorite)));
  $('rows').querySelectorAll('tr[data-id]').forEach(el=>el.addEventListener('click',event=>{ if (!event.target.closest('input,a,button')) select(el.dataset.id); }));
  renderDetails(); renderMap();
}
function select(id) { state.selected = id; render(); }
function toggleFavorite(id) { if (favorites.has(id)) favorites.delete(id); else favorites.add(id); state.favorites = [...favorites]; render(); }
function evidenceHtml(row) {
  return ['bathroom_evidence','ac_evidence','washer_evidence','bed_count_evidence','bedroom_count_evidence','cancellation_evidence'].map(key=>{
    const fact = nested(row[key]); if (!fact || typeof fact !== 'object') return '';
    const evidence = Array.isArray(fact.evidence) ? fact.evidence : [];
    return `<details class="evidence"><summary>${esc(key.replaceAll('_',' '))}: ${esc(fact.state || 'Unknown')}${fact.conflict ? ' · conflicting evidence':''}</summary>${evidence.map(e=>`<p class="small">${esc(e.source)}: ${esc(e.text)}</p>`).join('')}</details>`;
  }).join('');
}
function renderDetails() {
  const row = rows.find(r=>r.room_id === state.selected);
  if (!row) { $('details').innerHTML = '<p>Check a property in the grid or click its map marker to see full details here.</p>'; return; }
  const breakdown = nested(row.personal_fit_breakdown);
  const findings = breakdown && typeof breakdown === 'object' ? Object.entries(breakdown).filter(([,v])=>v && typeof v === 'object').map(([key,finding])=>{
    const evidence = Array.isArray(finding.evidence) ? finding.evidence : [];
    return `<div class="evidence"><strong>${esc(key.replaceAll('_',' '))} · ${esc(finding.score ?? 'Not assessed')}</strong><p>${esc(finding.rationale)}</p>${evidence.map(e=>`<blockquote>${esc(e.quote)}<br><small>${esc(e.source_id)}</small></blockquote>`).join('')}</div>`;
  }).join('') : '<p>No criterion breakdown was collected.</p>';
  const reviews = nested(row.personal_fit_review_coverage);
  $('details').innerHTML = `<div class="detail-head"><div><p class="eyebrow">SELECTED PROPERTY · ${esc(row.room_id)}</p><h2>${esc(row.name || row.title || 'Property')}</h2><p>${esc(row.accommodation_type || 'Unknown home type')} · ${esc(row.bathroom_privacy || 'Unknown bathroom')} · Cancellation: ${esc(row.cancellation_state || 'unknown')} · Discovery: ${esc(row.discovery_state === 'not_returned' ? 'Not returned in latest search (availability unconfirmed)' : 'Returned in latest search')}</p></div><a href="${esc(row.airbnb_url)}" target="_blank" rel="noopener noreferrer">Open on Airbnb ↗</a></div>
    <div class="metrics"><div class="metric"><strong>${esc(money(cost(row)))}</strong><span>FULL STAY · ${esc(row.quote_status_ui || 'unknown quote status')}</span></div><div class="metric"><strong>${number(row.Airbnb_rating)?.toFixed(2) ?? '—'}</strong><span>AIRBNB RATING</span></div><div class="metric"><strong>${fit(row)?.toFixed(2) ?? '—'}</strong><span>AI FIT / 5 · ${esc(row.personal_fit_status || 'missing')}</span></div><div class="metric"><strong>${esc(countText(row.bedroom_count))} / ${esc(countText(row.bed_count))}</strong><span>BEDROOMS / BEDS · SLEEPS ${esc(countText(row.person_capacity))}</span></div></div>
    <p class="small">Availability: ${esc(row.availability_state || 'unknown')} · Quote fetched: ${esc(dateText(row.quote_fetched_at || row.details_fetched_at))} · Quote source: ${esc(row.quote_source || 'unknown')} · Total completeness: ${esc(row.quote_total_completeness || 'unknown')}. The stay amount is the collected Airbnb quote; it does not certify the final checkout total.</p>
    ${row.personal_fit_status === 'stale' ? '<p class="status">Prior AI assessment is stale; it is excluded from the score and sort order.</p>':''}
    <div class="detail-columns"><div><h3>Description</h3><div class="prose">${esc(row.description || 'No description collected.')}</div><h3>Facilities and evidence</h3>${evidenceHtml(row)}<h3>Your notes</h3><textarea id="property-notes" class="notes" aria-label="Notes for selected property" placeholder="Your comments and questions"></textarea><p class="muted">Use Save reviewed copy to keep notes and favorites after closing this file.</p><button id="detail-favorite">${favorites.has(row.room_id) ? '★ Remove favorite':'☆ Add favorite'}</button></div>
    <div><h3>AI evaluation</h3><p class="small">Saved assessment date: ${esc(dateText(row.personal_fit_evaluated_at))}. Assessments and value findings use the collected evidence snapshot; the displayed stay quote may have been refreshed later.</p><p class="small">Profile: ${esc(row.personal_fit_profile || report.profile.name)} · Coverage: ${number(row.coverage ?? row.personal_fit_coverage) === null ? 'Unknown' : `${Math.round(Number(row.coverage ?? row.personal_fit_coverage)*100)}%`}</p>${findings}<h3>Review summary</h3><p class="small">Collection window: ${esc(row.reviews_cutoff ? `since ${row.reviews_cutoff}` : 'all recorded dates')} · Review cap: ${esc(row.reviews_limit || 'not recorded')} · Collected: ${esc(countText(row.reviews_retrieved_count))}</p><div class="prose">${esc(row.AI_review_summary || row.review_summary || 'No summary collected.')}</div><details class="evidence"><summary>Collected guest reviews</summary><div class="prose">${esc(row.reviews_text || 'No review text collected.')}</div></details>${reviews ? `<p class="small">Reviews retrieved: ${esc(reviews.reviews_retrieved_count ?? 'unknown')} · Analyzed: ${esc(reviews.reviews_analyzed_count ?? 'unknown')} · ${esc(reviews.review_date_min)} – ${esc(reviews.review_date_max)}${reviews.reviews_truncated ? ' · Review input truncated':''}${reviews.details_truncated ? ' · Listing input truncated':''}</p>`:''}</div></div>`;
  $('property-notes').value = state.notes[row.room_id] || '';
  $('property-notes').addEventListener('input',event=>{ state.notes[row.room_id] = event.target.value; });
  $('detail-favorite').addEventListener('click',()=>toggleFavorite(row.room_id));
}
// A small Web Mercator tile viewer: all map controls and markers are embedded.
// Only optional street tile images require a network connection.
const map = $('map');
// Public ArcGIS street tiles work from local HTML without an API key or a
// website Referer. OSM's volunteer tile server requires a web Referer.
const tileUrl = (z,x,y) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/${z}/${y}/${x}`;
let view = {x:0.5,y:0.5,z:12,ready:false};
const project = (lat,lon)=>{ const rad = Math.max(-85.051129,Math.min(85.051129,lat))*Math.PI/180; return {x:(lon+180)/360,y:(1-Math.log(Math.tan(rad)+1/Math.cos(rad))/Math.PI)/2}; };
function fitMap() {
  const points = shown.filter(knownLocation).map(r=>project(Number(r.latitude),Number(r.longitude)));
  if (!points.length) { const bounds = trip.bounds; if (bounds) { const p = project((Number(bounds.north)+Number(bounds.south))/2,(Number(bounds.east)+Number(bounds.west))/2); view={...p,z:12,ready:true}; } return; }
  const xs=points.map(p=>p.x), ys=points.map(p=>p.y);
  const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const size=map.getBoundingClientRect();
  const scale = Math.min(Math.max(1,size.width-80)/Math.max(maxX-minX,0.00001),Math.max(1,size.height-80)/Math.max(maxY-minY,0.00001));
  view={x:(minX+maxX)/2,y:(minY+maxY)/2,z:Math.max(2,Math.min(16,Math.floor(Math.log2(scale/256)))),ready:true};
}
function renderMap() {
  if (!view.ready) fitMap();
  const size=map.getBoundingClientRect(), scale=256*2**view.z;
  const left=view.x*scale-size.width/2, top=view.y*scale-size.height/2;
  const tileLayer = $('tiles');
  const existingTiles = new Map([...tileLayer.children].map(img=>[img.dataset.tile,img]));
  const visibleTiles = new Set();
  $('markers').replaceChildren();
  $('stations').replaceChildren();
  if ($('street-map').checked) {
    const tileZoom=Math.min(Math.floor(view.z),16), magnification=2**(view.z-tileZoom), tileSize=256*magnification;
    const tiles=2**tileZoom;
    for (let x=Math.floor(left/tileSize);x<=Math.floor((left+size.width)/tileSize);x++) for (let y=Math.floor(top/tileSize);y<=Math.floor((top+size.height)/tileSize);y++) {
      if (y<0 || y>=tiles) continue;
      const wrappedX=((x%tiles)+tiles)%tiles;
      const key=`${tileZoom}/${x}/${y}`;
      visibleTiles.add(key);
      let img=existingTiles.get(key);
      if (!img) {
        img=document.createElement('img'); img.alt=''; img.draggable=false;
        img.dataset.tile=key;
        img.addEventListener('error',()=>{
          img.dataset.failed='true'; img.style.visibility='hidden'; updateMapStatus();
        });
        img.src=tileUrl(tileZoom,wrappedX,y);
        tileLayer.append(img);
      }
      img.style.left=`${x*tileSize-left}px`; img.style.top=`${y*tileSize-top}px`;
      img.style.width=`${tileSize}px`; img.style.height=`${tileSize}px`;
    }
  }
  for (const [key,img] of existingTiles) if (!visibleTiles.has(key)) img.remove();
  const located = shown.filter(knownLocation);
  located.forEach(row=>{
    const p=project(Number(row.latitude),Number(row.longitude));
    const button=document.createElement('button');button.className=`pin${row.room_id === state.selected ? ' selected':''}`;
    button.style.left=`${p.x*scale-left}px`;button.style.top=`${p.y*scale-top}px`;
    button.textContent=cost(row) === null ? '?' : new Intl.NumberFormat(undefined,{maximumFractionDigits:0}).format(cost(row));
    button.title=`${row.name || row.room_id} · ${money(cost(row))}`;button.setAttribute('aria-label',`Select ${row.name || row.room_id}`);
    button.addEventListener('pointerdown',event=>event.stopPropagation());button.addEventListener('click',()=>select(row.room_id));$('markers').append(button);
  });
  // These station locations and symbols are embedded, independent of zoom,
  // filtering the listings, and the optional online background tiles.
  for (const station of report.transit?.elements || []) {
    const p=project(station.lat,station.lon), x=p.x*scale-left,y=p.y*scale-top;
    if (x < -40 || x > size.width+40 || y < -40 || y > size.height+40) continue;
    const metro=station.tags.station === 'subway', name=station.tags.name;
    const marker=document.createElement('div');marker.className='station-pin';
    marker.style.left=`${x}px`;marker.style.top=`${y}px`;
    marker.innerHTML=(report.transit.icons?.[metro?'metro':'rail'] || (metro?'Ⓜ':'🚆'));
    marker.title=`${name} — ${metro?'Tube / Metro':'Train'} station`;
    const label=document.createElement('span');label.className='station-label';label.textContent=name;marker.append(label);
    $('stations').append(marker);
  }
  updateMapStatus();
}
function updateMapStatus() {
  const located=shown.filter(knownLocation).length;
  const failed=$('street-map').checked && $('tiles').querySelector('[data-failed]');
  $('map-status').textContent=`${located} mapped · ${shown.length-located} without coordinates (still in grid). Drag to pan; + / − to zoom. ${failed ? 'Some street tiles could not load. Check your internet connection; property markers remain usable.' : 'Property and saved train / Tube markers work offline; the background needs internet.'}`;
}
let dragging = null;
map.addEventListener('pointerdown',event=>{ if (event.target.closest('a,button')) return; dragging={x:event.clientX,y:event.clientY,vx:view.x,vy:view.y};map.setPointerCapture(event.pointerId); });
map.addEventListener('pointermove',event=>{ if (!dragging) return; const scale=256*2**view.z;view.x=dragging.vx-(event.clientX-dragging.x)/scale;view.y=Math.max(0,Math.min(1,dragging.vy-(event.clientY-dragging.y)/scale)); renderMap(); });
map.addEventListener('pointerup',()=>{if(dragging){dragging=null;renderMap();}});
map.addEventListener('pointercancel',()=>{dragging=null;renderMap();});
function zoom(delta) {view.z=Math.max(2,Math.min(18,view.z+delta));renderMap();}
$('zoom-in').addEventListener('click',()=>zoom(0.25));$('zoom-out').addEventListener('click',()=>zoom(-0.25));
$('fit-map').addEventListener('click',()=>{fitMap();renderMap();});
$('street-map').addEventListener('change',renderMap);
new ResizeObserver(()=>renderMap()).observe(map);
$('reset').addEventListener('click',()=>{
  for(const id of filters){ const el=$(id);if(el.type === 'checkbox') el.checked=false;else el.value=['capacity','beds','bedrooms','rating','limit'].includes(id) ? '0':''; }
  sort={key:"fit",direction:-1};state.sort=sort;rememberFilters();render();fitMap();renderMap();
});
$('save-copy').addEventListener('click',()=>{
  rememberFilters();state.favorites=[...favorites];
  const clone=document.documentElement.cloneNode(true);
  clone.querySelector('#report-data').textContent=JSON.stringify(report).replace(/</g,'\\u003c');
  // Rebuild dynamic regions on open; keep data and scripts as the portable source.
  for(const id of ['rows','columns','tiles','markers','details'])clone.querySelector(`#${id}`).replaceChildren();
  const blob=new Blob(['<!doctype html>\n',clone.outerHTML],{type:'text/html;charset=utf-8'});
  const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`${report.search_name.replace(/[^a-zA-Z0-9_-]/g,'_')}_reviewed.html`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
rememberFilters();render();
})();
