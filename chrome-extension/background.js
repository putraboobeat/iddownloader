// Only observe responses. Do not block ads, change requests, or read cookies.
function classify(url, mime) {
  const path = new URL(url).pathname.toLowerCase();
  if (/\.(vtt|srt)$/.test(path) || mime.includes('text/vtt')) return 'subtitle';
  if (/\/config-[^/]+\.json$/.test(path)) return 'config';
  if (/\.(m3u8|mpd)$/.test(path) || /mpegurl|dash\+xml/.test(mime)) return 'playlist';
  if (/\.(mp4|webm)$/.test(path) || /video\/(mp4|webm)/.test(mime)) return 'video';
  return null;
}
const filter = {urls:['https://*.idlixku.com/*','https://*.majorplay.net/*']};
let queue = Promise.resolve();
function enqueue(fn) { queue = queue.then(fn).catch(() => {}); }
chrome.webRequest.onBeforeRequest.addListener(d => {
  if (d.tabId < 0 || d.type !== 'main_frame') return;
  enqueue(async () => {
    await chrome.storage.session.remove('tab:'+d.tabId);
    await chrome.action.setBadgeText({tabId:d.tabId,text:''});
  });
}, {urls:['http://*/*','https://*/*'],types:['main_frame']});
chrome.webRequest.onResponseStarted.addListener(d => {
  if (d.tabId < 0 || ![200,206].includes(d.statusCode)) return;
  const mime = (d.responseHeaders || []).find(h=>h.name.toLowerCase()==='content-type')?.value || '';
  const kind = classify(d.url,mime.toLowerCase());
  if (!kind) return;
  enqueue(async () => {
    const key='tab:'+d.tabId;
    const stored=(await chrome.storage.session.get(key))[key] || [];
    const items=stored.filter(x=>x.url!==d.url && Date.now()-x.time<3600000);
    items.push({url:d.url,kind,time:Date.now(),referer:d.initiator||''});
    await chrome.storage.session.set({[key]:items.slice(-60)});
    await chrome.action.setBadgeBackgroundColor({color:'#e7193f',tabId:d.tabId});
    await chrome.action.setBadgeText({text:String(Math.min(items.length,60)),tabId:d.tabId});
  });
},filter,['responseHeaders']);
chrome.tabs.onRemoved.addListener(id=>enqueue(()=>chrome.storage.session.remove('tab:'+id)));

chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id||!sender.tab||sender.frameId!==0)return;
  let host;try{host=new URL(sender.url).hostname;}catch{return;}
  if(!(host==='idlixku.com'||host.endsWith('.idlixku.com')))return;
  if(!['panel-status','panel-download','panel-settings','panel-copy'].includes(message.type))return;
  (async()=>{
    await queue;
    const list=((await chrome.storage.session.get('tab:'+sender.tab.id))['tab:'+sender.tab.id]||[]).filter(x=>Date.now()-x.time<3600000);
    const media=list.filter(x=>x.kind==='config').sort((a,b)=>b.time-a.time)[0];
    const settings=await chrome.storage.local.get('app');
    if(message.type==='panel-status')return {found:!!media,configured:!!settings.app};
    if(message.type==='panel-settings'){await chrome.runtime.openOptionsPage();return {ok:true};}
    if(message.type==='panel-copy'){if(!media)throw Error('Playlist belum ditemukan.');return {url:media.url};}
    if(!media)throw Error('Playlist belum ditemukan. Putar film terlebih dahulu.');
    if(!settings.app)throw Error('Isi alamat aplikasi terlebih dahulu.');
    const base=new URL(settings.app);
    if(base.protocol!=='http:'||base.hostname!=='127.0.0.1'||!base.port)throw Error('Alamat aplikasi tidak valid.');
    const group=new URL(media.url).pathname.match(/^\/v\/[^/]+\/[^/]+\//)?.[0];
    const subs=list.filter(x=>x.kind==='subtitle'&&group&&new URL(x.url).pathname.startsWith(group)).sort((a,b)=>Number(/\/i18n\/id\//.test(b.url))-Number(/\/i18n\/id\//.test(a.url))||b.time-a.time);
    const payload={url:media.url,mode:'direct',name:(sender.tab.title||'Video').replace(/\s*[|/]\s*IDLIX.*$/i,'').slice(0,100),referer:sender.tab.url,subtitle_url:subs[0]?.url||''};
    await chrome.tabs.create({url:base.origin+'/#import='+encodeURIComponent(JSON.stringify(payload))});
    return {ok:true};
  })().then(reply).catch(e=>reply({error:e.message}));
  return true;
});
