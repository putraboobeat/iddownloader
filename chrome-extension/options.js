const app=document.getElementById('app'),status=document.getElementById('status');
chrome.storage.local.get('app').then(s=>app.value=s.app||'');
document.getElementById('save').onclick=async()=>{
  try{
    const val=app.value.trim();
    if(!val) throw Error('Alamat aplikasi tidak boleh kosong.');
    const u=new URL(val.startsWith('http://')||val.startsWith('https://')?val:'http://'+val);
    if(!['http:','https:'].includes(u.protocol)||u.username||u.password||!u.hostname) {
      throw Error('Gunakan alamat valid: http://127.0.0.1:PORT atau https://dw.pmlab.id');
    }
    await chrome.storage.local.set({app:u.origin});
    app.value=u.origin;
    status.textContent='Pengaturan tersimpan.';
  }catch(e){
    status.textContent=e.message;
  }
};
