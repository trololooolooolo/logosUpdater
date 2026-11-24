function tr(k, v) { let s=(T[k]!==undefined?T[k]:k); if(v) for(const x in v) s=s.split('{'+x+'}').join(v[x]); return s; }
// basic mode has no 🎨 Logo versions / 🔨 Build: use the '<key>_basic' wording when the catalog has one
function trm(k, v) { return tr(!document.body.classList.contains('advanced') && T[k+'_basic']!==undefined ? k+'_basic' : k, v); }
function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(el => { const v=T[el.dataset.i18n]; if(v!==undefined) el.textContent=v; });
    document.querySelectorAll('[data-i18n-ph]').forEach(el => { const v=T[el.dataset.i18nPh]; if(v!==undefined) el.placeholder=v; });
    document.querySelectorAll('[data-i18n-title]').forEach(el => { const v=T[el.dataset.i18nTitle]; if(v!==undefined) el.title=v; });
}
let currentPage=1, pageSize=50, allLogos=[], filteredLogos=[], gIndex=0, gBatch=50, curData=null, cropper=null;
function toast(msg, type='info', timeout=6000) {
    const box=document.getElementById('toastBox');
    const el=document.createElement('div'); el.className='toast '+type;
    el.textContent=msg;
    const x=document.createElement('span'); x.className='x'; x.textContent='✕'; el.appendChild(x);
    el.onclick=()=>el.remove(); box.appendChild(el);
    if(timeout) setTimeout(()=>el.remove(), timeout);
}
function setBusy(on) { document.body.classList.toggle('busy', on); }
function confirmDialog(msg, yes=T['dialog.ok'], no=T['dialog.cancel']) {
    return new Promise(res=>{
        const m=document.getElementById('confirmModal');
        document.getElementById('confirmMsg').textContent=msg;
        const by=document.getElementById('confirmYes'), bn=document.getElementById('confirmNo');
        by.textContent=yes; bn.textContent=no; m.style.display='flex';
        const done=v=>{ m.style.display='none'; res(v); };
        by.onclick=()=>done(true); bn.onclick=()=>done(false);
    });
}
// update the row logo WITHOUT reloading the page (filters stay)
function updateRowLogo(s, base, where) {
    document.querySelectorAll('.row-check').forEach(c=>{
        if(c.dataset.id===s.station_id && c.dataset.type===s.type && c.dataset.ecc===s.ecc && String(c.dataset.freq)===String(s.freq)) {
            const tr=c.closest('tr');
            let img=tr.querySelector('img.logo-preview');
            if(!img && base) {      // a row without a logo has no <img> yet
                img=document.createElement('img'); img.className='logo-preview'; img.loading='lazy';
                img.onerror=()=>{ img.style.opacity=0.2; };
                const cell=tr.querySelector('.logo-cell'); if(cell) cell.prepend(img);
            }
            if(img && base) { img.src=`/logos/${s.ecc}/${base}.PNG?t=${Date.now()}`; img.style.opacity=1; }
            const tag=tr.querySelector('.src-tag');
            if(tag && where) { tag.className='src-tag src-'+where; tag.textContent=T['src.'+where]; tag.title=T['src.'+where+'_title']; }
            if(base) {
                c.dataset.logo=base;    // selection actions (copy/delete/style) read the current file name
                if(tr.cells[7]) tr.cells[7].textContent=base;
            }
        }
    });
    schedulePkgStatus();
}
function saveState() {
    try { localStorage.setItem('lu_state', JSON.stringify({
        q: document.getElementById('search').value,
        dup: document.getElementById('dupToggle').checked,
        nl: document.getElementById('noLogoToggle').checked,
        udb: document.getElementById('updDb').checked,
        ulg: document.getElementById('updLogos').checked,
        pf: document.getElementById('pkgFilter').value,
        so: SORT,
        lb: document.getElementById('logoBg').value, lbc: document.getElementById('logoBgColor').value,
        ps: pageSize, pg: currentPage })); } catch(e) {}
}
function restoreState() {
    let s={}; try { s=JSON.parse(localStorage.getItem('lu_state')||'{}'); } catch(e) {}
    if(s.q) document.getElementById('search').value=s.q;
    if(s.dup) document.getElementById('dupToggle').checked=true;
    if(s.nl) document.getElementById('noLogoToggle').checked=true;
    if(s.pf) document.getElementById('pkgFilter').value=s.pf;
    fillCountryFilter();
    if(s.so && s.so.key) { SORT=s.so; applySort(); }
    // background behind the logo previews (old "dark preview" checkbox -> 'dark')
    // default = Sync 3 black, like the car's screen (white styled logos would vanish on 'none');
    // a choice saved in this browser wins
    document.getElementById('logoBg').value = s.lb || (s.dk ? 'dark' : 'sync_black');
    if(!document.getElementById('logoBg').value) document.getElementById('logoBg').value='sync_black';
    if(s.lbc) document.getElementById('logoBgColor').value=s.lbc;
    applyLogoBg();
    // update switches: last choice wins, otherwise the config defaults
    const cfgU = (UI_CFG && UI_CFG.update) || {database:true, logos:true};
    if(UI_CFG && UI_CFG.source && UI_CFG.source.enabled===false) {   // data_source: none
        document.getElementById('updGroup').hidden = true;
        document.getElementById('noSourceHint').hidden = false;
    }
    document.getElementById('updLegend').textContent = tr('grp.update', {source:(UI_CFG.source||{}).name||''});
    document.getElementById('updDb').checked = (s.udb===undefined) ? !!cfgU.database : !!s.udb;
    document.getElementById('updLogos').checked = (s.ulg===undefined) ? !!cfgU.logos : !!s.ulg;
    if(s.ps) { pageSize=s.ps; document.getElementById('pageSize').value=String(s.ps); }
    filterTable();
    if(s.pg && s.pg>1) { currentPage=s.pg; updatePagination(); }
}
function updatePageSize() { pageSize=parseInt(document.getElementById('pageSize').value); resetPage(); }
function resetPage() { currentPage=1; updatePagination(); }
function prevPage() { if(currentPage>1) { currentPage--; updatePagination(); } }
function nextPage() { if(currentPage < Math.ceil(getVisible().length/pageSize)) { currentPage++; updatePagination(); } }
function getVisible() { return Array.from(document.querySelectorAll('#stationTable tbody tr')).filter(r=>r.getAttribute('data-matches-filter')!=='false'); }
function updatePagination() {
    const vis = getVisible(), total = Math.ceil(vis.length/pageSize)||1;
    if(currentPage>total) currentPage=total;
    document.querySelectorAll('#stationTable tbody tr').forEach(r=>r.style.display='none');
    vis.slice((currentPage-1)*pageSize, currentPage*pageSize).forEach(r=>r.style.display='');
    document.getElementById('pageInfo').innerText = tr('page.info', {page:currentPage, total:total, count:vis.length});
    document.getElementById('visCount').textContent = tr('ctrl.vis_count', {n:vis.length});
    saveState();
}
function filterTable() {
    const q=document.getElementById('search').value.toUpperCase(), dup=document.getElementById('dupToggle').checked, nl=document.getElementById('noLogoToggle').checked;
    document.querySelectorAll('#stationTable tbody tr').forEach(r=>{
        const txt=r.innerText.toUpperCase(), isD=r.dataset.isDuplicate==='true', hasL=r.cells[7].innerText.trim()!=='';
        const pf=document.getElementById('pkgFilter').value, pk=r.dataset.pkg||'';
        const pm = !pf || pk===pf || (pf==='problem' && (pk==='missing' || pk==='differs' || pk==='pending'));
        const m = txt.includes(q) && (!dup || isD) && (!nl || !hasL) && pm;
        r.setAttribute('data-matches-filter', m?'true':'false');
    });
    updatePagination();
}
// country picker: the table holds one RDS country (ECC + first PI digit, as in 🖌 Logo style);
// the list = every country in the DBs, picking one reloads the table (choice kept in a cookie)
function fillCountryFilter() {
    const cnt=(UI_CFG && UI_CFG.country_rows) || {}, names=(UI_CFG && UI_CFG.countries) || {};
    const cs=document.getElementById('countryFilter'); cs.innerHTML='';
    Object.keys(cnt).sort((a,b)=>cnt[b]-cnt[a] || a.localeCompare(b)).forEach(k=>{
        const o=document.createElement('option'); o.value=k;
        o.textContent=`${names[k]||'?'} (${k}) · ${tr('ctrl.country_rows', {n:cnt[k]})}`; cs.appendChild(o);
    });
    cs.value=UI_CFG.country||'';
}
function changeCountry() {
    const v=document.getElementById('countryFilter').value;
    try { document.cookie=`lu_country=${v}; path=/; max-age=31536000; SameSite=Lax`; } catch(e) {}
    currentPage=1; saveState(); setBusy(true);
    location.href='/?country='+encodeURIComponent(v);
}
// column sort (name / frequency): click = ascending -> descending -> original order
let SORT={key:'', dir:1};
function sortTable(key) {
    SORT = SORT.key!==key ? {key, dir:1} : (SORT.dir===1 ? {key, dir:-1} : {key:'', dir:1});
    applySort(); resetPage(); saveState();
}
function _sortVal(r, key) {
    if(key==='freq') {
        const c=r.querySelector('.row-check'), f=parseInt(c && c.dataset.freq, 10);
        return (c && c.dataset.type==='FM' && f>0) ? f : null;       // DAB / no frequency: always last
    }
    const t=(r.cells[3] ? r.cells[3].innerText.trim() : '');
    return (t && t!=='-') ? t : null;
}
function applySort() {
    const tb=document.querySelector('#stationTable tbody'), rows=Array.from(tb.rows);
    rows.forEach((r,i)=>{ if(r.dataset.idx===undefined) r.dataset.idx=i; });
    const col=new Intl.Collator(document.documentElement.lang||'pl', {numeric:true, sensitivity:'base'});
    rows.sort((a,b)=>{
        if(!SORT.key) return a.dataset.idx-b.dataset.idx;
        const x=_sortVal(a,SORT.key), y=_sortVal(b,SORT.key);
        if(x===null || y===null) return (x===null)-(y===null) || a.dataset.idx-b.dataset.idx;
        const d = SORT.key==='freq' ? x-y : col.compare(x,y);
        return d*SORT.dir || a.dataset.idx-b.dataset.idx;
    });
    rows.forEach(r=>tb.appendChild(r));
    document.querySelectorAll('#stationTable th.sortable').forEach(th=>{
        th.querySelector('.sort-ind').textContent = th.dataset.sort===SORT.key ? (SORT.dir===1?' ▲':' ▼') : '';
    });
}
function toggleAll(s) { document.querySelectorAll('.row-check').forEach(c=>{ if(c.closest('tr').style.display!=='none') c.checked=s.checked; }); updateSelBar(); }
function clearRowChecks() { document.querySelectorAll('.row-check').forEach(c=>c.checked=false); const all=document.querySelector('#stationTable thead input[type="checkbox"]'); if(all) all.checked=false; updateSelBar(); }
// actions on selected rows appear only while something is selected
function updateSelBar() {
    const n=document.querySelectorAll('.row-check:checked').length;
    document.getElementById('selBar').hidden = !n;
    document.getElementById('selN').textContent = tr('sel.count', {n:n});
}
document.addEventListener('change', e=>{ if(e.target.classList && e.target.classList.contains('row-check')) updateSelBar(); });
function openBulkGallery() {
    const checks = Array.from(document.querySelectorAll('.row-check:checked'));
    if(!checks.length) return toast(T['toast.nothing_selected'],'err');
    const sel = checks.map(c=>({station_id:c.dataset.id,type:c.dataset.type,ecc:c.dataset.ecc,freq:c.dataset.freq}));
    const withLogo = checks.find(c=>c.dataset.slu);  // source logo (if any)
    openGallery(sel[0].station_id, sel[0].type, sel[0].ecc, sel[0].freq,
                withLogo ? withLogo.dataset.slu : '', checks[0].dataset.name || '', '', sel);
}
function openGallery(id, type, ecc, freq, slu, sn, curL, bulk=null) {
    curData = {id, type, ecc, freq, slu, sn, bulk};
    document.getElementById('galleryTitle').textContent = tr('gallery.title_ecc', {ecc: ecc});
    document.getElementById('gallerySearch').value="";
    const rs=document.getElementById('remoteLogoSection'); if(slu) { document.getElementById('remoteLogoImg').src=slu; rs.style.display='block'; } else rs.style.display='none';
    document.getElementById('galleryGrid').innerHTML=T['gallery.loading'];
    document.getElementById('galleryModal').style.display='flex';
    setBusy(true);
    fetch(`/api/gallery?ecc=${ecc}&sid=${id}&name=${encodeURIComponent(sn)}`).then(r=>r.json()).then(d=>{
        allLogos=d.logos; filteredLogos=allLogos; gIndex=0; renderGallery(true); setBusy(false);
    }).catch(e=>{ setBusy(false); toast(T['err.gallery_load']+e,'err'); });
}
function filterGallery() {
    const q=document.getElementById('gallerySearch').value.toUpperCase();
    filteredLogos = q ? allLogos.filter(i=>(i.file.toUpperCase().includes(q) || i.ocr.toUpperCase().includes(q))) : allLogos;
    gIndex=0; renderGallery(true);
}
function renderGallery(reset) {
    const g=document.getElementById('galleryGrid'); if(reset) g.innerHTML="";
    const batch=filteredLogos.slice(gIndex, gIndex+gBatch);
    batch.forEach(i=>{
        const div=document.createElement('div'); div.style.textAlign='center'; div.style.padding='5px';
        if(i.score<10) div.style.background='rgba(45,125,70,0.3)';
        const img=document.createElement('img'); img.src=`/img_gallery/${curData.ecc}/${i.file}?v=${UI_CFG.img_ver||''}`;
        img.style.width='100px'; img.style.cursor='pointer'; img.onclick=()=>selectLogo(i.file);
        const l=document.createElement('div'); l.innerText=i.file.split('.')[0]; l.style.fontSize='10px';
        const o=document.createElement('div'); o.innerText=i.ocr; o.style.fontSize='9px'; o.style.color='#888';
        div.append(img, l, o); g.append(div);
    });
    gIndex+=batch.length; document.getElementById('galleryPagination').style.display=(gIndex<filteredLogos.length)?'block':'none';
}
function loadMoreGallery() { renderGallery(false); }
async function selectLogo(f) {
    const st = curData.bulk || [{station_id:curData.id, type:curData.type, ecc:curData.ecc, freq:curData.freq}];
    if(!(await confirmDialog(tr('msg.change_logo_for', {file:f, count:st.length}), T['dialog.yes_change']))) return;
    setBusy(true);
    try {
        const d = await (await fetch('/api/copy_logo', {method:'POST', body:JSON.stringify({stations:st, filename:f})})).json();
        setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+JSON.stringify(d),'err');
        const base=f.split('.')[0];
        st.forEach(s=>updateRowLogo(s, base, 'img'));
        closeGallery(); filterTable();
        toast(tr('toast.logo_changed', {count:d.updated}),'ok');
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
async function fetchRemoteLogo() {
    const st = curData.bulk || [{station_id:curData.id, type:curData.type, ecc:curData.ecc, freq:curData.freq}];
    if(!curData.slu) return toast(T['toast.no_logo_url'],'err');
    if(!(await confirmDialog(tr('msg.set_source_logo_for', {count:st.length}), T['dialog.yes_set']))) return;
    setBusy(true);
    try {
        const d = await (await fetch('/api/fetch_remote_logo', {method:'POST', body:JSON.stringify({stations:st, url:curData.slu})})).json();
        setBusy(false);
        if(d.status!=='ok') return toast(d.error || (T['err.prefix']+JSON.stringify(d)),'err');
        st.forEach(s=>updateRowLogo(s, d.new_logo, 'sync5'));
        closeGallery(); filterTable();
        toast(tr('toast.logo_set', {count:d.updated}),'ok');
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
function closeGallery() { document.getElementById('galleryModal').style.display='none'; }
function triggerUpload(id,type,ecc,freq) { curData={id,type,ecc,freq}; document.getElementById('logoInput').click(); }
document.getElementById('logoInput').onchange=function(e) {
    const file=this.files && this.files[0];
    this.value='';      // the same file picked again (e.g. for the next station) must fire onchange too
    if(!file) return;
    const reader=new FileReader(); reader.onload=function(ev) { document.getElementById('editorImage').src=ev.target.result; document.getElementById('editorModal').style.display='flex'; 
    if(cropper) cropper.destroy(); cropper=new Cropper(document.getElementById('editorImage'),{viewMode:1}); };
    reader.readAsDataURL(file);
};
function rotateLeft() { cropper.rotate(-90); }
function makeTransparent() {
    const c=cropper.getCroppedCanvas(), ctx=c.getContext('2d'), d=ctx.getImageData(0,0,c.width,c.height);
    for(let i=0;i<d.data.length;i+=4) if(d.data[i]>230 && d.data[i+1]>230 && d.data[i+2]>230) d.data[i+3]=0;
    ctx.putImageData(d,0,0); const url=c.toDataURL(); document.getElementById('editorImage').src=url; cropper.replace(url);
}
function saveAndUpload() {
    cropper.getCroppedCanvas().toBlob(b=>{
        const fd=new FormData(); fd.append('file',b,'logo.png'); fd.append('station_id',curData.id); fd.append('type',curData.type); fd.append('ecc',curData.ecc); fd.append('freq',curData.freq);
        const s={station_id:curData.id, type:curData.type, ecc:curData.ecc, freq:curData.freq};
        setBusy(true);
        fetch('/api/upload_logo',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
            setBusy(false);
            if(d.status!=='ok') return toast(T['err.upload']+JSON.stringify(d),'err');
            updateRowLogo(s, d.new_logo, 'sync5'); closeEditor(); filterTable();
            toast(T['toast.logo_saved'],'ok');
        }).catch(e=>{ setBusy(false); toast(T['err.prefix']+e,'err'); });
    },'image/png');
}
function closeEditor() { document.getElementById('editorModal').style.display='none'; if(cropper) cropper.destroy(); }
async function deleteSelected() {
    const sel = Array.from(document.querySelectorAll('.row-check:checked')); if(!sel.length) return toast(T['toast.nothing_selected'],'err');
    if(!(await confirmDialog(tr('msg.delete_selected', {count:sel.length}), T['dialog.yes_delete']))) return;
    const p={fm:[],dab:[]}; sel.forEach(c=>p[c.dataset.type.toLowerCase()].push(parseInt(c.dataset.rowid)));
    setBusy(true);
    try {
        const d = await (await fetch('/api/delete_records',{method:'POST',body:JSON.stringify(p)})).json();
        setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+JSON.stringify(d),'err');
        sel.forEach(c=>c.closest('tr').remove()); filterTable(); updateSelBar(); scheduleChanges();
        // keep the country list's row count in step with the table (as after adding a station)
        const cnt = UI_CFG.country_rows || {}, k = UI_CFG.country;
        if(k && cnt[k] != null) { cnt[k] = Math.max(0, cnt[k] - (d.deleted || 0)); fillCountryFilter(); }
        schedulePkgStatus();
        toast(tr('toast.deleted', {count:d.deleted}),'ok');
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
async function runUpdate(btn) {
    const database = document.getElementById('updDb').checked;
    const logos = document.getElementById('updLogos').checked;
    if(!database && !logos) return toast(T['toast.nothing_to_update'],'err');
    const what = [database ? T['ctrl.update_db'] : null, logos ? T['ctrl.update_logos'] : null].filter(Boolean).join(' + ');
    if(!(await confirmDialog(tr('msg.update_confirm', {what:what}), T['dialog.yes_update']))) return;
    try {
        const r = await fetch('/api/update', {method:'POST', body:JSON.stringify({database, logos})});
        const d = await r.json().catch(()=>({}));
        if(d.status==='started') pollUpdate();
        else toast(T['err.update'] + (d.error || JSON.stringify(d)), 'err');
    } catch(e) { toast(T['err.prefix'] + e, 'err'); }
}
// The update runs on the server in the background (requests are spaced out on purpose).
let UPD_TIMER=null;
function showUpdateRunning(on) {
    document.getElementById('updGo').disabled=on;
    document.getElementById('updStop').hidden=!on;
    const p=document.getElementById('updProgress'); p.hidden=!on;
    if(on) p.title=T['upd.slow_note']||'';
}
async function pollUpdate() {
    clearTimeout(UPD_TIMER);
    let s;
    try { s = await (await fetch('/api/update_status')).json(); } catch(e) { UPD_TIMER=setTimeout(pollUpdate, 3000); return; }
    if(s.running) {
        showUpdateRunning(true);
        const ph = T['upd.phase_'+s.phase] || s.phase || '';
        let txt = s.total ? tr('upd.progress', {phase:ph, done:s.done, total:s.total}) : ph + '…';
        if(s.eta) txt += tr('upd.eta', {min: Math.max(1, Math.round(s.eta/60))});
        document.getElementById('updProgress').textContent = txt;
        UPD_TIMER=setTimeout(pollUpdate, 1000);
        return;
    }
    showUpdateRunning(false);
    if(!s.result && !s.error) return;                     // nothing ran since the server started
    const seenKey=String(s.result && s.result.seconds)+(s.error_text||'');   // toast each result once per tab
    try { if(sessionStorage.getItem('lu_upd_seen')===seenKey) return; sessionStorage.setItem('lu_upd_seen', seenKey); } catch(e) {}
    if(s.error_text) toast(s.error_text, 'err', 15000);
    const d = s.result;
    if(!d) return;
    if(d.cancelled) toast(T['upd.cancelled'], 'err', 10000);
    else if(!s.error_text) {
        const sk = Object.entries(d.skipped||{}).map(([k,v])=>`${k}: ${v}`).join(', ');
        toast(tr('toast.update_done', {seconds:d.seconds, downloaded:d.downloaded, db_added:d.added, db_updated:d.updated})
              + (sk ? '\n' + tr('toast.update_skipped', {list:sk}) : ''), sk ? 'err' : 'ok', sk ? 12000 : undefined);
    }
    if(d.added || d.updated || d.downloaded) setTimeout(()=>location.reload(), 1800);
}
async function cancelUpdate() {
    document.getElementById('updStop').disabled=true;
    try { await fetch('/api/update_cancel', {method:'POST'}); } finally { document.getElementById('updStop').disabled=false; }
}
async function rebuildImage(btn) {
    if(!(await confirmDialog(T['msg.build_confirm'], T['dialog.yes_build']))) return;
    const overwrite = await confirmDialog(T['msg.build_overwrite'], T['dialog.overwrite_all'], T['dialog.only_missing']);
    const old = btn.innerText;
    btn.disabled = true; btn.innerText = T['status.building']; setBusy(true);
    try {
        const d = await (await fetch('/api/rebuild_image', {method:'POST', body:JSON.stringify({overwrite})})).json();
        setBusy(false); btn.disabled=false; btn.innerText=old;
        if(d.status==='ok') {
            toast(tr('toast.build_done', {seconds:d.seconds, added:d.added, updated:d.updated, files:d.files, mb:(d.size/1048576).toFixed(1), checksum_ok:d.checksum_ok, image:d.image}), 'ok', 12000);
            refreshPkgStatus();
            window.location = '/download/' + d.image;
        } else { toast(T['err.prefix'] + JSON.stringify(d), 'err'); }
    } catch(e) { setBusy(false); btn.disabled=false; btn.innerText=old; toast(T['err.prefix'] + e, 'err'); }
}
// input sets (inputs/NN_*): list + swap the working img/db set without a restart
async function openInputs() {
    const body=document.getElementById('inputsBody');
    body.innerHTML=''; document.getElementById('inputsModal').style.display='flex'; setBusy(true);
    try {
        const d = await (await fetch('/api/inputs')).json();
        setBusy(false);
        document.getElementById('inputsDir').textContent = d.inputs_dir;
        if(!d.sets.length) { body.innerHTML=`<tr><td colspan="7">${T['inputs.empty']}</td></tr>`; return; }
        d.sets.forEach(s=>{
            const r=document.createElement('tr'); if(s.active) r.className='active';
            const cells=[s.name, s.version, `${s.img_mb} MB · ${s.img_date}`, s.fm_rows ?? '—', s.dab_rows ?? '—', s.info];
            cells.forEach((v,i)=>{ const td=document.createElement('td'); td.textContent=v; if(i===0||i===1) td.className='mono'; if(i===5) td.className='info'; r.appendChild(td); });
            const td=document.createElement('td'), b=document.createElement('button');
            b.textContent = s.active ? T['inputs.reload'] : T['inputs.load'];
            if(s.active) b.className='secondary';
            b.onclick=()=>loadInputSet(s.name);
            td.appendChild(b); r.appendChild(td); body.appendChild(r);
        });
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
function closeInputs() { document.getElementById('inputsModal').style.display='none'; }
async function loadInputSet(name) {
    if(!(await confirmDialog(tr('msg.load_confirm', {name:name}), T['dialog.yes_load']))) return;
    setBusy(true);
    try {
        const d = await (await fetch('/api/load_inputs', {method:'POST', body:JSON.stringify({name})})).json();
        if(d.status!=='ok') { setBusy(false); return toast(T['err.load']+(d.error||JSON.stringify(d)),'err'); }
        toast(tr('toast.loaded', {name:d.name, seconds:d.seconds}),'ok');
        setTimeout(()=>location.reload(), 900);
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
// ---- manual station editor (add / edit rows, display names, CSV import/export) ----
function _rowData(btn) { return btn.closest('tr').querySelector('.row-check').dataset; }
function triggerUploadRow(b) { const d=_rowData(b); triggerUpload(d.id, d.type, d.ecc, d.freq); }
function openGalleryRow(b) { const d=_rowData(b); openGallery(d.id, d.type, d.ecc, d.freq, d.slu, d.name, d.logo); }
let ST_EDIT=null;
const _OWN = ['1','2'];
function _countryLabel() { const c=UI_CFG.country||'', n=(UI_CFG.countries||{})[c]; return n ? `${n} (${c})` : c; }
function addStation() { openStationDialog(null, null); }
function editStation(b) { openStationDialog(_rowData(b), b.closest('tr')); }
function openStationDialog(d, row) {
    ST_EDIT = {d, row};
    const digit = (UI_CFG.country||'').split('-')[1] || '';
    const g = id => document.getElementById(id);
    g('stTitle').textContent = d ? T['st.edit'] : T['st.add'];
    g('stCountry').textContent = _countryLabel();
    g('stType').value = d ? d.type : 'FM'; g('stType').disabled = !!d;
    // new station: empty field with the country's first PI digit as a hint (a pre-filled digit
    // plus maxlength=4 silently turned a typed "3ABC" into "33AB")
    g('stPi').value = d ? d.id : '';
    g('stPi').placeholder = digit ? digit + '…' : '';
    g('stFreq').value = (d && d.type==='FM' && parseInt(d.freq,10)>0) ? String(parseInt(d.freq,10)/1000) : '';
    g('stName').value = d ? (d.name||'') : '';
    const factory = !!d && !_OWN.includes(d.rt);
    g('stPi').disabled = g('stFreq').disabled = factory;
    g('stFactoryNote').hidden = !factory;
    g('stSaveLogo').hidden = !!d;
    ['stPi','stFreq'].forEach(i=>g(i).classList.remove('bad'));
    stTypeChanged();
    g('stationModal').style.display='flex';
    const first = factory ? g('stName') : g('stPi'); first.focus(); first.select();
}
function stTypeChanged() { document.getElementById('stFreqRow').hidden = document.getElementById('stType').value==='DAB'; }
function closeStation() { document.getElementById('stationModal').style.display='none'; }
async function _postStation(body) {
    const r = await fetch('/api/station', {method:'POST', body:JSON.stringify(body)});
    return r.json();
}
async function saveStation(pickLogo) {
    const g = id => document.getElementById(id), d = ST_EDIT.d;
    const body = {action: d ? 'update' : 'add', country: UI_CFG.country, type: g('stType').value,
                  pi: g('stPi').value.trim().toUpperCase(), freq: g('stFreq').value.trim(), name: g('stName').value.trim()};
    if(d) { body.rowid = d.rowid; body.rt = d.rt; }
    ['stPi','stFreq'].forEach(i=>g(i).classList.remove('bad'));
    try {
        let res = await _postStation(body);
        if(res.status==='confirm') {
            if(!(await confirmDialog(res.message, T['st.save']))) return;
            body.confirm = true; res = await _postStation(body);
        }
        if(res.status!=='ok') {
            const field = {bad_pi:'stPi', bad_freq:'stFreq', duplicate: body.type==='FM' ? 'stFreq' : 'stPi'}[res.code];
            if(field) g(field).classList.add('bad');
            return toast(res.message || JSON.stringify(res), 'err');
        }
        const tmp = document.createElement('tbody'); tmp.innerHTML = res.row_html.trim();
        const row = tmp.firstElementChild;
        if(row) {
            if(ST_EDIT.row) { row.dataset.idx = ST_EDIT.row.dataset.idx; ST_EDIT.row.replaceWith(row); }
            else {
                row.dataset.idx = -1; document.querySelector('#stationTable tbody').prepend(row); currentPage = 1;
                // keep the country list's row count in step with the table (no page reload needed)
                const cnt = UI_CFG.country_rows || {}, k = UI_CFG.country;
                if(k) { cnt[k] = (cnt[k] || 0) + 1; UI_CFG.country_rows = cnt; fillCountryFilter(); }
            }
        }
        let other = null;   // saved, but under another country (PI from another country digit): not in this view
        if(!row) {
            const ecc = String(UI_CFG.country||'').split('-')[0];
            other = body.pi ? `${ecc}-${body.pi[0]}` : null;
            if(ST_EDIT.row) ST_EDIT.row.remove();
            const cnt = UI_CFG.country_rows || {};
            if(other) {      // the row now counts for that country (an edited one leaves this one)
                cnt[other] = (cnt[other] || 0) + 1;
                if(d && cnt[UI_CFG.country]) cnt[UI_CFG.country] -= 1;
                UI_CFG.country_rows = cnt; fillCountryFilter();
            }
        }
        filterTable(); schedulePkgStatus(); refreshChangesBadge();
        closeStation();
        if(other) {
            const names = UI_CFG.countries || {};
            toast(tr('st.saved_other_country', {country: other, name: names[other] || '?'}), 'ok', 10000);
        } else toast(d ? T['st.saved'] : T['st.added'], 'ok');
        if(pickLogo && row) openGalleryRow(row.querySelector('.actions button.secondary'));
    } catch(e) { toast(T['err.prefix']+e, 'err'); }
}
function exportStations() { location.href = '/api/stations/export?country=' + encodeURIComponent(UI_CFG.country||''); }
let IMPORT=null;
function _reason(code) { return T['err.st_'+code] || code; }
async function importStations(inp) {
    const f = inp.files && inp.files[0]; inp.value = '';
    if(!f) return;
    const text = await f.text();
    setBusy(true);
    try {
        const d = await (await fetch('/api/stations/import', {method:'POST', body:JSON.stringify({csv:text, dry_run:true})})).json();
        setBusy(false);
        if(d.status!=='ok') return toast(d.message || JSON.stringify(d), 'err');
        IMPORT = {text, token:d.token};
        const esc = s => String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
        const item = it => `${it.type} ${it.ecc} ${it.pi}${it.frequency ? ' · '+it.frequency+' MHz' : ''}${it.logo ? ' · '+esc(it.logo) : ''}`;
        let h = `<p class="imp-sum">${tr('st.import_summary', {add:`<b>${d.add.length}</b>`, update:`<b>${d.update.length}</b>`,
                 names:`<b>${d.names}</b>`, skipped:`<b>${d.skipped.length}</b>`, rejected:`<b>${d.rejected.length}</b>`})}</p>`;
        if(d.add.length) h += `<h4>${T['st.import_add']}</h4><ul class="imp-list">${d.add.slice(0,200).map(it=>`<li>${item(it)}</li>`).join('')}</ul>`;
        if(d.update.length) h += `<h4>${T['st.import_update']}</h4><ul class="imp-list">${d.update.slice(0,200).map(it=>`<li>${item(it)}</li>`).join('')}</ul>`;
        if(d.rejected.length) h += `<h4>${T['st.import_rejected']}</h4><ul class="imp-list">${d.rejected.slice(0,200).map(r=>`<li>${tr('st.import_line', {line:r.line})}: ${esc(_reason(r.reason))}</li>`).join('')}</ul>`;
        if(d.skipped.length) h += `<p class="small">${tr('st.import_skipped_note', {n:d.skipped.length})}</p>`;
        h += `<p class="small">${T['st.import_backup_note']}</p>`;
        document.getElementById('importBody').innerHTML = h;
        document.getElementById('importGo').disabled = !(d.add.length || d.update.length || d.names);
        document.getElementById('importModal').style.display = 'flex';
    } catch(e) { setBusy(false); toast(T['err.prefix']+e, 'err'); }
}
function closeImport() { document.getElementById('importModal').style.display='none'; IMPORT=null; }
async function confirmImport(btn) {
    if(!IMPORT) return;
    btn.disabled = true; setBusy(true);
    try {
        const d = await (await fetch('/api/stations/import', {method:'POST', body:JSON.stringify({csv:IMPORT.text, token:IMPORT.token, dry_run:false})})).json();
        if(d.status!=='ok') { setBusy(false); btn.disabled=false; return toast(d.message || JSON.stringify(d), 'err'); }
        toast(tr('st.import_done', {add:d.add.length, update:d.update.length, names:d.names}), 'ok');
        setTimeout(()=>location.reload(), 900);
    } catch(e) { setBusy(false); btn.disabled=false; toast(T['err.prefix']+e, 'err'); }
}
// what the USB package's image lacks vs the DBs (missing / differs / absent), per row + summary
let PKG=null, _pkgTimer=null;
function schedulePkgStatus() { clearTimeout(_pkgTimer); _pkgTimer=setTimeout(refreshPkgStatus, 800); }
async function refreshPkgStatus() {
    try {
        const r = await fetch('/api/pkg_status');
        if(r.status===409) return void (_pkgTimer=setTimeout(refreshPkgStatus, 3000));
        const d = await r.json(); if(d.status!=='ok') return;
        const verChanged = PKG ? PKG.ver!==d.ver : (d.ver!==UI_CFG.img_ver_logos);
        PKG = d;
        const st={}; ['missing','differs','pending','absent'].forEach(k=>d[k].forEach(n=>st[n]=k));
        const inImg = d.kind==='built' ? 'built' : 'img';
        document.querySelectorAll('#stationTable tbody tr').forEach(row=>{
            const c=row.querySelector('.row-check'), fn=(row.cells[7]?row.cells[7].textContent.trim():'').toUpperCase();
            const k = fn ? (st[`${(c.dataset.ecc||'').toUpperCase()}/${fn.endsWith('.PNG')?fn:fn+'.PNG'}`]||'') : '';
            row.dataset.pkg=k;
            // preview source mirrors server logo_source(): the 🎨 pick (a build puts it in), else the
            // package image, else logos_sync5, else none — i.e. what the USB package holds after a build
            const pick = fn ? (d.picks||{})[`${(c.dataset.ecc||'').toUpperCase()}/${fn.endsWith('.PNG')?fn:fn+'.PNG'}`] : null;
            const where = !fn ? null : (pick ? (pick==='image' ? 'img' : pick) : (k==='missing' ? 'sync5' : (k==='absent' ? 'none' : inImg)));
            const stag=row.querySelector('.src-tag');
            if(stag && where) { stag.className='src-tag src-'+where; stag.textContent=T['src.'+where]; stag.title=T['src.'+where+'_title']; }
            const im=row.querySelector('img.logo-preview');
            // same URL, other file: bump it so the browser does not keep the old picture
            // no logo name = nothing to fetch (never request /logos/E2/.PNG)
            if(im && fn && (verChanged || (row.dataset.src && row.dataset.src!==where))) {
                im.style.opacity=1; im.src=im.src.replace(/([?&]v=)[^&]*/, '$1'+d.ver).replace(/&s=[^&]*/, '')+'&s='+where+Date.now();
            }
            row.dataset.src=where||'';
            const tag=row.querySelector('.pkg-tag'); if(!tag) return;
            // absent: the src tag already says "none"
            tag.className='pkg-tag'+(k && k!=='absent' ? ' pkg-'+k : '');
            tag.textContent = (k && k!=='absent') ? T['pkg.tag_'+k] : '';
            tag.title = (k && k!=='absent') ? tr('pkg.tag_'+k+'_title', {image:d.image}) : '';
            tag.onclick = (k==='differs' || k==='pending') ? (()=>openVersions(fn.endsWith('.PNG')?fn:fn+'.PNG')) : null;
        });
        const el=document.getElementById('pkgStatus');
        const kind=T['pkg.kind_'+d.kind];
        const bad = d.missing.length || d.differs.length || d.pending.length;
        el.className='pkg-status'+(bad?' warn':' ok');
        el.textContent = bad
            ? tr('pkg.summary_warn', {image:d.image, kind:kind, missing:d.missing.length, differs:d.differs.length, pending:d.pending.length})
            : tr('pkg.summary_ok', {image:d.image, kind:kind});
        document.getElementById('verCount').textContent = (d.differs.length||d.pending.length) ? `(${d.differs.length+d.pending.length})` : '';
        if(document.getElementById('pkgFilter').value) filterTable();
        scheduleChanges();
    } catch(e) { console.warn('pkg_status', e); }
}
function showPkgProblems() {
    if(!PKG || !(PKG.missing.length||PKG.differs.length||PKG.pending.length)) return;
    document.getElementById('pkgFilter').value='problem'; clearRowChecks(); filterTable(); resetPage(); saveState();
}
// per-file version picker: loaded image vs logos_sync5 for files both hold in different versions
let VERS=[], VER_EDIT={};
async function openVersions(q) {
    setBusy(true);
    try {
        const r = await fetch('/api/logo_versions'), d = await r.json();
        setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
        VERS=d.items; VER_EDIT={}; VERS.kind=d.kind; VERS.image=d.image;
        document.getElementById('verSearch').value = typeof q==='string' ? q : '';
        if(typeof q==='string') document.getElementById('verFilter').value='';
        document.getElementById('versionsModal').style.display='flex';
        renderVersions();
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
function closeVersions() { document.getElementById('versionsModal').style.display='none'; }
function _verChoice(it) { return (it.key in VER_EDIT) ? VER_EDIT[it.key] : it.choice; }
function _verState(it) {
    const c=_verChoice(it);
    if(!c) return 'undecided';
    return c===it.in_pkg ? c : 'pending';
}
function _verShown() {
    const f=document.getElementById('verFilter').value, q=document.getElementById('verSearch').value.toUpperCase();
    return VERS.filter(it=>{
        const st=_verState(it), c=_verChoice(it);
        if(f==='undecided' && c) return false;
        if(f==='pending' && st!=='pending') return false;
        if(['image','sync5','styled'].includes(f) && c!==f) return false;
        return !q || (it.key+' '+it.stations.join(' ')).toUpperCase().includes(q);
    });
}
function renderVersions() {
    const g=document.getElementById('verGrid'); g.innerHTML='';
    _verShown().forEach(it=>{
        const c=_verChoice(it), card=document.createElement('div');
        card.className='ver-card ver-'+_verState(it);
        const h=document.createElement('div'); h.className='ver-head';
        h.textContent=`${it.key} · ${tr('ver.rows', {n:it.rows})}`;
        const n=document.createElement('div'); n.className='ver-names'; n.textContent=it.stations.join(', '); n.title=n.textContent;
        card.appendChild(h); card.appendChild(n);
        const opts=document.createElement('div'); opts.className='ver-opts';
        [['image', it.img_url, T['ver.opt_image']], ['sync5', it.sync5_url, T['ver.opt_sync5']], ['styled', it.styled_url, T['ver.opt_styled']]]
          .filter(o=>o[1]).forEach(([v,url,label])=>{
            const o=document.createElement('div'); o.className='ver-opt ver-opt-'+v+(c===v?' chosen':'');
            const im=document.createElement('img'); im.src=url; im.loading='lazy';
            const l=document.createElement('div'); l.className='ver-label'; l.textContent=label;
            if(it.in_pkg===v) { const b=document.createElement('span'); b.className='ver-inpkg'; b.textContent=T['ver.in_pkg']; b.title=tr('ver.in_pkg_title', {image:VERS.image}); l.appendChild(b); }
            o.appendChild(im); o.appendChild(l);
            o.onclick=()=>{ VER_EDIT[it.key] = (c===v) ? null : v; renderVersions(); };
            opts.appendChild(o);
        });
        card.appendChild(opts); g.appendChild(card);
    });
    const all=VERS.map(_verState), cnt=k=>all.filter(x=>x===k).length;
    const changed=Object.keys(VER_EDIT).filter(k=>VER_EDIT[k]!==(VERS.find(i=>i.key===k)||{}).choice).length;
    document.getElementById('verInfo').textContent = tr('ver.info', {total:VERS.length, undecided:cnt('undecided'), pending:cnt('pending'), changed:changed, image:VERS.image});
}
function bulkVersions(v) { _verShown().forEach(it=>VER_EDIT[it.key]=v); renderVersions(); }
async function saveVersions(build) {
    const ch={}; Object.keys(VER_EDIT).forEach(k=>{ const it=VERS.find(i=>i.key===k); if(it && VER_EDIT[k]!==it.choice) ch[k]=VER_EDIT[k]; });
    setBusy(true);
    try {
        if(Object.keys(ch).length) {
            const d = await (await fetch('/api/logo_versions', {method:'POST', body:JSON.stringify({choices:ch})})).json();
            if(d.status!=='ok') { setBusy(false); return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err'); }
        }
        if(build) {
            // overwrite=false: picked files follow their pick, undecided keep the image version (= current preview)
            const b = await (await fetch('/api/rebuild_image', {method:'POST', body:JSON.stringify({overwrite:false})})).json();
            if(b.status!=='ok') { setBusy(false); return toast(T['err.prefix']+(b.error||JSON.stringify(b)),'err'); }
            toast(tr('toast.ver_built', {saved:Object.keys(ch).length, added:b.added, updated:b.updated, seconds:b.seconds, image:b.image}),'ok',10000);
        } else toast(tr('toast.ver_saved', {saved:Object.keys(ch).length}),'ok');
        setBusy(false); closeVersions(); refreshPkgStatus();
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
// dark preview background (as on the car): white styled logos vanish on white
// background behind every logo preview (table, zoom, 🎨, 🖌): Sync 3-like presets (approximations of the
// car's screens), a custom colour or a custom image (e.g. a photo of the car's radio screen; kept in this browser)
const LOGO_BGS = {
    none: '', dark: '#1a1f2b', white: '#ffffff',
    sync_black: 'linear-gradient(180deg, #1a1d22 0%, #08090b 100%)',
    sync_tile: 'linear-gradient(180deg, #3a3f47 0%, #262a30 100%)',
    sync_blue: 'linear-gradient(180deg, #123463 0%, #071a36 100%)',
    sync_grey: 'linear-gradient(180deg, #eef0f2 0%, #c9ced4 100%)',
    checker: 'repeating-conic-gradient(#8a8a8a 0% 25%, #bdbdbd 0% 50%) 50% / 16px 16px',
};
function _bgImage() { try { return localStorage.getItem('lu_bgimg') || ''; } catch(e) { return ''; } }
function applyLogoBg() {
    const v=document.getElementById('logoBg').value, col=document.getElementById('logoBgColor');
    col.style.display = v==='color' ? '' : 'none';
    let bg = LOGO_BGS[v] || '';
    if(v==='color') bg=col.value;
    if(v==='image') { const im=_bgImage(); bg = im ? `url("${im}") center / cover` : ''; }
    document.body.style.setProperty('--logo-bg', bg || 'transparent');
    document.body.classList.toggle('has-logo-bg', !!bg);
}
function logoBgChanged() {
    const v=document.getElementById('logoBg').value;
    if(v==='image') document.getElementById('logoBgFile').click();   // pick (or replace) the picture
    applyLogoBg(); saveState();
}
function logoBgFile(inp) {
    const f=inp.files && inp.files[0]; inp.value='';
    if(!f) return;
    const img=new Image(), url=URL.createObjectURL(f);
    img.onload=()=>{
        // downscale so it fits in localStorage (a few hundred kB)
        const k=Math.min(1, 640/Math.max(img.width, img.height)), c=document.createElement('canvas');
        c.width=Math.round(img.width*k); c.height=Math.round(img.height*k);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
        try { localStorage.setItem('lu_bgimg', c.toDataURL('image/jpeg', 0.85)); }
        catch(e) { toast(T['ctrl.bg_image_err'],'err'); }
        applyLogoBg();
    };
    img.onerror=()=>{ URL.revokeObjectURL(url); toast(T['ctrl.bg_image_err'],'err'); };
    img.src=url;
}
// 🖌 logo style: a styled copy of each file in scope (white without background, tile, bevel...)
// becomes its third version ("styled" pick in 🎨); the build puts it into the image
const STYLE_SPEC = [['remove_bg','check'], ['tolerance','range',0,120],
    ['bg_pick','select',['auto','white','black','custom']], ['bg_custom','color'], ['bg_scope','select',['holes','edge','all']], ['plate','select',['auto','outline','off']],
    ['mode','select',['white','black','keep']], ['opacity','range',5,100], ['margin','range',0,40],
    ['upscale','range',100,300], ['bevel','range',0,8], ['shadow','range',0,10],
    ['shape','select',['none','rect','rounded','square','circle']], ['radius','range',0,60], ['fill','color'],
    ['fade','range',0,60], ['fade_shape','select',['circle','square','rect']],
    ['ai_style','select',['flat','outline','neon','color','metal','emboss']], ['ai_extra','text']];   // ai_*: the 🤖 prompt only
const STYLE = {info:null, preset:'white', params:{}, items:[], total:0, excl:new Set(), force:new Set(), timer:null, seq:0,
    ai:{}, aiProvider:'', aiRunning:false, aiStop:false};   // ai: key -> AI preview item (🤖)
function _rowKey(row) {
    const c=row.querySelector('.row-check'), fn=(row.cells[7]?row.cells[7].textContent.trim():'').toUpperCase();
    return fn ? `${(c.dataset.ecc||'').toUpperCase()}/${fn.endsWith('.PNG')?fn:fn+'.PNG'}` : '';
}
function _keysOf(rows) { return [...new Set(rows.map(_rowKey).filter(Boolean))]; }
function _checkedRows() { return Array.from(document.querySelectorAll('.row-check:checked')).map(c=>c.closest('tr')); }
function _styleScope() {
    const v=(document.querySelector('input[name=styleScope]:checked')||{}).value;
    if(v==='country') return {country: document.getElementById('styleCountry').value};
    return {keys: _keysOf(v==='selected' ? _checkedRows() : getVisible())};
}
async function openStyle() {
    setBusy(true);
    try {
        const d = await (await fetch('/api/style_info')).json(); setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
        STYLE.info=d; _styleCount(d.styled);
        const sel=_keysOf(_checkedRows());
        document.getElementById('styleSelN').textContent=sel.length;
        document.getElementById('styleFiltN').textContent=_keysOf(getVisible()).length;
        const cs=document.getElementById('styleCountry'); cs.innerHTML='';
        d.countries.forEach(c=>{ const o=document.createElement('option'); o.value=c.code;
            o.textContent=`${c.name||'?'} (${c.code}) · ${tr('style.files', {n:c.files})}`; cs.appendChild(o); });
        if(d.countries.find(c=>c.code==='E2-3')) cs.value='E2-3';
        document.querySelector(`input[name=styleScope][value=${sel.length?'selected':'country'}]`).checked=true;
        let last={}; try { last=JSON.parse(localStorage.getItem('lu_style')||'{}'); } catch(e) {}
        STYLE.preset = (last.preset && d.presets[last.preset]) ? last.preset : 'white';
        STYLE.params = Object.assign({}, d.presets[STYLE.preset], last.preset===STYLE.preset ? (last.params||{}) : {});
        _fillPresets(d.presets); renderStyleParams();
        const pv=document.getElementById('styleAiProv');
        Array.from(pv.options).forEach(o=>{ o.disabled=!d.ai.available[o.value]; o.textContent=(o.value==='kie'?'kie.ai':'Gemini')+(d.ai.available[o.value]?'':' — '+T['style.ai_no_key_short']); });
        pv.value = d.ai.available[d.ai.provider] ? d.ai.provider : (Object.keys(d.ai.available).find(p=>d.ai.available[p]) || d.ai.provider);
        STYLE.items=[]; STYLE.total=0; STYLE.excl.clear(); STYLE.force.clear(); STYLE.ai={};
        document.getElementById('styleModal').style.display='flex';
        _styleAiLabel(); renderStyleGrid(); stylePreview(true);
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
function _styleCount(n) { document.getElementById('styleCount').textContent = n ? `(${n})` : ''; }
async function refreshStyleCount() { try { const d=await (await fetch('/api/style_info')).json(); if(d.status==='ok') _styleCount(d.styled); } catch(e) {} }
function closeStyle() { document.getElementById('styleModal').style.display='none'; }
function _fillPresets(presets) {
    const ps=document.getElementById('stylePreset'); ps.innerHTML='';
    Object.keys(presets).forEach(k=>{ const o=document.createElement('option'); o.value=k;
        o.textContent = T['style.preset_'+k] || k; ps.appendChild(o); });
    ps.value=STYLE.preset;
    document.getElementById('stylePresetDel').disabled = STYLE.info.builtin.includes(STYLE.preset);
}
function stylePresetPick() {
    STYLE.preset=document.getElementById('stylePreset').value;
    STYLE.params=Object.assign({}, STYLE.info.presets[STYLE.preset]);
    document.getElementById('stylePresetDel').disabled = STYLE.info.builtin.includes(STYLE.preset);
    renderStyleParams(); stylePreview(true);
}
function renderStyleParams() {
    const box=document.getElementById('styleParams'); box.innerHTML='';
    STYLE_SPEC.forEach(([k,type,a,b])=>{
        const l=document.createElement('label'); l.title=T['style.p_'+k+'_title']||'';
        const n=document.createElement('span'); n.className='sp-name'; n.textContent=T['style.p_'+k]||k; l.appendChild(n);
        let inp;
        if(type==='check') { inp=document.createElement('input'); inp.type='checkbox'; inp.checked=!!STYLE.params[k]; }
        else if(type==='select') { inp=document.createElement('select');
            a.forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=T['style.o_'+v]||v; inp.appendChild(o); });
            inp.value=STYLE.params[k]; }
        else if(type==='color') { inp=document.createElement('input'); inp.type='color'; inp.value=STYLE.params[k]; }
        else if(type==='text') { inp=document.createElement('input'); inp.type='text'; inp.maxLength=300; inp.value=STYLE.params[k]||'';
            inp.placeholder=T['style.p_'+k+'_ph']||''; l.classList.add('sp-wide'); }
        else { inp=document.createElement('input'); inp.type='range'; inp.min=a; inp.max=b; inp.value=STYLE.params[k]; }
        const val=document.createElement('span'); val.className='sp-val'; if(type==='range') val.textContent=inp.value;
        inp.oninput=()=>{
            STYLE.params[k] = type==='check' ? inp.checked : (type==='range' ? parseInt(inp.value) : inp.value);
            if(type==='range') val.textContent=inp.value;
            clearTimeout(STYLE.timer); STYLE.timer=setTimeout(()=>stylePreview(true), type==='text' ? 1200 : 500);
        };
        if(k.startsWith('ai_')) l.classList.add('sp-ai');
        if(type==='check') { l.insertBefore(inp, n); } else l.appendChild(inp);
        if(type==='range') l.appendChild(val);
        box.appendChild(l);
    });
}
function styleScopeChanged() { STYLE.excl.clear(); STYLE.force.clear(); STYLE.ai={}; _styleAiLabel(); stylePreview(true); }
async function stylePreview(reset) {
    const scope=_styleScope(), seq=++STYLE.seq;
    if(reset) { STYLE.items=[]; STYLE.total=0; }
    if(scope.keys && !scope.keys.length) { renderStyleGrid(); return; }
    document.getElementById('styleInfo').textContent=T['style.working'];
    try {
        const r = await fetch('/api/style_preview', {method:'POST', body:JSON.stringify(Object.assign({params:STYLE.params, offset:STYLE.items.length, limit:40}, scope))});
        const d = await r.json();
        if(seq!==STYLE.seq) return;            // a newer preview was started meanwhile
        if(d.status!=='ok') { document.getElementById('styleInfo').textContent=''; return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err'); }
        STYLE.items=STYLE.items.concat(d.items); STYLE.total=d.total;
        _mergeAi(); renderStyleGrid();
        // parameters changed: re-render the AI versions from the cache (no new paid calls)
        if(reset && Object.keys(STYLE.ai).length && !STYLE.aiRunning) _aiFetch(Object.keys(STYLE.ai), true, seq);
    } catch(e) { toast(T['err.prefix']+e,'err'); }
}
function _mergeAi() {
    // AI versions replace the local preview of the same file; AI files not on the shown pages go first
    const shown=new Set(STYLE.items.map(i=>i.key));
    STYLE.items=STYLE.items.map(i=>STYLE.ai[i.key]||i);
    const extra=Object.keys(STYLE.ai).filter(k=>!shown.has(k)).map(k=>STYLE.ai[k]);
    STYLE.items=extra.concat(STYLE.items);
}
async function _aiFetch(keys, cachedOnly, seq) {
    const prov=STYLE.aiProvider || document.getElementById('styleAiProv').value;
    for(let i=0; i<keys.length; i+=6) {
        if(STYLE.aiStop || (seq && seq!==STYLE.seq)) break;
        const r = await fetch('/api/style_ai', {method:'POST', body:JSON.stringify({keys:keys.slice(i,i+6), params:STYLE.params, provider:prov, cached_only:cachedOnly})});
        if(r.status===409) { await new Promise(res=>setTimeout(res,2000)); i-=6; continue; }
        const d=await r.json();
        if(d.status!=='ok') { toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err'); break; }
        if(seq && seq!==STYLE.seq) break;
        let gone=false;
        d.items.forEach(it=>{
            // other AI style / prompt text: no cached answer for it -> back to the local version
            if(cachedOnly && it.error==='not cached') { delete STYLE.ai[it.key]; STYLE.force.delete(it.key); gone=true; return; }
            if(cachedOnly && it.error) return;             // keep what we had
            STYLE.ai[it.key]=it;
            // a good AI result is meant to be applied: force it past "Skip ⚠"
            if(!it.error && !it.warn.length && !cachedOnly) STYLE.force.add(it.key);
        });
        if(gone) { stylePreview(true); return; }       // re-fetch the local previews it replaced
        _mergeAi(); renderStyleGrid();
        if(!cachedOnly) document.getElementById('styleInfo').textContent=tr('style.ai_progress', {done:Math.min(i+6,keys.length), total:keys.length});
    }
}
// AI scope: one card (keys), every selected row (scope "selected"), otherwise only the ⚠ ones
function _styleAiAll() { return (document.querySelector('input[name=styleScope]:checked')||{}).value==='selected'; }
function _styleAiLabel() {
    if(STYLE.aiRunning) return;
    document.getElementById('styleAiBtn').textContent = _styleAiAll()
        ? tr('style.ai_btn_sel', {n:document.getElementById('styleSelN').textContent}) : T['style.ai_btn'];
}
async function styleAi(keys) {
    const btn=document.getElementById('styleAiBtn');
    if(STYLE.aiRunning) { if(!keys) STYLE.aiStop=true; return; }
    const prov=document.getElementById('styleAiProv').value, scope=keys ? {keys:keys} : _styleScope();
    const all=!!keys || _styleAiAll();
    if(!STYLE.info.ai.available[prov]) return toast(T['style.ai_no_key'],'err',10000);
    document.getElementById('styleInfo').textContent=T['style.ai_scanning'];
    const d = await (await fetch('/api/style_ai', {method:'POST', body:JSON.stringify(Object.assign({scan:true, all:all, params:STYLE.params, provider:prov}, scope))})).json();
    if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
    if(!d.keys.length) { renderStyleGrid(); return toast(T['style.ai_none'],'ok'); }
    const pay=d.keys.length-d.cached;
    const cost = d.price!=null ? '≈ $'+(pay*d.price).toFixed(2) : T['style.ai_cost_unknown'];
    if(!(await confirmDialog(trm(all ? 'style.ai_confirm_sel' : 'style.ai_confirm', {n:d.keys.length, cached:d.cached, pay:pay, cost:cost, provider:prov==='kie'?'kie.ai':'Gemini'})))) { renderStyleGrid(); return; }
    STYLE.aiProvider=prov; STYLE.aiRunning=true; STYLE.aiStop=false; btn.textContent=T['style.ai_stop'];
    try { await _aiFetch(d.keys, false, 0); }
    catch(e) { toast(T['err.prefix']+e,'err'); }
    finally { STYLE.aiRunning=false; STYLE.aiStop=false; _styleAiLabel(); renderStyleGrid(); }
}
function _styleOff(it) {
    if(it.error || STYLE.excl.has(it.key)) return true;
    return it.warn.length>0 && document.getElementById('styleSkipWarn').checked && !STYLE.force.has(it.key);
}
function renderStyleGrid() {
    const g=document.getElementById('styleGrid'); g.innerHTML='';
    STYLE.items.forEach(it=>{
        const card=document.createElement('div'); card.className='ver-card style-card'+(_styleOff(it)?' st-off':'')+(it.ai?' st-ai':'');
        const h=document.createElement('div'); h.className='ver-head'; h.textContent=it.key;
        if(it.ai) { const a=document.createElement('span'); a.className='style-ai-tag'; a.textContent='🤖 '+(it.ai==='kie'?'kie.ai':'Gemini')+(it.cached?' · '+T['style.ai_cached']:''); h.appendChild(a); }
        if(!it.error || it.ai) { const b=document.createElement('button'); b.className='style-ai-one'; b.textContent='🤖';
            b.title=T['style.ai_one_title']; b.onclick=e=>{ e.stopPropagation(); styleAi([it.key]); }; h.appendChild(b); }
        if(it.warn.length) { const w=document.createElement('span'); w.className='style-warn'; w.textContent='⚠ '+it.warn.map(x=>T['style.warn_'+x]||x).join(', '); h.appendChild(w); }
        const n=document.createElement('div'); n.className='ver-names'; n.textContent=it.stations.join(', ') || '\u00a0'; n.title=n.textContent;
        card.appendChild(h); card.appendChild(n);
        const opts=document.createElement('div'); opts.className='ver-opts';
        [['before', it.before, tr('style.before', {base:T['style.base_'+it.base]||it.base||'-'})], ['after', it.after, it.error||(it.ai?T['style.after_ai']:T['style.after'])]].forEach(([cls,src,label])=>{
            const o=document.createElement('div'); o.className='ver-opt st-'+cls;
            const im=document.createElement('img'); if(src) im.src=src;
            const l=document.createElement('div'); l.className='ver-label'; l.textContent=label;
            o.appendChild(im); o.appendChild(l); opts.appendChild(o);
        });
        card.appendChild(opts);
        card.title=T['style.card_title'];
        card.onclick=()=>{
            if(it.error) return;
            if(it.warn.length && document.getElementById('styleSkipWarn').checked && !STYLE.excl.has(it.key))
                STYLE.force.has(it.key) ? STYLE.force.delete(it.key) : STYLE.force.add(it.key);
            else STYLE.excl.has(it.key) ? STYLE.excl.delete(it.key) : STYLE.excl.add(it.key);
            renderStyleGrid();
        };
        g.appendChild(card);
    });
    if(STYLE.items.length < STYLE.total) {
        const m=document.createElement('div'); m.className='style-more';
        const b=document.createElement('button'); b.className='secondary'; b.textContent=tr('style.more', {n:STYLE.total-STYLE.items.length});
        b.onclick=()=>stylePreview(false); m.appendChild(b); g.appendChild(m);
    }
    const off=STYLE.items.filter(_styleOff).length;
    document.getElementById('styleInfo').textContent = STYLE.total
        ? tr('style.info', {total:STYLE.total, shown:STYLE.items.length, off:off}) : T['style.empty'];
}
async function styleApply(build) {
    const scope=_styleScope();
    if(!STYLE.total) return toast(T['style.empty'],'err');
    if(!(await confirmDialog(trm('style.confirm', {n:STYLE.total, preset:T['style.preset_'+STYLE.preset]||STYLE.preset})))) return;
    setBusy(true);
    try {
        const d = await (await fetch('/api/style_apply', {method:'POST', body:JSON.stringify(Object.assign({
            params:STYLE.params, preset:STYLE.preset, exclude:[...STYLE.excl], force:[...STYLE.force],
            ai:Object.keys(STYLE.ai).filter(k=>!STYLE.ai[k].error), provider:STYLE.aiProvider||undefined,
            skip_warn:document.getElementById('styleSkipWarn').checked}, scope))})).json();
        if(d.status!=='ok') { setBusy(false); return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err'); }
        try { localStorage.setItem('lu_style', JSON.stringify({preset:STYLE.preset, params:STYLE.params})); } catch(e) {}
        let msg=tr('toast.style_applied', {applied:d.applied, excluded:d.excluded, warned:d.warned, failed:d.failed.length});
        if(build) {
            const b = await (await fetch('/api/rebuild_image', {method:'POST', body:JSON.stringify({overwrite:false})})).json();
            if(b.status!=='ok') { setBusy(false); return toast(T['err.prefix']+(b.error||JSON.stringify(b)),'err'); }
            msg += '\n' + tr('toast.usb_built', {seconds:b.seconds, added:b.added, updated:b.updated});
        } else msg += '\n' + trm('toast.style_build_hint');
        setBusy(false); toast(msg,'ok',12000); closeStyle(); refreshPkgStatus(); refreshStyleCount();
        if(document.getElementById('logoBg').value==='none' && STYLE.params.mode==='white') { document.getElementById('logoBg').value='sync_black'; applyLogoBg(); saveState(); }
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
async function styleClear() {
    const scope=_styleScope();
    if(!(await confirmDialog(trm('style.clear_confirm')))) return;
    setBusy(true);
    try {
        const d = await (await fetch('/api/style_apply', {method:'POST', body:JSON.stringify(Object.assign({action:'clear'}, scope))})).json();
        setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
        toast(trm('toast.style_cleared', {n:d.cleared}),'ok'); refreshPkgStatus(); refreshStyleCount();
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
async function styleSavePreset() {
    const name=(prompt(T['style.preset_name'], STYLE.info.builtin.includes(STYLE.preset)?'':STYLE.preset)||'').trim();
    if(!name) return;
    const d = await (await fetch('/api/style_presets', {method:'POST', body:JSON.stringify({name, params:STYLE.params})})).json();
    if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
    STYLE.info.presets=d.presets; STYLE.preset=name; _fillPresets(d.presets); toast(T['toast.style_preset_saved'],'ok');
}
async function styleDeletePreset() {
    if(STYLE.info.builtin.includes(STYLE.preset)) return;
    const d = await (await fetch('/api/style_presets', {method:'POST', body:JSON.stringify({name:STYLE.preset, delete:true})})).json();
    if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
    STYLE.info.presets=d.presets; STYLE.preset='white'; _fillPresets(d.presets); stylePresetPick();
}
// USB package (SyncMyMod/autoinstall.sh + files/other/) for FMods Tools on the car
function _radio(name, value, label, checked, disabled, note, noteCls) {
    const l=document.createElement('label'); l.className='usb-opt'+(disabled?' disabled':'');
    const i=document.createElement('input'); i.type='radio'; i.name=name; i.value=value; i.checked=checked; i.disabled=!!disabled;
    l.appendChild(i); l.appendChild(document.createTextNode(' '+label));
    if(note) { const n=document.createElement('div'); n.className='usb-note '+(noteCls||''); n.textContent=note; l.appendChild(n); }
    return l;
}
function _reportNote(c) {
    if(!c) return ['',''];
    if(!c.missing && !c.differs && !c.pending) return [T['usb.note_ok'], 'ok'];
    return [tr('usb.note_warn', {missing:c.missing, differs:c.differs, pending:c.pending}), 'warn'];
}
async function openUsb() {
    setBusy(true);
    try {
        const r = await fetch('/api/usb_targets'), d = await r.json();
        setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
        const im=document.getElementById('usbImages'), tg=document.getElementById('usbTargets');
        im.innerHTML=''; tg.innerHTML='';
        const rb=d.report.built, rl=d.report.loaded, rAuto=d.report[d.auto];
        // default: build now whenever the would-be image misses logos the table shows
        const def = (rAuto && (rAuto.missing || rAuto.differs || rAuto.pending)) ? 'build' : d.auto;
        im.appendChild(_radio('usbImg','build', T['usb.img_build'], def==='build', false,
            tr('usb.note_build', {missing:rl?rl.missing:0, differs:rl?rl.differs:0}), 'ok'));
        const ow=document.createElement('label'); ow.className='usb-opt usb-sub';
        // off by default: overwriting swaps image logos for logos_sync5 ones that look different from the preview
        ow.innerHTML=`<input type="checkbox" id="usbOverwrite"> `;
        ow.appendChild(document.createTextNode(tr('usb.overwrite', {differs:rl?rl.differs:0})));
        im.appendChild(ow);
        im.appendChild(_radio('usbImg','built', d.images.built ? tr('usb.img_built', {date:d.images.built}) : T[d.stale ? 'usb.img_built_stale' : 'usb.img_built_none'], def==='built', !d.images.built, ..._reportNote(rb)));
        im.appendChild(_radio('usbImg','loaded', tr('usb.img_loaded', {date:d.images.loaded||'—'}), def==='loaded', false, ..._reportNote(rl)));
        tg.appendChild(_radio('usbTgt','', T['usb.tgt_zip'], true));
        d.targets.forEach(t=>tg.appendChild(_radio('usbTgt', t.path, tr('usb.tgt_stick', {path:t.path, fs:t.fstype, free:t.free_mb}) + (t.has_pkg ? ' — '+T['usb.has_pkg'] : ''))));
        if(!d.targets.length) { const s=document.createElement('div'); s.className='small'; s.style.color='#999'; s.textContent=T['usb.no_sticks']; tg.appendChild(s); }
        // undo package: ticked by default when the loaded files came from a SyncMyMod/backup folder
        const lp=((UI_CFG.session||{}).loaded||{}).path||'';
        document.getElementById('usbRestore').checked=/[\\/]backup([\\/]|$)/i.test(lp);
        usbRestoreChanged();
        document.getElementById('usbModal').style.display='flex';
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
function closeUsb() { document.getElementById('usbModal').style.display='none'; }
function usbRestoreChanged() {
    // "unchanged" wins over the image choice: grey the image options out while it is ticked
    const on=document.getElementById('usbRestore').checked;
    document.querySelectorAll('#usbImages input').forEach(i=>i.disabled=on);
    document.querySelector('#usbModal .usb-basic').hidden=on;
}
async function makeUsb(btn) {
    // basic mode: always build now, so the package holds exactly what the preview shows
    const adv=isAdvanced();
    const restore=document.getElementById('usbRestore').checked;
    const image=restore ? 'restore' : (adv ? document.querySelector('input[name=usbImg]:checked').value : 'build');
    const target=document.querySelector('input[name=usbTgt]:checked').value;
    const overwrite=adv && document.getElementById('usbOverwrite').checked;
    if(target && !(await confirmDialog(tr('msg.usb_confirm', {path:target}), T['usb.make']))) return;
    closeUsb(); setBusy(true);
    try {
        const d = await (await fetch('/api/usb_package', {method:'POST', body:JSON.stringify({image, target, overwrite})})).json();
        setBusy(false);
        if(d.status!=='ok') return toast(T['err.usb']+(d.error||JSON.stringify(d)),'err',12000);
        const img = tr(d.kind==='built' ? 'usb.kind_built' : (d.kind==='restore' ? 'usb.kind_restore' : 'usb.kind_loaded'), {image:d.image});
        const built = d.build ? '\n'+tr('toast.usb_built', {added:d.build.added, updated:d.build.updated, seconds:d.build.seconds}) : '';
        if(d.target) toast(tr('toast.usb_written', {path:d.target, image:img, files:d.img_files, seconds:d.seconds}) + built + (d.moved_old ? '\n'+tr('toast.usb_moved', {name:d.moved_old}) : '') + (d.kept_backup ? '\n'+T['toast.usb_kept_backup'] : '') + ((d.old_folders||[]).length ? '\n'+tr('toast.usb_old_folders', {n:d.old_folders.length, mb:d.old_folders.reduce((a,o)=>a+o.mb,0)}) : ''), 'ok', 15000);
        else { toast(tr('toast.usb_zip', {zip:d.zip, mb:d.zip_mb, image:img, files:d.img_files, seconds:d.seconds}) + built, 'ok', 15000); window.location='/download/'+d.zip; }
        refreshPkgStatus(); markStep(4);
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
// floating logo preview (tooltip) — outside the container, so not clipped
const _tip=document.getElementById('logoTip'), _tipImg=_tip.querySelector('img');
_tipImg.onerror=()=>{ _tip.style.display='none'; };
function _placeTip(x,y) {
    const w=_tip.offsetWidth||214, h=_tip.offsetHeight||214;
    let left=x+18, top=y+18;
    if(left+w>window.innerWidth) left=x-w-18;
    if(top+h>window.innerHeight) top=y-h-18;
    _tip.style.left=Math.max(4,left)+'px'; _tip.style.top=Math.max(4,top)+'px';
}
document.addEventListener('mouseover', e=>{
    const img=e.target.closest('.logo-preview'); if(!img) return;
    _tipImg.src=img.src; _tip.style.display='block'; _placeTip(e.clientX, e.clientY);
});
document.addEventListener('mousemove', e=>{ if(_tip.style.display==='block') _placeTip(e.clientX, e.clientY); });
document.addEventListener('mouseout', e=>{ if(e.target.closest('.logo-preview')) _tip.style.display='none'; });
document.addEventListener('DOMContentLoaded', ()=>{ applyI18n(); initAdvanced(); pollUpdate(); renderSession(UI_CFG.session); restoreState(); if(UI_CFG.session && UI_CFG.session.has_data) { refreshPkgStatus(); refreshStyleCount(); refreshChangesBadge(); }
    // deep links (docs, bookmarks): /#load, /#changes, /#usb, /#settings
    const open={'#load':openLoad, '#changes':openChanges, '#usb':openUsb, '#settings':openSettings}[location.hash];
    if(open) open(); });
// Esc closes the topmost open dialog (style above versions, etc.)
document.addEventListener('keydown', e=>{
    if(e.key!=='Escape') return;
    // topmost first: the confirm box (Esc = "No"), then the modals from the last in the page down
    // (same z-index, so a later one is drawn over an earlier one)
    const open=[['confirmModal',()=>document.getElementById('confirmNo').click()],
                ['settingsModal',closeSettings],['changesModal',closeChanges],['loadModal',closeLoad],['usbModal',closeUsb],
                ['styleModal',closeStyle],['versionsModal',closeVersions],['inputsModal',closeInputs],
                ['galleryModal',closeGallery],['editorModal',closeEditor],['importModal',closeImport],['stationModal',closeStation]]
        .find(([id])=>{ const m=document.getElementById(id); return m && m.style.display==='flex'; });
    if(open) { e.preventDefault(); open[1](); }
});

// ---- guided workflow: ① load → ② edit → ③ review → ④ USB package ----
function isAdvanced() { return document.body.classList.contains('advanced'); }
function setAdvanced(on) {
    document.body.classList.toggle('advanced', !!on);
    const t=document.getElementById('advToggle'); if(t) t.checked=!!on;
    try { localStorage.setItem('lu_advanced', on ? '1' : ''); } catch(e) {}
}
function initAdvanced() {
    let on=false; try { on=!!localStorage.getItem('lu_advanced'); } catch(e) {}
    setAdvanced(on);
}
function _esc(s) { return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function renderSession(s) {
    s = s || {};
    const ed=document.getElementById('editing'), L=s.loaded||{};
    document.body.classList.toggle('no-data', !s.has_data);
    document.getElementById('emptyState').hidden = !!s.has_data;
    if(!s.has_data) {
        ed.className='editing none'; ed.textContent=T['wf.nothing_loaded']; ed.title=ed.textContent;
    } else {
        const parts=[];
        if(s.firmware) parts.push(tr('wf.fw', {v:s.firmware}));
        if(s.rows) parts.push(tr('wf.rows', {fm:s.rows.fm??'—', dab:s.rows.dab??'—'}));
        if(L.time) parts.push(tr('wf.loaded_at', {t:L.time}));
        ed.className='editing';
        ed.innerHTML = `${_esc(T['wf.editing'])} <b>${_esc(L.label || T['wf.unknown'])}</b>`
            + (L.kind ? ` <span class="muted">(${_esc(T['wf.kind_'+L.kind]||L.kind)})</span>` : '')
            + (parts.length ? ` · ${_esc(parts.join(' · '))}` : '');
        ed.title = ed.textContent + (L.path ? '\n' + L.path : '');   // the line may end in … (CSS ellipsis)
    }
    document.getElementById('step1st').textContent = s.has_data
        ? [L.label, s.firmware && tr('wf.fw', {v:s.firmware})].filter(Boolean).join(' · ') : T['step.1_sub'];
    document.querySelectorAll('.step').forEach(b=>{
        const n=+b.dataset.step;
        b.classList.toggle('done', !!s.has_data && n===1);
        b.classList.toggle('current', s.has_data ? n===2 : n===1);
        b.disabled = !s.has_data && n>1;
    });
}
function markStep(n) {
    document.querySelectorAll('.step').forEach(b=>{ const k=+b.dataset.step; b.classList.toggle('current', k===n); if(k<n) b.classList.add('done'); });
}
// step ③ badge: how many changes the package would carry
let _chgTimer=null;
function scheduleChanges() { clearTimeout(_chgTimer); _chgTimer=setTimeout(refreshChangesBadge, 1200); }
async function refreshChangesBadge() {
    try {
        const r=await fetch('/api/changes'); if(r.status===409) return scheduleChanges();
        const d=await r.json(); if(d.status!=='ok') return;
        const b=document.querySelector('.step[data-step="3"]');
        document.getElementById('step3st').textContent = d.identical ? T['step.3_none'] : tr('step.3_count', {n:d.total});
        b.classList.toggle('badge', !d.identical);
    } catch(e) {}
}
// ① load: USB stick found by the server, three uploaded files, or (advanced) inputs/ sets
async function openLoad() {
    document.getElementById('loadModal').style.display='flex';
    LOAD_FILES={}; renderPicked(); refreshUsbInputs();
}
function closeLoad() { document.getElementById('loadModal').style.display='none'; }
async function refreshUsbInputs() {
    const box=document.getElementById('loadUsb'); box.textContent=T['gallery.loading'];
    try {
        const d=await (await fetch('/api/usb_inputs')).json();
        box.innerHTML='';
        if(!d.dirs.length) { box.innerHTML=`<p class="small chg-warn">${_esc(T['load.usb_none'])}</p>`; return; }
        d.dirs.forEach(x=>{
            const row=document.createElement('div'); row.className='load-usb-item';
            const p=document.createElement('span'); p.className='mono small'; p.textContent=x.path;
            const b=document.createElement('button'); b.textContent=T['load.usb_load'];
            if(x.path===d.active) { b.className='secondary'; b.textContent=T['inputs.reload']; }
            b.onclick=()=>loadFromUsb(x.path);
            row.append(p, b); box.appendChild(row);
        });
    } catch(e) { box.textContent=T['err.prefix']+e; }
}
// the backup sentence only when there are working files to back up (not on the very first load)
function loadConfirmText(name) {
    const s=tr('load.confirm', {name:name});
    return UI_CFG.session && UI_CFG.session.has_data ? s+'\n'+T['load.confirm_backup']+'\n'+T['load.confirm_keep'] : s;
}
async function loadFromUsb(path) {
    if(!(await confirmDialog(loadConfirmText(path), T['dialog.yes_load']))) return;
    setBusy(true);
    try {
        const d=await (await fetch('/api/load_inputs', {method:'POST', body:JSON.stringify({usb:path})})).json();
        if(d.status!=='ok') { setBusy(false); return toast(T['err.load']+(d.error||JSON.stringify(d)),'err',12000); }
        toast(tr('load.done', {seconds:d.seconds}),'ok'); setTimeout(()=>location.reload(), 900);
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
const LOAD_NAMES={image:'station_logos.img', fm:'fm_station_logo.db', dab:'dab_station_logo.db'};
let LOAD_FILES={};
function loadFilesPicked(inp) {
    Array.from(inp.files||[]).forEach(f=>{
        const k=Object.keys(LOAD_NAMES).find(k=>LOAD_NAMES[k]===f.name.toLowerCase());
        if(k) LOAD_FILES[k]=f; else toast(tr('load.unknown_file', {name:f.name}),'err',9000);
    });
    inp.value=''; renderPicked();
}
function renderPicked() {
    const ul=document.getElementById('loadPicked'); ul.innerHTML='';
    Object.entries(LOAD_NAMES).forEach(([k,n])=>{
        const f=LOAD_FILES[k], li=document.createElement('li');
        li.className=f?'ok':'miss';
        li.textContent=(f?'✓ ':'✗ ')+n+(f?` (${(f.size/1048576).toFixed(1)} MB)`:'');
        ul.appendChild(li);
    });
    document.getElementById('loadUploadBtn').disabled = Object.keys(LOAD_NAMES).some(k=>!LOAD_FILES[k]);
    document.getElementById('loadProgress').hidden=true;
}
async function uploadFiles(btn) {
    if(!(await confirmDialog(loadConfirmText(Object.values(LOAD_NAMES).join(', ')), T['dialog.yes_load']))) return;
    const fd=new FormData(); Object.keys(LOAD_NAMES).forEach(k=>fd.append(k, LOAD_FILES[k], LOAD_NAMES[k]));
    const pr=document.getElementById('loadProgress'), bar=pr.querySelector('div'), lbl=pr.querySelector('span');
    pr.hidden=false; btn.disabled=true;
    const xhr=new XMLHttpRequest(); xhr.open('POST','/api/load_upload');
    xhr.upload.onprogress=e=>{ if(e.lengthComputable) { const p=Math.round(e.loaded*100/e.total); bar.style.width=p+'%'; lbl.textContent=tr('load.progress', {pct:p}); } };
    xhr.upload.onload=()=>{ bar.style.width='100%'; lbl.textContent=T['load.checking']; };
    xhr.onload=()=>{
        let d={}; try { d=JSON.parse(xhr.responseText); } catch(e) {}
        if(xhr.status===200 && d.status==='ok') { toast(tr('load.done', {seconds:d.seconds}),'ok'); setTimeout(()=>location.reload(), 900); }
        else { btn.disabled=false; pr.hidden=true; toast(T['err.load']+(d.error||xhr.statusText),'err',15000); }
    };
    xhr.onerror=()=>{ btn.disabled=false; pr.hidden=true; toast(T['err.load']+'network','err'); };
    xhr.send(fd);
}
// ③ review: what the package changes compared with what was loaded from the car
async function openChanges() {
    setBusy(true);
    try {
        const r=await fetch('/api/changes'), d=await r.json(); setBusy(false);
        if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
        markStep(3);
        const body=document.getElementById('changesBody'); let h='';
        if(d.identical) h+=`<p class="chg-ok">${_esc(T['chg.identical'])}</p>`;
        const link=(country, q, text)=>country ? `<a href="#" data-c="${_esc(country)}" data-q="${_esc(q)}">${_esc(text)}</a>` : _esc(text);
        const list=(sec, fmt)=>`<ul class="chg-list">${sec.items.map(fmt).join('')}${sec.count>sec.items.length?`<li>${_esc(tr('chg.more', {n:sec.count-sec.items.length}))}</li>`:''}</ul>`;
        const rowTxt=it=>`${it.type} ${it.ecc} ${it.pi}${it.freq?' · '+it.freq+' MHz':''}${it.logo?' · '+it.logo:''}`;
        if(d.rows) {
            const R=d.rows;
            if(R.added.count||R.deleted.count||R.logo.count) {
                h+=`<section class="chg-sec"><h4>${_esc(T['chg.rows_h'])}</h4>`;
                if(R.added.count) h+=`<b>${_esc(tr('chg.added', {n:R.added.count}))}</b>`+list(R.added, it=>`<li>${link(it.country, it.pi, rowTxt(it))}</li>`);
                if(R.logo.count) h+=`<b>${_esc(tr('chg.logo', {n:R.logo.count}))}</b>`+list(R.logo, it=>`<li>${link(it.country, it.pi, rowTxt(it))} <span class="muted">(${_esc(it.old_logo||'—')} → ${_esc(it.logo||'—')})</span></li>`);
                if(R.deleted.count) h+=`<b>${_esc(tr('chg.deleted', {n:R.deleted.count}))}</b>`+list(R.deleted, it=>`<li>${_esc(rowTxt(it))}</li>`);
                h+=`<p class="small muted">${_esc(T['chg.edit_note'])}</p></section>`;
            }
        } else h+=`<p class="small chg-warn">${_esc(T['chg.no_orig'])}</p>`;
        const L=d.logos;
        if(L.added.count||L.replaced.count||L.styled.count) {
            h+=`<section class="chg-sec"><h4>${_esc(T['chg.logos_h'])}</h4>`;
            [['added','chg.l_added'],['replaced','chg.l_replaced'],['styled','chg.l_styled']].forEach(([k,t])=>{
                if(L[k].count) h+=`<b>${_esc(tr(t, {n:L[k].count}))}</b>`+list(L[k], it=>`<li>${link(it.country, it.q, it.key)}</li>`);
            });
            h+='</section>';
        }
        if(L.absent.count) h+=`<p class="chg-warn">${_esc(tr('chg.l_absent', {n:L.absent.count}))}</p>`+list(L.absent, it=>`<li>${link(it.country, it.q, it.key)}</li>`);
        if(L.undecided.count) h+=`<p class="small muted">${_esc(tr('chg.l_undecided', {n:L.undecided.count}))}</p>`;
        body.innerHTML=h;
        body.querySelectorAll('a[data-c]').forEach(a=>a.onclick=e=>{ e.preventDefault(); showInTable(a.dataset.c, a.dataset.q); });
        document.getElementById('changesModal').style.display='flex';
    } catch(e) { setBusy(false); toast(T['err.prefix']+e,'err'); }
}
function closeChanges() { document.getElementById('changesModal').style.display='none'; }
function showInTable(country, q) {
    document.getElementById('search').value=q;
    document.getElementById('dupToggle').checked=false; document.getElementById('noLogoToggle').checked=false;
    document.getElementById('pkgFilter').value='';
    if(country && country!==UI_CFG.country) {
        currentPage=1; saveState();
        try { document.cookie=`lu_country=${country}; path=/; max-age=31536000; SameSite=Lax`; } catch(e) {}
        setBusy(true); location.href='/?country='+encodeURIComponent(country); return;
    }
    closeChanges(); clearRowChecks(); filterTable(); resetPage(); saveState();
}
// ⚙ settings: language + station data source, written to config.yaml
let SETTINGS=null;
async function openSettings() {
    try {
        SETTINGS=await (await fetch('/api/settings')).json();
        const ls=document.getElementById('setLang'), ss=document.getElementById('setSource');
        ls.innerHTML=SETTINGS.languages.map(l=>`<option value="${_esc(l)}">${_esc(T['set.lang_'+l]||l)}</option>`).join('');
        ls.value=SETTINGS.language;
        ss.innerHTML=SETTINGS.sources.map(x=>`<option value="${_esc(x.id)}">${_esc(T['set.src_'+x.id]||x.name)}</option>`).join('');
        ss.value=SETTINGS.data_source;
        setSourceChanged();
        const pe=document.getElementById('setPluginErrors'), errs=SETTINGS.plugin_errors||[];
        pe.hidden=!errs.length; pe.textContent=errs.join(' · ');
        document.getElementById('settingsModal').style.display='flex';
    } catch(e) { toast(T['err.prefix']+e,'err'); }
}
function closeSettings() { document.getElementById('settingsModal').style.display='none'; }
// Fields of the selected source, drawn from its settings_schema (server: _source_fields).
function setSourceChanged() {
    const v=document.getElementById('setSource').value;
    document.getElementById('setSourceHelp').textContent = T['set.src_help_'+v] || '';
    const src=(SETTINGS.sources||[]).find(x=>x.id===v) || {fields:[]};
    document.getElementById('setFields').innerHTML = src.fields.map(f=>{
        const id='setF_'+_esc(f.key), req=f.required?' required':'', ph=_esc(f.placeholder||'');
        let input;
        if(f.type==='bool') input=`<input type="checkbox" id="${id}"${f.value?' checked':''}>`;
        else if(f.type==='choice') input=`<select id="${id}">${f.choices.map(c=>`<option${String(f.value)===c?' selected':''}>${_esc(c)}</option>`).join('')}</select>`;
        else {
            const type={secret:'password', number:'number', url:'url'}[f.type]||'text';
            const extra = f.type==='ecc' ? ' class="mono" maxlength="2"' : (f.type==='number' ? `${f.min!=null?` min="${f.min}"`:''}${f.max!=null?` max="${f.max}"`:''} step="any"` : '');
            const phx = f.type==='secret' && f.is_set ? _esc(T['set.secret_kept']||'••••') : ph;
            input=`<input type="${type}" id="${id}" value="${_esc(f.value)}" placeholder="${phx}"${extra}${req} autocomplete="off">`;
        }
        const help = f.help ? `<small class="muted">${_esc(f.help)}</small>` : '';
        return `<label><span>${_esc(f.label)}${f.required?' *':''}</span>${input}${help}</label>`;
    }).join('');
}
function _sourceSettingsBody(v) {
    const src=(SETTINGS.sources||[]).find(x=>x.id===v);
    if(!src || !src.fields.length) return null;
    const out={};
    src.fields.forEach(f=>{
        const el=document.getElementById('setF_'+f.key); if(!el) return;
        out[f.key] = f.type==='bool' ? el.checked : el.value.trim();
    });
    return out;
}
async function saveSettings(btn) {
    const body={language:document.getElementById('setLang').value, data_source:document.getElementById('setSource').value};
    const fields=_sourceSettingsBody(body.data_source);
    if(fields) body.sources={[body.data_source]: fields};
    btn.disabled=true;
    try {
        const d=await (await fetch('/api/settings', {method:'POST', body:JSON.stringify(body)})).json();
        btn.disabled=false;
        if(d.status!=='ok') return toast(T['err.prefix']+(d.error||JSON.stringify(d)),'err');
        toast(T['set.saved'],'ok'); setTimeout(()=>location.reload(), 700);
    } catch(e) { btn.disabled=false; toast(T['err.prefix']+e,'err'); }
}
