(()=>{
 let dismissed=false,root=null,busy=false;
 const message=data=>chrome.runtime.sendMessage(data);
 function mount(configured){
  const host=document.createElement('div');host.style.cssText='position:fixed;right:20px;bottom:20px;z-index:2147483647';
  const shadow=host.attachShadow({mode:'closed'});
  shadow.innerHTML=`<style>:host{all:initial}*{box-sizing:border-box}.panel{width:310px;max-width:calc(100vw - 40px);padding:20px;background:#111116;color:#fff;border:1px solid #a82645;border-radius:16px;box-shadow:0 12px 50px #0009;font:13px/1.6 system-ui}header{display:flex;justify-content:space-between;align-items:center}strong{color:#ff3555;font-size:19px}p{color:#bcbcc8;margin:10px 0}button,input{font:inherit;border-radius:8px;padding:10px;width:100%;border:1px solid #444;background:#24242b;color:white}button{cursor:pointer}button:disabled{opacity:.5}.close{width:auto;padding:0 7px;background:none;border:0;font-size:22px}.download{background:#ed1740;border:0;font-weight:700;margin-top:9px}.status{font-size:11px;overflow-wrap:anywhere}summary{cursor:pointer;color:#aaa;font-size:11px}details{margin-top:12px}input{margin:8px 0;font-size:12px}label{font-size:11px}</style><section class="panel" aria-label="OmniFetch"><header><strong>OmniFetch</strong><button class="close" aria-label="Tutup">×</button></header><p>Playlist film terdeteksi.<br>Film sudah diputar? Siap dikirim ke OmniFetch.</p><button class="copy">Copy link JSON</button><button class="download">Kirim ke OmniFetch</button><button class="settings" style="margin-top:10px">⚙ Pengaturan ekstensi</button><textarea class="fallback" readonly hidden style="width:100%;margin-top:10px" aria-label="Link playlist"></textarea><p class="status" role="status">Kualitas dan folder dipilih di aplikasi. Subtitle Indonesia disertakan jika terdeteksi.</p></section>`;
  document.documentElement.append(host);root=host;
  const status=shadow.querySelector('.status');if(!configured)status.textContent='Atur alamat Downloader melalui Pengaturan ekstensi. Copy link bisa langsung dipakai.';
  shadow.querySelector('.close').onclick=()=>{dismissed=true;host.remove();};
  shadow.querySelector('.settings').onclick=()=>message({type:'panel-settings'});
  shadow.querySelector('.copy').onclick=async()=>{try{const r=await message({type:'panel-copy'});if(r.error)throw Error(r.error);try{await navigator.clipboard.writeText(r.url);status.textContent='Link JSON lengkap tersalin.';}catch{const box=shadow.querySelector('.fallback');box.hidden=false;box.value=r.url;box.focus();box.select();status.textContent='Tekan Command+C atau Ctrl+C untuk menyalin.';}}catch(e){status.textContent=e.message;}};
  shadow.querySelector('.download').onclick=async e=>{const btn=e.currentTarget;btn.disabled=true;try{const r=await message({type:'panel-download'});if(r?.error)throw Error(r.error);status.textContent='Terbuka di aplikasi. Klik Mulai unduh untuk melanjutkan.';}catch(e){status.textContent=e.message;}finally{btn.disabled=false;}};
 }
 async function check(){if(dismissed||busy)return;busy=true;try{const r=await message({type:'panel-status'});if(r?.found&&!root)mount(r.configured);}catch{clearInterval(timer);}finally{busy=false;}}
 const timer=setInterval(check,2000);check();
})();
