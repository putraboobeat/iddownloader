// Read on demand; never inject remote markup or retain response bodies.
function describePlaylist(text) {
  const value=text.replace(/^\uFEFF/,'').trimStart();
  if(value.startsWith('#EXTM3U')) {
    if(value.includes('#EXT-X-STREAM-INF:')) {
      const sizes=[...new Set([...value.matchAll(/RESOLUTION=(\d+x\d+)/g)].map(m=>m[1]))];
      return 'Playlist utama HLS'+(sizes.length?' · '+sizes.join(', '):'');
    }
    return 'Playlist potongan HLS (video atau audio)';
  }
  if(/<MPD[\s>]/.test(value))return 'Playlist DASH';
  try{JSON.parse(value);return 'JSON biasa — bukan playlist HLS';}catch{}
  return 'Respons teks — belum terverifikasi sebagai playlist';
}
let previewController=null;
function resetPreview(){
  if(previewController)previewController.abort();
  previewController=null;
  $('preview-panel').hidden=true;$('preview-body').textContent='';
  $('preview').disabled=!$('media').value;
}
async function previewSelected(){
  resetPreview();
  const item=items.find(x=>x.url===$('media').value);
  if(!item)return;
  const controller=new AbortController();previewController=controller;
  $('preview-panel').hidden=false;$('preview-summary').textContent='Membaca isi playlist…';
  $('preview').disabled=true;
  const timer=setTimeout(()=>controller.abort(),15000);
  try{
    if(item.kind==='video')throw Error('Sumber ini berupa file video. Pilih playlist untuk melihat isinya.');
    const response=await fetch(item.url,{signal:controller.signal,credentials:'omit',cache:'no-store'});
    if(!response.ok)throw Error('HTTP '+response.status+'. Server menolak preview. Coba putar ulang film untuk memperoleh tautan terbaru.');
    if(!response.body)throw Error('Respons tidak berisi data.');
    const reader=response.body.getReader(),decoder=new TextDecoder();
    const limit=262144;let size=0,text='',truncated=false;
    try{
      while(true){const {done,value}=await reader.read();if(done)break;
        const remaining=limit-size;
        text+=decoder.decode(value.subarray(0,remaining),{stream:true});size+=Math.min(value.length,remaining);
        if(value.length>remaining||size>=limit){truncated=true;break;}
      }
      text+=decoder.decode();
    }finally{await reader.cancel();}
    if(previewController!==controller)return;
    let description=describePlaylist(text);
    if(!truncated){try{text=JSON.stringify(JSON.parse(text),null,2);}catch{}}
    // Signed access tokens should not be exposed in previews/screenshots.
    text=text.replace(/([?&](?:t|token|sig|signature|key)=)[^\s"'&<>]+/gi,'$1[disembunyikan]');
    $('preview-summary').textContent=description+(truncated?' · Dibatasi 256 KB':'');
    $('preview-body').textContent=text;
  }catch(e){
    if(previewController!==controller)return;
    $('preview-summary').textContent=e.name==='AbortError'?'Preview melewati batas waktu 15 detik. Coba lagi.':e instanceof TypeError?'Preview gagal diambil. Server atau izin ekstensi mungkin menolak permintaan ulang.':e.message;
  }finally{
    clearTimeout(timer);
    if(previewController===controller){previewController=null;$('preview').disabled=false;}
  }
}
$('preview').onclick=previewSelected;
