// Execute the shipped worker, including concurrent deliveries, against bounded browser API fixtures.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const handlers={},notifications=[],opened=[],cache=new Map();
let focused=0;
const origin='https://fixture.example';
const self={location:{origin},skipWaiting:()=>{},registration:{showNotification:async(title,options)=>notifications.push({title,...options})},
  clients:{claim:()=>{},matchAll:async()=>[{url:origin+'/#action?id=unfinished',navigate:()=>{throw Error('Clobbered an unfinished action');},focus:()=>focused++}],openWindow:async(url)=>opened.push(url)},
  addEventListener:(type,handler)=>handlers[type]=handler};
const caches={open:async()=>({match:async key=>cache.has(key)?new Response(cache.get(key)):undefined,
  put:async(key,response)=>cache.set(key,await response.text()),keys:async()=>[...cache.keys()],delete:async key=>cache.delete(key)})};
vm.runInNewContext(fs.readFileSync('wsl_resource_guard/web/sw.js','utf8'),{self,caches,URL,Response,Promise,Number,String,Math});
function push(payload){let result;handlers.push({data:{json:()=>payload},waitUntil:p=>result=p});return result;}
function click(data){let result;handlers.notificationclick({notification:{data,close:()=>{}},waitUntil:p=>result=p});return result;}
(async()=>{
 const a='a'.repeat(32),b='b'.repeat(32);
 await Promise.all([push({incident_id:a,revision:2,observed_at:100,title:'private title',body:'private body'}),push({incident_id:a,revision:1,observed_at:90})]);
 assert.equal(notifications.length,1);
 await push({incident_id:a,revision:2,observed_at:100});assert.equal(notifications.length,1);
 await push({incident_id:b,revision:1,observed_at:100});assert.equal(notifications.length,2);
 assert.notEqual(notifications[0].tag,notifications[1].tag);
 for(const value of cache.values()){assert(!value.includes('private'));assert(Object.keys(JSON.parse(value)).every(k=>['revision','observed_at'].includes(k)));}
 await click({incident_id:a,url:'https://evil.example/'});assert.equal(opened[0],origin+'/#incident?id='+a);assert.equal(focused,0);
 await click({url:'javascript:alert(1)'});assert.equal(opened[1],origin+'/#alerts');
 self.clients.matchAll=async()=>[{url:origin+'/#incident?id='+a,focus:()=>focused++}];
 await click({incident_id:a});assert.equal(focused,1);assert.equal(opened.length,2);
 console.log('Worker passed: stable distinct incidents, out-of-order suppression, private metadata bounds, same-origin links, unfinished tab preserved');
})().catch(error=>{console.error(error);process.exitCode=1;});
