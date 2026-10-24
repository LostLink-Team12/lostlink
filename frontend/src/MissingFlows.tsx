import QRCode from 'qrcode';
import React, { useEffect, useRef, useState } from 'react';
import { type Api, ItemCards, PageList, useAction } from './App';

export function ScanToken({ busy, submit }: { busy: boolean; submit: (token: string) => Promise<void> }) {
  const [token, setToken] = useState(''), [scanning, setScanning] = useState(false), [error, setError] = useState('');
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (!scanning) return;
    let active = true, stream: MediaStream | undefined, timer: ReturnType<typeof setTimeout>;
    void (async () => {
      try {
        const Detector = (window as any).BarcodeDetector;
        if (!Detector) throw Error('QR scanning is unavailable in this browser. Paste the original handover payload below.');
        const detector = new Detector({ formats: ['qr_code'] });
        const opened = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        if (!active) { opened.getTracks().forEach(t => t.stop()); return; }
        stream = opened;
        video.current!.srcObject = stream; await video.current!.play();
        const scan = async () => {
          if (!active) return;
          try {
            const codes = await detector.detect(video.current);
            if (!active) return;
            if (codes.length) { setToken(codes[0].rawValue); setScanning(false); return; }
            timer = setTimeout(() => void scan(), 300);
          } catch (e) { if (active) { setError(e instanceof Error ? e.message : 'Scan failed'); setScanning(false); } }
        };
        await scan();
      } catch (e) { if (active) { setError(e instanceof Error ? e.message : 'Camera unavailable'); setScanning(false); } }
    })();
    return () => { active = false; clearTimeout(timer); stream?.getTracks().forEach(t => t.stop()); };
  }, [scanning]);
  return <form onSubmit={e => { e.preventDefault(); void submit(token); }}>
    <p>Scan the owner's QR code or paste the exact handover payload. Confirm the item before completing handover.</p>
    <button type="button" disabled={busy} onClick={() => { setError(''); setScanning(s => !s); }}>{scanning ? 'Stop scanning' : 'Scan QR code'}</button>
    {scanning && <video ref={video} muted playsInline aria-label="QR camera preview" style={{ width: '100%', maxWidth: 400 }} />}
    {error && <p role="alert">{error}</p>}
    <label>Handover payload<input required minLength={20} maxLength={200} value={token} onChange={e => setToken(e.target.value)} autoComplete="off" /></label>
    <button disabled={busy || scanning}>Complete handover</button>
  </form>;
}

export function Members({ api, expanded = false }: { api: Api; expanded?: boolean }) {
  const action = useAction(), [revision, setRevision] = useState(0), [opened, setOpened] = useState(expanded);
  const changed = (member: any) => { setRevision(r => r + 1); action.setMessage(member.identity_sync_operation_id ? 'Member saved. Identity sync pending; sign in again after sync completes.' : 'Member saved.'); };
  return <section>{expanded && <h2 data-page-heading>Staff management</h2>}<details open={expanded || undefined} onToggle={e => setOpened(e.currentTarget.open)}><summary>Staff management (venue administrators)</summary>
    {opened && <><button disabled={action.busy} onClick={() => setRevision(r => r + 1)}>Refresh staff</button>
    <form onSubmit={e => { e.preventDefault(); const form = e.currentTarget, data = new FormData(form); void action.run(async () => {
      changed(await api('venue', '/v1/venues/current/members', { body: { user_sub: String(data.get('user_sub')).trim(), role: data.get('role') } })); form.reset();
    }); }}><label>Cognito subject<input name="user_sub" required maxLength={128} /></label><label>New member role<select name="role"><option value="venue_staff">Venue staff</option><option value="venue_admin">Venue administrator</option></select></label><button disabled={action.busy}>Add member</button></form>
    <PageList api={api} service="venue" path="/v1/venues/current/members" revision={revision}>{rows => rows.map(member => <article key={member.id}><h3>{member.user_sub}</h3><p>{member.role} / {member.status}</p><form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void action.run(async () => {
      changed(await api('venue', '/v1/venues/current/members/' + encodeURIComponent(member.id), { method: 'PATCH', version: member.version, body: { role: data.get('role'), status: data.get('status') } }));
    }); }}><label>Role for {member.user_sub}<select name="role" defaultValue={member.role}><option value="venue_staff">Venue staff</option><option value="venue_admin">Venue administrator</option></select></label><label>Status for {member.user_sub}<select name="status" defaultValue={member.status}><option value="active">Active</option><option value="suspended">Suspended</option></select></label><button disabled={action.busy}>Save member</button></form></article>)}</PageList></>}
    {action.status}</details></section>;
}

export function MatchLink({ api, choose, revision, busy = false }: { api: Api; choose: React.ComponentProps<typeof ItemCards>['choose']; revision: number; busy?: boolean }) {
  const matchId = location.pathname.match(/^\/matches\/([^/]+)\/?$/)?.[1];
  const [item, setItem] = useState<any>(null), [reportId, setReportId] = useState<string | undefined>(), [error, setError] = useState(''), [loading, setLoading] = useState(true), [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!matchId) return;
    let active = true;
    setLoading(true); setError(''); setItem(null);
    // Only owner-authorized public APIs are used; stale/foreign matches remain invisible.
    const pages = async (path: string, visit: (row: any) => Promise<boolean>): Promise<boolean> => {
      for (let offset = 0; offset <= 10000 && active; offset += 100) {
        const page = await api('matching', path + '?limit=100&offset=' + offset);
        for (const row of page.items) if (await visit(row)) return true;
        if (!page.has_more) return false;
      }
      if (active) throw Error('Too many records to resolve this link. Open your report matches below.');
      return false;
    };
    void pages('/v1/lost-reports', async report => report.state === 'open' && await pages('/v1/lost-reports/' + encodeURIComponent(report.id) + '/matches', async match => {
      if (match.id !== decodeURIComponent(matchId)) return false;
      if (active) { setItem(match.item); setReportId(report.id); }
      return true;
    })).then(found => { if (active && !found) setError('This match is no longer available or does not belong to your account.'); }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, matchId, revision, retry]);
  if (!matchId) return null;
  return <section><h2>Your notified match</h2>{loading ? <p role="status">Loading match...</p> : error ? <><p role="alert">{error}</p><button onClick={() => setRetry(r => r + 1)}>Retry match</button></> : <ItemCards api={api} items={[item]} choose={value => choose?.(value, reportId)} busy={busy} />}</section>;
}

export function HandoverCode({ payload }: { payload: string }) {
  const [url, setUrl] = useState(''), [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setUrl(''); setError('');
    void QRCode.toDataURL(payload, { width: 280, margin: 4, errorCorrectionLevel: 'M' }).then(value => { if (active) setUrl(value); }).catch(() => { if (active) setError('Could not render QR code. Show the original payload below.'); });
    return () => { active = false; };
  }, [payload]);
  return <div>{url && <img src={url} alt="Handover QR code" style={{ width: 280, height: 280 }} />}{error && <p role="alert">{error}</p>}<p className="code">{payload}</p></div>;
}
