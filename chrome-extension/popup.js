let tab,items=[],manualMedia=false,manualSubtitle=false;
function rank(item){return item.kind==='config'?3:item.kind==='playlist'?2:item.kind==='video'?1:0;}
function showSelection(){resetPreview();const chosen=items.find(x=>x.url===$('media').value);$('selection').textContent=chosen?(chosen.kind==='config'?'Playlist utama dipilih otomatis':'Sumber video terpilih'):'Menunggu video diputar…';$('selection-detail').textContent=chosen?'Kualitas terbaik tersedia akan dipilih di aplikasi.':'Putar film setelah iklan, lalu klik Perbarui daftar.';$('send').disabled=!chosen;}
function label(item){const u=new URL(item.url);return `${item.kind==='config'?'★ Config':item.kind} · ${u.pathname.split('/').pop()} · ${u.hostname}`;}
async function refresh(){
  [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  items=((await chrome.storage.session.get('tab:'+tab.id))['tab:'+tab.id]||[]).filter(x=>Date.now()-x.time<3600000);
  items.sort((a,b)=>rank(b)-rank(a)||b.time-a.time);
  const selected=$('media').value,sub=$('subtitle').value;
  $('media').replaceChildren(new Option('Pilih playlist film…',''));
  $('subtitle').replaceChildren(new Option('Tanpa subtitle tambahan',''));
  for(const item of items){const option=new Option(label(item),item.url);$(item.kind==='subtitle'?'subtitle':'media').append(option);}
  const best=items.find(x=>x.kind!=='subtitle');
  $('media').value=manualMedia&&items.some(x=>x.url===selected)?selected:(best?.url||'');const source=items.find(x=>x.url===$('media').value);
  const group=source?new URL(source.url).pathname.match(/^\/v\/[^/]+\/[^/]+\//)?.[0]:null;
  const candidates=items.filter(x=>x.kind==='subtitle'&&group&&new URL(x.url).origin===new URL(source.url).origin&&new URL(x.url).pathname.startsWith(group));
  candidates.sort((a,b)=>Number(/\/i18n\/id\//.test(b.url))-Number(/\/i18n\/id\//.test(a.url))||b.time-a.time);
  $('subtitle').value=manualSubtitle&&(sub===''||candidates.some(x=>x.url===sub))?sub:(candidates[0]?.url||'');showSelection();
  $('status').textContent=items.length?'Sumber dipilih otomatis. Anda bisa langsung mengirim ke Downloader.':'Belum ada sumber. Putar film, aktifkan subtitle, lalu perbarui daftar.';
}
async function send(subOnly){
  try{
    const settings=await chrome.storage.local.get('app');if(!settings.app)throw Error('Atur alamat aplikasi di Pengaturan ekstensi terlebih dahulu.');const app=new URL(settings.app);
    if(!['http:','https:'].includes(app.protocol)||!app.hostname||app.username||app.password)throw Error('Gunakan alamat aplikasi yang valid: http://127.0.0.1:PORT atau https://dw.pmlab.id');
    const selected=items.find(x=>x.url===$(subOnly?'subtitle':'media').value);
    if(!selected)throw Error(subOnly?'Pilih subtitle terlebih dahulu.':'Pilih playlist terlebih dahulu.');
    await chrome.storage.local.set({app:app.origin});
    const payload={url:selected.url,mode:subOnly?'subtitle':'direct',name:$('name').value||'Video',referer:tab.url?.startsWith('https://')?tab.url:selected.referer,subtitle_url:subOnly?'':$('subtitle').value};
    // Transfer only after the user clicks. The app presents a form for review.
    await chrome.tabs.create({url:app.origin+'/#import='+encodeURIComponent(JSON.stringify(payload))});
    $('status').textContent='Tautan dikirim. Pilih kualitas/folder di aplikasi lalu klik Mulai unduh.';
  }catch(e){$('status').textContent=e.message;}
}
$('media').onchange=()=>{manualMedia=true;manualSubtitle=false;refresh().catch(e=>$('status').textContent=e.message);};
$('subtitle').onchange=()=>{manualSubtitle=true;};
$('send').onclick=()=>send(false);$('send-sub').onclick=()=>send(true);
$('refresh').onclick=()=>refresh().catch(e=>$('status').textContent=e.message);
$('clear').onclick=async()=>{await chrome.storage.session.remove('tab:'+tab.id);await chrome.action.setBadgeText({tabId:tab.id,text:''});await refresh();};
(async()=>{await refresh();$('name').value=(tab.title||'Video').replace(/\s*[|/]\s*[^|/]+$/i,'').slice(0,100);})().catch(e=>$('status').textContent=e.message);

$('settings').onclick=()=>chrome.runtime.openOptionsPage();
$('copy-link').onclick=async()=>{const item=items.find(x=>x.url===$('media').value);if(!item){$('status').textContent='Playlist belum ditemukan.';return;}try{await navigator.clipboard.writeText(item.url);$('status').textContent='Link playlist lengkap tersalin.';}catch{const box=$('copy-link-fallback');box.hidden=false;box.value=item.url;box.focus();box.select();$('status').textContent='Tekan Command+C atau Ctrl+C untuk menyalin.'}};
