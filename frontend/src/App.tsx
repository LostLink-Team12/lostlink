import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ItemPhoto } from './ItemPhoto';
import { ReportClaimMarker } from './ReportClaimMarker';
import { RecordTimes } from './RecordTimes';
import { PortalLayout, usePortalPage } from './PortalLayout';
import { FoundDateFilters, foundDateError, foundDateRange } from './SearchDates';
import { HandoverCode, MatchLink, Members, ScanToken } from './MissingFlows';
import { photoBase64, registerItem, validatePhoto, waitOperation } from './api.js';

export type Api = (service: string, path: string, options?: { body?: unknown; version?: number; key?: string; method?: string }) => Promise<any>;
type Item = { id?: string; item_id?: string; title: string; description: string; category: string; state?: string; created_at?: string; found_at?: string; score?: number; photo_url?: string; public_location_hint?: string };
type Claim = { id: string; item_title: string; state: string; version: number; created_at?: string; approved_at?: string | null; closed_at?: string | null; answers?: { question_id: string; prompt: string; answer: string }[]; active_operation_id?: string };
type Question = { id: string; version: number; prompt: string };
const id = encodeURIComponent;
const text = (data: FormData, key: string) => String(data.get(key) || '').trim();
const file = (data: FormData) => { const value = data.get('photo') as File; return value?.size ? value : undefined; };

export function useAction() {
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

export function PageList({ api, service, path, revision = 0, children }: { api: Api; service: string; path: string; revision?: number; children: (rows: any[]) => React.ReactNode }) {
  const [page, setPage] = useState({ items: [] as any[], has_more: false, source: '' }), [offset, setOffset] = useState(0), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  const source = `${service}:${path}`;
  useEffect(() => {
    let current = true; setLoading(true); setError('');
    api(service, `${path}${path.includes('?') ? '&' : '?'}limit=20&offset=${offset}`).then(value => { if (current) setPage({ ...value, source }); }).catch(e => { if (current) setError(e.message); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, service, path, offset, revision]);
  return <>{loading && <p role="status">{page.source === source ? 'Refreshing…' : 'Loading…'}</p>}{error && <p role="alert">{error}</p>}{page.source === source && <>{page.items.length ? children(page.items) : <p>No records yet.</p>}<div className="pagination"><button disabled={!offset} onClick={() => setOffset(x => x - 20)}>Previous</button><span>Page {offset / 20 + 1}</span><button disabled={!page.has_more} onClick={() => setOffset(x => x + 20)}>Next</button></div></>}</>;
}

export function ItemCards({ items, choose, busy = false, api }: { items: Item[]; choose?: (item: Item, reportId?: string) => void; busy?: boolean; api?: Api }) {
  return <div className="cards">{items.map(item => <article key={item.item_id || item.id}>
    {item.photo_url && <ItemPhoto key={item.photo_url} itemId={(item.item_id || item.id)!} title={item.title} url={item.photo_url} api={api} />}
    <span className="tag">{item.category}</span><h3>{item.title}</h3><p>{item.description}</p>
    {item.public_location_hint && <p>{item.public_location_hint}</p>}
    {item.score !== undefined && <p>Similarity {item.score.toFixed(3)}</p>}
    {item.state && <p>Status: {item.state}</p>}
    <RecordTimes entries={[{ label: 'Found', value: item.found_at }, { label: 'Registered', value: item.created_at }]} />
    {choose && <button disabled={busy} onClick={() => choose(item)} aria-label={`Claim ${item.title}`}>This could be mine</button>}
  </article>)}</div>;
}

export function OwnerApp({ api }: { api: Api }) {
  const action = useAction(), claimAction = useAction();
  const [route, navigate] = usePortalPage('search');
  const page = route.startsWith('claim/') ? 'claim' : ['search', 'reports', 'claims'].includes(route) ? route : 'search';
  const claimHeading = useRef<HTMLHeadingElement>(null);
  const [claimItem, setClaimItem] = useState<Item | null>(null), [claimSubmitted, setClaimSubmitted] = useState(false);
  useEffect(() => {
    if (claimItem) {
      claimHeading.current?.focus({ preventScroll: true });
      claimHeading.current?.scrollIntoView?.({ block: 'start' });
    }
  }, [claimItem]);
  const [results, setResults] = useState<Item[] | null>(null), [truncated, setTruncated] = useState(false), [revision, setRevision] = useState(0);
  const [claim, setClaim] = useState<{ item_id: string; title: string; questions: Question[] } | null>(null);
  const [matches, setMatches] = useState<string | null>(null), [claimReport, setClaimReport] = useState<string | null>(null);
  const query = async (data: FormData) => {
    const photo = file(data), dates = foundDateRange(text(data, 'from'), text(data, 'until'));
    return { ...(text(data, 'description') ? { text: text(data, 'description') } : {}), ...(photo ? { photo_base64: await photoBase64(photo) } : {}), ...(text(data, 'category') ? { category: text(data, 'category') } : {}), ...(text(data, 'venue') ? { venue_id: text(data, 'venue') } : {}), ...dates };
  };
  const choose = (item: Item, reportId?: string) => claimAction.run(async () => {
    setClaimItem(item); setClaim(null); setClaimSubmitted(false); setClaimReport(reportId || null);
    const itemId = item.item_id || item.id;
    if (!itemId) throw Error('This item is missing its identifier. Search again.');
    navigate(`claim/${id(itemId)}`);
    setClaim(await api('claim', `/v1/claim-questions?item_id=${id(itemId)}`));
  });
  return <PortalLayout label="Owner" links={[
    { id: 'search', label: 'Search', description: 'Find your belongings' },
    { id: 'reports', label: 'My lost reports', description: 'Track future matches' },
    { id: 'claims', label: 'My claims', description: 'Follow your requests' },
  ]} current={page} navigate={navigate} locked={claimAction.busy}>
    <div hidden={page !== 'search'}>
    <section><p className="eyebrow">OWNER APP</p><h1>Find what matters.</h1><p>Search found items across venues, or save a lost report for future matches.</p>
      <form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); const mode = (e.nativeEvent as SubmitEvent).submitter?.getAttribute('value'); if (foundDateError(text(data, 'from'), text(data, 'until'))) { const input = e.currentTarget.elements.namedItem('from') as HTMLInputElement; const filters = input.closest('details'); if (filters) filters.open = true; input.focus(); return; } void action.run(async () => {
        const body = await query(data);
        if (mode === 'report') {
          if (!body.text) throw Error('Describe your lost item before saving a report.');
          const { text: description, ...filters } = body;
          await api('matching', '/v1/lost-reports', { body: { description, ...filters } });
          setRevision(x => x + 1); action.setMessage('Lost report saved. View it in My lost reports.');
        } else {
          if (!body.text && !body.photo_base64) throw Error('Enter a description or choose a photo.');
          const value = await api('matching', '/v1/search', { body: { ...body, limit: 20 } }); setResults(value.items); setTruncated(value.potentially_truncated);
        }
      }); }}>
        <Field label="Describe your lost item" name="description" required={false} /><Photo />
        <details><summary>Filter results</summary><div className="grid"><Field label="Category" name="category" maxLength={100} required={false} /><Field label="Venue ID" name="venue" required={false} /><FoundDateFilters /></div></details>
        <div className="actions"><button disabled={action.busy} value="search">Search items</button><button disabled={action.busy} value="report" className="secondary">Save lost report</button></div>
      </form>{page === 'search' && action.status}
    </section>
    <MatchLink api={api} choose={choose} revision={revision} busy={claimAction.busy || action.busy} />
    {results && <section><h2>Search results</h2>{!results.length && <p>No matches. Try another description or save a lost report.</p>}{truncated && <p>Results may be incomplete. Narrow your filters.</p>}<ItemCards api={api} items={results} choose={choose} busy={claimAction.busy || action.busy} /></section>}
    </div>
    {page === 'claim' && claimItem && <div className="claim-layout"><div className="claim-item"><p className="eyebrow">SELECTED ITEM</p><ItemCards api={api} items={[claimItem]} /></div><section aria-labelledby="claim-heading">
      <h1 id="claim-heading" ref={claimHeading} tabIndex={-1}>Claim {claimItem.title}</h1>
      {claimAction.status}
      {claim && <><p>Tell the venue details only the owner would know.</p><form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void claimAction.run(async () => {
        const contact = { ...(text(data, 'name') ? { name: text(data, 'name') } : {}), ...(text(data, 'phone') ? { phone: text(data, 'phone') } : {}) };
        await api('claim', '/v1/claims', { body: { item_id: claim.item_id, ...(claimReport ? { lost_report_id: claimReport } : {}), answers: claim.questions.map(q => ({ question_id: q.id, version: q.version, answer: text(data, q.id) })), ...(Object.keys(contact).length ? { contact } : {}) } });
        setClaim(null); setClaimSubmitted(true); setRevision(x => x + 1); claimAction.setMessage('Claim submitted. The venue will review your answers.');
      }); }}>{claim.questions.map(q => <Field key={q.id} label={q.prompt} name={q.id} maxLength={1000} />)}<Field label="Contact name" name="name" required={false} maxLength={120} /><Field label="Phone" name="phone" type="tel" required={false} maxLength={32} /><button disabled={claimAction.busy}>Submit claim</button></form></>}
      {!claim && !claimSubmitted && !claimAction.busy && <button onClick={() => void choose(claimItem)}>Retry loading questions</button>}
      <button type="button" disabled={claimAction.busy} className="secondary" onClick={() => { setClaimItem(null); setClaim(null); navigate('search'); }}>{claimSubmitted ? 'Close' : 'Cancel'}</button>
    </section></div>}
    {page === 'claim' && !claimItem && <section><h1>Select an item to claim</h1><p>Return to Search and choose an item to see its current details and verification questions.</p><button onClick={() => navigate('search')}>Back to Search</button></section>}
    {page === 'reports' && <><section><h1>My lost reports</h1><p>Save a report from Search, then check for new matches here.</p>{action.status}<button className="secondary" onClick={() => setRevision(x => x + 1)}>Refresh records</button><PageList api={api} service="matching" path="/v1/lost-reports" revision={revision}>{rows => rows.map(report => <article key={report.id}><h3>{report.description}</h3><p>{report.state}</p><RecordTimes entries={[{ label: 'Reported', value: report.created_at }]} /><ReportClaimMarker api={api} reportId={report.id} revision={revision} /><button onClick={() => setMatches(report.id)}>View matches</button>{report.state === 'open' && <form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void action.run(async () => {
      await api('matching', '/v1/lost-reports/' + id(report.id) + '/close', { body: { reason: text(data, 'reason') }, version: report.version });
      setRevision(x => x + 1); action.setMessage('Lost report closed.');
    }); }}><label>Closure reason<select name="reason"><option value="found">Found my item</option><option value="no_longer_needed">No longer needed</option></select></label><button disabled={action.busy}>Close lost report</button></form>}</article>)}</PageList></section>
    {matches && <section><h2>Report matches</h2><PageList api={api} service="matching" path={`/v1/lost-reports/${id(matches)}/matches`} revision={revision}>{rows => <ItemCards api={api} items={rows.map(row => row.item)} choose={item => void choose(item, matches)} busy={claimAction.busy || action.busy} />}</PageList></section>}
    </>}
    {page === 'claims' && <section><h1>My claims</h1><p>View your requests, collect an approved item, or withdraw a request you no longer need.</p><button className="secondary" onClick={() => setRevision(x => x + 1)}>Refresh claims</button><PageList api={api} service="claim" path="/v1/claims" revision={revision}>{rows => <ClaimList rows={rows} api={api} venue={false} refresh={() => setRevision(x => x + 1)} />}</PageList></section>}
  </PortalLayout>;
}

function ClaimList({ rows, api, venue, refresh }: { rows: Claim[]; api: Api; venue: boolean; refresh: () => void }) {
  const action = useAction(), [selected, setSelected] = useState<Claim | null>(null), [qr, setQr] = useState(''), [withdrawing, setWithdrawing] = useState(false), [expandedId, setExpandedId] = useState<string | null>(null);
  const details = useRef<HTMLDivElement>(null);
  useEffect(() => { details.current?.focus({ preventScroll: true }); }, [selected?.id]);
  const path = `/v1/${venue ? 'venue/claims' : 'claims'}`;
  const approve = () => action.run(async () => {
    const result = await api('claim', `${path}/${id(selected!.id)}/approve`, { body: {}, version: selected!.version });
    const done = await waitOperation(api, 'claim', result.operation_id);
    if (done.claim_state !== 'approved') throw Error(`Approval ended with state ${done.claim_state}. Refresh before continuing.`);
    setSelected(null); refresh(); action.setMessage('Claim approved.');
  });
  return <>{rows.map(row => {
    const expanded = expandedId === row.id, loaded = expanded && selected?.id === row.id;
    const shown = loaded ? selected! : row;
    return <article key={row.id} className={expanded ? 'review' : undefined} aria-label={`Claim for ${row.item_title}`} aria-busy={expanded && action.busy}>
      <h3>{shown.item_title}</h3><p>Status: {shown.state}</p>
      <RecordTimes entries={[{ label: 'Submitted', value: shown.created_at }, { label: 'Approved', value: shown.approved_at }, { label: 'Closed', value: shown.closed_at }]} />
      {!loaded ? <button disabled={action.busy} aria-expanded={expanded} aria-controls={`claim-details-${row.id}`} aria-label={`${venue ? 'Review' : 'View'} ${row.item_title}`} onClick={() => void action.run(async () => {
        setExpandedId(row.id); setSelected(null); setQr(''); setWithdrawing(false);
        setSelected(await api('claim', `${path}/${id(row.id)}`));
      })}>{venue ? 'Review claim' : 'View claim'}</button> : <button className="secondary" disabled={action.busy} aria-expanded="true" aria-controls={`claim-details-${row.id}`} onClick={() => { setExpandedId(null); setSelected(null); setQr(''); setWithdrawing(false); }}>Hide details</button>}
      {loaded && selected && <div id={`claim-details-${row.id}`} ref={details} tabIndex={-1}>
<button disabled={action.busy} onClick={() => void action.run(async () => { setQr(''); setSelected(await api('claim', path + '/' + id(selected.id))); })}>Refresh claim details</button>{selected.answers?.map(answer => <div key={answer.question_id}><strong>{answer.prompt}</strong><p>{answer.answer}</p></div>)}
      {selected.active_operation_id && <button disabled={action.busy} onClick={() => void action.run(async () => { const done = await waitOperation(api, 'claim', selected.active_operation_id!); if (selected.state === 'cancellation_pending' && done.claim_state !== 'cancelled') throw Error('Withdrawal not completed. Refresh before continuing.'); if (selected.state === 'handover_pending' && done.claim_state !== 'handed_over') throw Error('Handover not completed. Refresh before continuing.'); setSelected(await api('claim', `${path}/${id(selected.id)}`)); refresh(); action.setMessage('Operation completed.'); })}>Check pending operation</button>}
      {venue && selected.state === 'submitted' && <><button disabled={action.busy} onClick={() => void approve()}>Approve claim</button><form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void action.run(async () => {
        const value = await api('claim', path + '/' + id(selected.id) + '/reject', { body: { reason_code: text(data, 'reason_code') }, version: selected.version });
        setSelected(value); refresh(); action.setMessage('Claim rejected.');
      }); }}><label>Rejection reason<select name="reason_code"><option value="answers_incorrect">Answers incorrect</option><option value="insufficient_detail">Insufficient detail</option><option value="duplicate">Duplicate</option><option value="other">Other</option></select></label><button disabled={action.busy}>Reject claim</button></form></>}
      {venue && selected.state === 'approved' && <ScanToken busy={action.busy} submit={token => action.run(async () => {
        const result = await api('claim', path + '/' + id(selected.id) + '/handover', { body: { token }, version: selected.version });
        setSelected({ ...selected, state: 'handover_pending', active_operation_id: result.operation_id });
        const done = await waitOperation(api, 'claim', result.operation_id);
        if (done.claim_state !== 'handed_over') throw Error('Handover not completed. Refresh before continuing.');
        setSelected(await api('claim', path + '/' + id(selected.id))); refresh(); action.setMessage('Item handed over.');
      })} />}
      {!venue && ['submitted', 'approval_pending', 'approved'].includes(selected.state) && <>
        <button className="secondary" disabled={action.busy} onClick={() => setWithdrawing(true)}>Withdraw claim</button>
        {withdrawing && <form aria-label="Withdraw claim" onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void action.run(async () => {
          const value = await api('claim', `${path}/${id(selected.id)}/cancel`, { body: { reason_code: text(data, 'withdrawal_reason') }, version: selected.version });
          setQr(''); setWithdrawing(false);
          if (value.operation_id) {
            setSelected({ ...selected, state: 'cancellation_pending', version: value.version, active_operation_id: value.operation_id }); refresh();
            const done = await waitOperation(api, 'claim', value.operation_id);
            if (done.claim_state !== 'cancelled') throw Error('Withdrawal not completed. Refresh before continuing.');
            setSelected(await api('claim', `${path}/${id(selected.id)}`));
          } else { setSelected(value); }
          refresh(); action.setMessage('Claim withdrawn. This request is no longer active.');
        }); }}>
          <p>Withdrawing cancels this request and releases any item reservation. The record remains in your history.</p>
          <label>Withdrawal reason<select name="withdrawal_reason"><option value="no_longer_needed">No longer needed</option><option value="found_elsewhere">Found elsewhere</option><option value="submitted_in_error">Submitted in error</option><option value="other">Other</option></select></label>
          <button disabled={action.busy}>Confirm withdrawal</button><button type="button" className="secondary" disabled={action.busy} onClick={() => setWithdrawing(false)}>Keep claim</button>
        </form>}
      </>}
      {!venue && selected.state === 'approved' && <button disabled={action.busy} onClick={() => void action.run(async () => {
        const value = await api('claim', `${path}/${id(selected.id)}/handover-token`, { body: {} });
        setQr(value.qr_payload); action.setMessage(`Handover code expires ${new Date(value.expires_at).toLocaleString()}. Show the code at the venue.`);
      })}>Get handover code</button>}
      {qr && <HandoverCode payload={qr} />}

      </div>}
      {expanded && action.status}
    </article>;
  })}</>;
}

type PortalAccess = { venue: { name: string } | null; can_manage_venue: boolean; can_manage_members: boolean; can_onboard_venues: boolean };

export function VenueApp({ api, goToOwner }: { api: Api; goToOwner?: () => void }) {
  const action = useAction(), onboardingAction = useAction(), [revision, setRevision] = useState(0);
  const [route, navigate] = usePortalPage('register');
  const [access, setAccess] = useState<PortalAccess | null>(null), [accessError, setAccessError] = useState(''), [accessAttempt, setAccessAttempt] = useState(0);
  const progress = useRef<Record<string, any>>({}), [lockedForm, setLockedForm] = useState(false);
  const draft = useRef<{ item: object; photo: File } | null>(null);
  const refresh = useCallback(() => setRevision(x => x + 1), []);
  useEffect(() => {
    let current = true; setAccess(null); setAccessError('');
    api('venue', '/v1/session').then(value => { if (current) setAccess(value); }).catch(e => { if (current) setAccessError(e instanceof Error ? e.message : 'Could not verify venue access.'); });
    return () => { current = false; };
  }, [api, accessAttempt]);
  if (accessError) return <section><h1>Venue access unavailable</h1><p role="alert">Could not verify your venue access. {accessError}</p><button onClick={() => setAccessAttempt(x => x + 1)}>Retry access check</button><a href="/owner-app/" onClick={e => { if (goToOwner) { e.preventDefault(); goToOwner(); } }}>Go to Owner App</a></section>;
  if (!access) return <section><h1>Venue Portal</h1><p role="status">Checking venue access…</p></section>;
  const staff = access.can_manage_venue === true;
  const administrator = staff && access.can_manage_members === true;
  const platform = access.can_onboard_venues === true;
  if (!staff && !platform) return <section><h1>Venue access required</h1><p>Your account does not have active venue access. Ask a venue administrator to add you, then sign in again after membership sync completes.</p><a href="/owner-app/" onClick={e => { if (goToOwner) { e.preventDefault(); goToOwner(); } }}>Go to Owner App</a><button onClick={() => setAccessAttempt(x => x + 1)}>Check access again</button></section>;
  const links = [
    ...(staff ? [
      { id: 'register', label: 'Register item', description: 'Record a found belonging' },
      { id: 'items', label: 'Found items', description: 'Browse your inventory' },
      { id: 'reviews', label: 'Claims to review', description: 'Review and hand over' },
    ] : []),
    ...(administrator ? [{ id: 'members', label: 'Staff management', description: 'Manage venue access' }] : []),
    ...(platform ? [{ id: 'onboarding', label: 'Venue onboarding', description: 'Create a new venue' }] : []),
  ];
  const page = links.some(link => link.id === route) ? route : links[0].id;
  return <PortalLayout label="Venue" links={links} current={page} navigate={navigate} locked={action.busy || onboardingAction.busy}>
    <section><p className="eyebrow">VENUE PORTAL</p><h1>{access.venue?.name || 'Venue Portal'}</h1><p>Register found items and help them get home.</p><button className="secondary" onClick={() => { refresh(); setAccessAttempt(x => x + 1); }}>Refresh records</button></section>
    {staff && <div hidden={page !== 'register'}><section><h2 data-page-heading>Register a found item</h2><form onSubmit={e => { e.preventDefault(); const form = e.currentTarget, data = new FormData(form); void action.run(async () => {
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
    </div>}
    {staff && page === 'items' && <section><h2 data-page-heading>Found items</h2><PageList api={api} service="item" path="/v1/items" revision={revision}>{rows => <ItemCards items={rows} />}</PageList></section>}
    {staff && page === 'reviews' && <section><h2 data-page-heading>Claims to review</h2><PageList api={api} service="claim" path="/v1/venue/claims" revision={revision}>{rows => <ClaimList rows={rows} api={api} venue refresh={refresh} />}</PageList></section>}
    {administrator && page === 'members' && <Members api={api} expanded />}
    {platform && page === 'onboarding' && <section><h2 data-page-heading>Venue onboarding (platform administrators)</h2><form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); void onboardingAction.run(async () => {
      await api('venue', '/v1/admin/venues', { body: { name: text(data, 'name'), slug: text(data, 'slug'), initial_admin_sub: text(data, 'admin') } }); onboardingAction.setMessage('Venue created. The initial administrator must sign in again after identity sync.');
    }); }}><Field label="Venue name" name="name" maxLength={120} /><Field label="Venue slug" name="slug" maxLength={80} /><Field label="Initial administrator Cognito subject" name="admin" maxLength={128} /><button disabled={onboardingAction.busy}>Create venue</button></form>{onboardingAction.status}</section>}
  </PortalLayout>;
}
