const app=document.getElementById('app'),status=document.getElementById('status');
chrome.storage.local.get('app').then(s=>app.value=s.app||'');
document.getElementById('save').onclick=async()=>{try{const u=new URL(app.value.trim());if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||!u.port||u.username||u.password||u.pathname!=='/'||u.search||u.hash)throw Error('Gunakan http://127.0.0.1:PORT tanpa tambahan lainnya.');await chrome.storage.local.set({app:u.origin});status.textContent='Pengaturan tersimpan.';}catch(e){status.textContent=e.message;}};
