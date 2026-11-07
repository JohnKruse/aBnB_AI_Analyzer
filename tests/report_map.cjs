// Exercise the exported map's real code with a small DOM fixture; no network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor() {
    this.children=[]; this.dataset={}; this.style={}; this.events={}; this.checked=true;
    this.attributes={};
    const classes = new Set();
    this.classList = {contains: name => classes.has(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name)};
  }
  get offsetWidth() { return (this.textContent || '').length * 7 + 12; }
  get offsetHeight() { return 21; }
  append(child) { child.parent=this; this.children.push(child); }
  replaceChildren() { this.children=[]; }
  remove() { this.parent.children=this.parent.children.filter(child=>child!==this); }
  addEventListener(name, handler) { this.events[name]=handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector() { return this.children.find(child=>child.dataset.failed); }
  getBoundingClientRect() { return {width:640,height:397}; }
}
const elements=Object.fromEntries(['map','tiles','markers','stations','street-map','map-status'].map(id=>[id,new Element()]));
let created=0;
const context=vm.createContext({
  $:id=>elements[id],
  document:{createElement:()=>{created++;return new Element();}},
  report:{transit:{elements:[],icons:{}}},
  shown:[{room_id:'one',latitude:51.46,longitude:-0.22,name:'Putney'}],
  knownLocation:()=>true, trip:{}, state:{}, cost:()=>100, money:()=> '€100', select:()=>{}, Intl,
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/map_assets/station_labels.js'),'utf8'),context);
const script=fs.readFileSync(path.join(__dirname,'../src/report_assets/report.js'),'utf8');
vm.runInContext(script.slice(script.indexOf('const map ='),script.indexOf('let dragging =')),context);
vm.runInContext('renderMap()',context);
assert.ok(elements.tiles.children.length>0);
assert.equal(elements.markers.children.length,1);
for (const img of elements.tiles.children) {
  const [z,x,y]=img.dataset.tile.split('/');
  assert.equal(img.src,`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/${z}/${y}/${x}`);
}
const first=elements.tiles.children[0];
vm.runInContext('renderMap()',context);
assert.equal(elements.tiles.children[0],first,'Selecting a property must reuse visible tiles');
first.events.error();
assert.equal(first.style.visibility,'hidden');
assert.match(elements['map-status'].textContent,/Some street tiles could not load/);
assert.equal(elements.markers.children.length,1,'Tile failures must preserve property markers');
elements['street-map'].checked=false;
vm.runInContext('renderMap()',context);
assert.equal(elements.tiles.children.length,0);
assert.equal(elements.markers.children.length,1);
assert.doesNotMatch(elements['map-status'].textContent,/could not load/);
const before=created;
vm.runInContext('renderMap()',context);
assert.equal(created-before,1,'Offline redraw creates only a property marker, no tiles');
// A station must remain visible with a distinct icon at every zoom, offline.
vm.runInContext(`report.transit={icons:{rail:'<svg>train</svg>',metro:'<svg>roundel</svg>'},elements:[
  {lat:51.46,lon:-.22,tags:{name:'Putney',station:'rail'}},
  {lat:51.46,lon:-.22,tags:{name:'East Putney',station:'subway'}}]};
  view={...project(51.46,-.22),z:12,ready:true}`,context);
for(const zoom of [2,8,12,13.25,18]) {
  vm.runInContext(`view.z=${zoom};renderMap()`,context);
  assert.equal(elements.stations.children.length,2,`Stations remain visible at zoom ${zoom}`);
  assert.equal(elements.stations.children[0].children[0].textContent,'Putney');
  assert.equal(elements.stations.children[1].children[0].textContent,'East Putney');
  assert.notEqual(elements.stations.children[0].innerHTML,elements.stations.children[1].innerHTML);
}
const visibleLabels = () => elements.stations.children
  .filter(marker => marker.children[0].classList.contains('station-label-visible'))
  .map(marker => marker.children[0].textContent);
vm.runInContext(`
  view = {...project(51.4615, -.22), z: 16, ready: true};
  report.transit.elements = Array.from({length: 24}, (_, i) => ({
    lat: 51.46 + .001 * Math.floor(i / 6),
    lon: -.22 + .002 * (i % 6 - 2.5),
    tags: {name: 'Stop ' + i, station: 'rail'},
  }));
  renderMap();
`, context);
const labels = visibleLabels();
assert.equal(elements.stations.children.length, 24, 'Capping labels must keep every icon');
assert.equal(labels.length, 12, 'Even spaced stations must respect the label cap');
assert.ok(elements.stations.children.every(pin => pin.tabIndex === 0 && pin.attributes['aria-label']));
vm.runInContext('renderMap()', context);
assert.deepEqual(visibleLabels(), labels, 'Redrawing the same view must keep the same labels');
vm.runInContext('view.z = 13; renderMap()', context);
assert.equal(elements.stations.children.length, 24);
assert.ok(visibleLabels().length < labels.length, 'Dense stations leave fewer labels in view');
console.log('Report map tile and transit lifecycle passed');

// Cancellation choices operate on embedded evidence, with no provider calls.
const inputs = new Map();
const input = id => { if (!inputs.has(id)) inputs.set(id,{value:'',checked:false}); return inputs.get(id); };
const filterContext = vm.createContext({$:input, number:value=>Number(value), text:value=>String(value||''),
  nested:value=>value, cost:()=>100, favorites:new Set()});
vm.runInContext(script.slice(script.indexOf('function passes('),script.indexOf('function renderHeaders(')),filterContext);
input('cancellation').value='free';
assert.equal(vm.runInContext("passes({cancellation_state:'free'})",filterContext),true);
assert.equal(vm.runInContext("passes({cancellation_state:'unknown'})",filterContext),false);
input('cancellation').value='nonrefundable';
assert.equal(vm.runInContext("passes({cancellation_state:'nonrefundable'})",filterContext),true);
input('cancellation').value='conflict';
assert.equal(vm.runInContext("passes({cancellation_state:'unknown',cancellation_evidence:{conflict:true}})",filterContext),true);
input('cancellation').value='';
assert.equal(vm.runInContext("passes({cancellation_state:'unknown'})",filterContext),true);
