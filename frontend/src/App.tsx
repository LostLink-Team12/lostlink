import React, { useCallback, useEffect, useRef, useState } from 'react';
import { photoBase64, registerItem, validatePhoto, waitOperation } from './api.js';

type Api = (service: string, path: string, options?: { body?: unknown; version?: number; key?: string; method?: string }) => Promise<any>;
type Item = { id?: string; item_id?: string; title: string; description: string; category: string; state?: string; score?: number; photo_url?: string; public_location_hint?: string };
type Claim = { id: string; item_title: string; state: string; version: number; answers?: { question_id: string; prompt: string; answer: string }[]; active_operation_id?: string };
type Question = { id: string; version: number; prompt: string };
const id = encodeURIComponent;
const text = (data: FormData, key: string) => String(data.get(key) || '').trim();
const file = (data: FormData) => { const value = data.get('photo') as File; return value?.size ? value : undefined; };

function useAction() {
  const [busy, setBusy] = useState(false), [message, setMessage] = useState(''), [error, setError] = useState('');
  const locked = useRef(false);
  const run = async (action: () => Promise<void>) => {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError(''); setMessage('');
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : 'Request failed'); }
    finally { locked.current = false; setBusy(false); }
  };
  return { busy, run, setMessage, status: <><p role="status" aria-live="polite">{busy ? 'Working…' : message}</p>{error && <p role="alert">{error}</p>}</> };
}

function Field({ label, name, type = 'text', required = true, maxLength = 2000 }: { label: string; name: string; type?: string; required?: boolean; maxLength?: number }) {
  return <label>{label}<input name={name} type={type} required={required} maxLength={maxLength} /></label>;
}
function Photo({ required = false }: { required?: boolean }) {
  return <label>Photo (JPEG, PNG or WebP)<input name="photo" type="file" accept="image/jpeg,image/png,image/webp" required={required} /></label>;
}

function PageList({ api, service, path, revision = 0, children }: { api: Api; service: string; path: string; revision?: number; children: (rows: any[]) => React.ReactNode }) {
  const [page, setPage] = useState({ items: [] as any[], has_more: false }), [offset, setOffset] = useState(0), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  useEffect(() => {
    let current = true; setLoading(true); setError('');
    api(service, `${path}${path.includes('?') ? '&' : '?'}limit=20&offset=${offset}`).then(value => { if (current) setPage(value); }).catch(e => { if (current) setError(e.message); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, service, path, offset, revision]);
  return <>{loading ? <p role="status">Loading…</p> : error ? <p role="alert">{error}</p> : <>{page.items.length ? children(page.items) : <p>No records yet.</p>}<div className="pagination"><button disabled={!offset} onClick={() => setOffset(x => x - 20)}>Previous</button><span>Page {offset / 20 + 1}</span><button disabled={!page.has_more} onClick={() => setOffset(x => x + 20)}>Next</button></div></>}</>;
}

function ItemCards({ items, choose }: { items: Item[]; choose?: (item: Item) => void }) {
  return <div className="cards">{items.map(item => <article key={item.item_id || item.id}>
    {item.photo_url && <img src={item.photo_url} alt={item.title} loading="lazy" />}
    <span className="tag">{item.category}</span><h3>{item.title}</h3><p>{item.description}</p>
    {item.public_location_hint && <p>{item.public_location_hint}</p>}
    {item.score !== undefined && <p>Similarity {item.score.toFixed(3)}</p>}
    {item.state && <p>Status: {item.state}</p>}
    {choose && <button onClick={() => choose(item)} aria-label={`Claim ${item.title}`}>This could be mine</button>}
  </article>)}</div>;
}

export function OwnerApp({ api }: { api: Api }) {
  const action = useAction();
  const [results, setResults] = useState<Item[] | null>(null), [truncated, setTruncated] = useState(false), [revision, setRevision] = useState(0);
  const [claim, setClaim] = useState<{ item_id: string; title: string; questions: Question[] } | null>(null);
  const [matches, setMatches] = useState<string | null>(null);
  const query = async (data: FormData) => {
    const photo = file(data);
    return { ...(text(data, 'description') ? { text: text(data, 'description') } : {}), ...(photo ? { photo_base64: await photoBase64(photo) } : {}), ...(text(data, 'category') ? { category: text(data, 'category') } : {}), ...(text(data, 'venue') ? { venue_id: text(data, 'venue') } : {}), ...(text(data, 'from') ? { found_from: new Date(text(data, 'from')).toISOString() } : {}), ...(text(data, 'until') ? { found_until: new Date(text(data, 'until')).toISOString() } : {}) };
  };
  const choose = (item: Item) => action.run(async () => { setClaim(await api('claim', `/v1/claim-questions?item_id=${id(item.item_id!)}`)); });
  return <>
    <section><p className="eyebrow">OWNER APP</p><h1>Find what matters.</h1><p>Search found items across venues, or save a lost report for future matches.</p>
      <form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); const mode = (e.nativeEvent as SubmitEvent).submitter?.getAttribute('value'); void action.run(async () => {
        const body = await query(data);
        if (mode === 'report') {
          if (!body.text) throw Error('Describe your lost item before saving a report.');
          const { text: description, ...filters } = body;
          await api('matching', '/v1/lost-reports', { body: { description, ...filters } });
          setRevision(x => x + 1); action.setMessage('Lost report saved. New matches will appear below.');
        } else {
          if (!body.text && !body.photo_base64) throw Error('Enter a description or choose a photo.');
          const value = await api('matching', '/v1/search', { body: { ...body, limit: 20 } }); setResults(value.items); setTruncated(value.potentially_truncated);
        }
      }); }}>
        <Field label="Describe your lost item" name="description" required={false} /><Photo />
        <details><summary>Filter results</summary><div className="grid"><Field label="Category" name="category" maxLength={100} required={false} /><Field label="Venue ID" name="venue" required={false} /><Field label="Found from" name="from" type="datetime-local" required={false} /><Field label="Found until" name="until" type="datetime-local" required={false} /></div></details>
        <div className="actions"><button disabled={action.busy} value="search">Search items</button><button disabled={action.busy} value="report" className="secondary">Save lost report</button></div>
      </form>{action.status}
    </section>
    {results && <section><h2>Search results</h2>{!results.length && <p>No matches. Try another description or save a lost report.</p>}{truncated && <p>Results may be incomplete. Narrow your filters.</p>}<ItemCards items={results} choose={choose} /></section>}
    {claim && <section><h2>Claim {claim.title}</h2><p>Tell the venue details only the owner would know.</p><form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void action.run(async () => {
      const contact = { ...(text(data, 'name') ? { name: text(data, 'name') } : {}), ...(text(data, 'phone') ? { phone: text(data, 'phone') } : {}) };
      await api('claim', '/v1/claims', { body: { item_id: claim.item_id, answers: claim.questions.map(q => ({ question_id: q.id, version: q.version, answer: text(data, q.id) })), ...(Object.keys(contact).length ? { contact } : {}) } });
      setClaim(null); setRevision(x => x + 1); action.setMessage('Claim submitted. The venue will review your answers.');
    }); }}>{claim.questions.map(q => <Field key={q.id} label={q.prompt} name={q.id} maxLength={1000} />)}<Field label="Contact name" name="name" required={false} maxLength={120} /><Field label="Phone" name="phone" type="tel" required={false} maxLength={32} /><button disabled={action.busy}>Submit claim</button><button type="button" className="secondary" onClick={() => setClaim(null)}>Cancel</button></form></section>}
    <section><h2>My lost reports</h2><button className="secondary" onClick={() => setRevision(x => x + 1)}>Refresh records</button><PageList api={api} service="matching" path="/v1/lost-reports" revision={revision}>{rows => rows.map(report => <article key={report.id}><h3>{report.description}</h3><p>{report.state}</p><button onClick={() => setMatches(report.id)}>View matches</button></article>)}</PageList></section>
    {matches && <section><h2>Report matches</h2><PageList api={api} service="matching" path={`/v1/lost-reports/${id(matches)}/matches`} revision={revision}>{rows => <ItemCards items={rows.map(row => row.item)} choose={choose} />}</PageList></section>}
    <section><h2>My claims</h2><PageList api={api} service="claim" path="/v1/claims" revision={revision}>{rows => <ClaimList rows={rows} api={api} venue={false} refresh={() => setRevision(x => x + 1)} />}</PageList></section>
  </>;
}

function ClaimList({ rows, api, venue, refresh }: { rows: Claim[]; api: Api; venue: boolean; refresh: () => void }) {
  const action = useAction(), [selected, setSelected] = useState<Claim | null>(null), [qr, setQr] = useState('');
  const path = `/v1/${venue ? 'venue/claims' : 'claims'}`;
  const approve = () => action.run(async () => {
    const result = await api('claim', `${path}/${id(selected!.id)}/approve`, { body: {}, version: selected!.version });
    const done = await waitOperation(api, 'claim', result.operation_id);
    if (done.claim_state !== 'approved') throw Error(`Approval ended with state ${done.claim_state}. Refresh before continuing.`);
    setSelected(null); refresh(); action.setMessage('Claim approved.');
  });
  return <>{rows.map(row => <article key={row.id}><h3>{row.item_title}</h3><p>Status: {row.state}</p><button disabled={action.busy} aria-label={`${venue ? 'Review' : 'View'} ${row.item_title}`} onClick={() => void action.run(async () => { setQr(''); setSelected(await api('claim', `${path}/${id(row.id)}`)); })}>{venue ? 'Review claim' : 'View claim'}</button></article>)}
    {selected && <article className="review"><h3>{selected.item_title}</h3><p>Status: {selected.state}</p>{selected.answers?.map(answer => <div key={answer.question_id}><strong>{answer.prompt}</strong><p>{answer.answer}</p></div>)}
      {selected.active_operation_id && <button disabled={action.busy} onClick={() => void action.run(async () => { await waitOperation(api, 'claim', selected.active_operation_id!); setSelected(await api('claim', `${path}/${id(selected.id)}`)); refresh(); action.setMessage('Operation completed.'); })}>Check pending operation</button>}
      {venue && selected.state === 'submitted' && <button disabled={action.busy} onClick={() => void approve()}>Approve claim</button>}
      {!venue && selected.state === 'approved' && <button disabled={action.busy} onClick={() => void action.run(async () => {
        const value = await api('claim', `${path}/${id(selected.id)}/handover-token`, { body: {} });
        setQr(value.qr_payload); action.setMessage(`Handover code expires ${new Date(value.expires_at).toLocaleString()}. Show the code at the venue.`);
      })}>Get handover code</button>}
      {qr && <p className="code">{qr}</p>}
    </article>}{action.status}</>;
}

export function VenueApp({ api }: { api: Api }) {
  const action = useAction(), [revision, setRevision] = useState(0), [venue, setVenue] = useState('Venue Portal');
  const progress = useRef<Record<string, any>>({}), [lockedForm, setLockedForm] = useState(false);
  const draft = useRef<{ item: object; photo: File } | null>(null);
  const refresh = useCallback(() => setRevision(x => x + 1), []);
  useEffect(() => { let current = true; api('venue', '/v1/venues/current').then(v => { if (current) setVenue(v.name); }).catch(() => {}); return () => { current = false; }; }, [api]);
  return <>
    <section><p className="eyebrow">VENUE PORTAL</p><h1>{venue}</h1><p>Register found items and help them get home.</p><button className="secondary" onClick={refresh}>Refresh records</button></section>
    <section><h2>Register a found item</h2><form onSubmit={e => { e.preventDefault(); const form = e.currentTarget, data = new FormData(form); void action.run(async () => {
      if (!draft.current) {
        validatePhoto(file(data), 10);
        draft.current = { photo: file(data)!, item: { title: text(data, 'title'), description: text(data, 'description'), category: text(data, 'category'), found_at: new Date(text(data, 'found_at')).toISOString(), public_location_hint: text(data, 'location') || null, verification_questions: [{ prompt: text(data, 'question'), reference_answer: text(data, 'answer') }] } };
        setLockedForm(true);
      }
      await registerItem(api, draft.current.item, draft.current.photo, progress.current, () => setLockedForm(true));
      progress.current = {}; draft.current = null; setLockedForm(false); form.reset(); refresh(); action.setMessage('Photo confirmed. The item will become searchable after embedding completes.');
    }); }}>
      <fieldset disabled={lockedForm || action.busy}><div className="grid"><Field label="Item title" name="title" maxLength={120} /><Field label="Category" name="category" maxLength={100} /></div><Field label="Description" name="description" /><Field label="Found at" name="found_at" type="datetime-local" /><Field label="Public location hint" name="location" required={false} maxLength={300} /><Photo required /><Field label="Verification question" name="question" maxLength={300} /><Field label="Private reference answer" name="answer" maxLength={1000} /></fieldset>
      <button disabled={action.busy}>{lockedForm ? 'Retry registration' : 'Register item'}</button>
    </form>{lockedForm && <p>Registration started. Keep this page open and retry with the same photo if the connection fails.</p>}{action.status}</section>
    <section><h2>Found items</h2><PageList api={api} service="item" path="/v1/items" revision={revision}>{rows => <ItemCards items={rows} />}</PageList></section>
    <section><h2>Claims to review</h2><PageList api={api} service="claim" path="/v1/venue/claims" revision={revision}>{rows => <ClaimList rows={rows} api={api} venue refresh={refresh} />}</PageList></section>
    <section><details><summary>Venue onboarding (platform administrators)</summary><form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void action.run(async () => {
      await api('venue', '/v1/admin/venues', { body: { name: text(data, 'name'), slug: text(data, 'slug'), initial_admin_sub: text(data, 'admin') } }); action.setMessage('Venue created. The initial administrator must sign in again after identity sync.');
    }); }}><Field label="Venue name" name="name" maxLength={120} /><Field label="Venue slug" name="slug" maxLength={80} /><Field label="Initial administrator Cognito subject" name="admin" maxLength={128} /><button disabled={action.busy}>Create venue</button></form></details></section>
  </>;
}
