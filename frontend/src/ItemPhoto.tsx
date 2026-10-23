import React, { useEffect, useRef, useState } from 'react';
import type { Api } from './App';

// The parent keys this component by the original URL, resetting its retry budget
// when a new search or match response supplies fresh metadata.
export function ItemPhoto({ itemId, title, url, api }: { itemId: string; title: string; url: string; api?: Api }) {
  const [source, setSource] = useState(url), [failed, setFailed] = useState(false), [refreshing, setRefreshing] = useState(false);
  const automaticUsed = useRef(false), pending = useRef(false), active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const refresh = async () => {
    if (!api || pending.current) { if (!api) setFailed(true); return; }
    pending.current = true; setRefreshing(true);
    try {
      const value = await api('matching', `/v1/items/${encodeURIComponent(itemId)}/photo`);
      if (active.current) {
        if (!value.photo_url || value.item_id !== itemId || value.photo_url === source) setFailed(true);
        else { setSource(value.photo_url); setFailed(false); }
      }
    } catch { if (active.current) setFailed(true); }
    finally { pending.current = false; if (active.current) setRefreshing(false); }
  };
  return <div>{failed ? <><p role="status">Photo unavailable.</p>{api && <button type="button" disabled={refreshing} onClick={() => void refresh()}>Retry photo for {title}</button>}</> : <img src={source} alt={title} loading="lazy" onError={() => {
    if (pending.current) return;
    if (!automaticUsed.current && api) { automaticUsed.current = true; void refresh(); }
    else setFailed(true);
  }} />}{refreshing && <p role="status">Refreshing photo…</p>}</div>;
}
