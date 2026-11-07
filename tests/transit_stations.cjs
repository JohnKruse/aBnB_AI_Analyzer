// Exercise the actual Leaflet overlay script offline, including service failure.
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const markers=[], requests=[], callbacks={}, timers=[];
let zoom=13, bounds=[51.44,-.24,51.48,-.19], enabled=true, initial, fail=false;
const status={textContent:''};
const stations={clearLayers(){markers.length=0;},eachLayer(fn){markers.forEach(fn);}};
const map={
  getZoom:()=>zoom, getBounds:()=>({getSouth:()=>bounds[0],getWest:()=>bounds[1],getNorth:()=>bounds[2],getEast:()=>bounds[3]}),
  hasLayer:()=>enabled, distance:(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1])*100000,
  getSize:()=>({x:640,y:397}),
  latLngToContainerPoint:([lat,lon])=>({x:320+(lon+.22)*8000,y:198+(51.46-lat)*8000}),
  on(names,fn){for(const name of names.split(' ')) callbacks[name]=fn;},
  whenReady(fn){initial=fn();}
};
const session=new Map();
const L={
  control:()=>({addTo(){this.onAdd();}}),
  DomUtil:{create:()=>status},DomEvent:{disableClickPropagation(){},disableScrollPropagation(){}},
  divIcon:x=>x, marker:(point,options)=>{
    const label = {offsetWidth: 100, offsetHeight: 21, style:{}, classList:{toggle(){},remove(){}}};
    const icon = {querySelector:()=>label, setAttribute(name,value){this[name]=value;}};
    return {options, getLatLng:()=>point, getElement:()=>icon,
      bindPopup(html){this.popup=html;return this;},
      on(name,fn){this[name]=fn;return this;},
      addTo(){markers.push(this);this.add();return this;}};
  }
};
const context=vm.createContext({L,document:{createElement:()=>({}),head:{appendChild(){}}},
  sessionStorage:{getItem:key=>session.get(key),setItem:(key,value)=>session.set(key,value)},
  setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},
  URLSearchParams,AbortController,Date,
  fetch:async(url,options)=>{requests.push({url,options});return fail?{ok:false,status:429,headers:{get:()=> '120'}}:
    {ok:true,json:async()=>({elements:[
      {lat:51.46,lon:-.22,tags:{railway:'station',name:'Putney <script>'}},
      {center:{lat:51.46001,lon:-.22001},tags:{railway:'station',name:'Putney <script>'}},
      {lat:51.459,lon:-.211,tags:{railway:'station',station:'subway',name:'East Putney'}},
      {lat:NaN,lon:0,tags:{railway:'station'}}]})};}
});
const path = require('node:path');
for (const file of ['../src/map_assets/station_labels.js','../src/ui/assets/transit_stations.js']) {
  vm.runInContext(fs.readFileSync(path.join(__dirname,file),'utf8'),context);
}
context.map=map;context.stations=stations;
context.labelStyles=fs.readFileSync(path.join(__dirname,'../src/map_assets/station_labels.css'),'utf8');
vm.runInContext('setupBnbTransit(map,stations,null,labelStyles)',context);
async function move(){callbacks.moveend();await timers.filter(x=>x.delay===1000).at(-1).fn();}
(async()=>{
  await initial;
  assert.equal(requests.length,1);
  assert.equal(markers.length,2,'Duplicate station representations are merged');
  assert.match(markers[0].options.icon.html,/&lt;script&gt;/,'Provider text must be escaped');
  assert.match(markers[1].getElement()['aria-label'],/Tube/);
  assert.match(markers[1].popup,/Tube \/ Metro/);
  assert.match(String(requests[0].options.body),/station/);
  zoom=14;await move();
  assert.equal(requests.length,1,'Zooming within cached bounds must not fetch again');
  fail=true;bounds=[45,7,45.1,7.1];await move();
  assert.equal(requests.length,2);
  assert.match(status.textContent,/search map still works/);
  await move();assert.equal(requests.length,2,'Rate limit must prevent request storms');
  enabled=false;await move();assert.match(status.textContent,/hidden/);
  // A previous provider cooldown must not block bundled London stations.
  enabled=true;bounds=[51.44,-.24,51.48,-.19];
  session.set('bnb-transit-stations-v1-cooldown',String(Date.now()+600000));
  context.savedOverlay={bounds:[51.25,-.65,51.8,.35],source:'Saved London station data',elements:[
    {lat:51.4611,lon:-.2162,tags:{name:'Putney',station:'rail'}},
    {lat:51.4586,lon:-.2112,tags:{name:'East Putney',station:'subway'}},
    {lat:51.4682,lon:-.2089,tags:{name:'Putney Bridge',station:'subway'}}]};
  const before=requests.length;
  vm.runInContext('setupBnbTransit(map,stations,savedOverlay,labelStyles)',context);
  await initial;
  assert.equal(requests.length,before,'London station markers must load without ANY live API request');
  assert.equal(markers.length,3);
  assert.match(status.textContent,/Saved London station data/);
  assert.match(markers[1].popup,/East Putney/);
  assert.match(markers[1].getElement()['aria-label'],/Tube/);
  for(const level of [2,8,12,18]) {
    zoom=level;await move();
    assert.equal(markers.length,3,`Bundled stations remain visible at zoom ${level}`);
    assert.equal(requests.length,before);
  }
  console.log('Transit stations overlay lifecycle passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
