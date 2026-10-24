import React, { useEffect, useRef, useState } from 'react';

type PageLink = { id: string; label: string; description: string };

export function usePortalPage(defaultPage: string) {
  const read = () => location.hash.slice(1) || defaultPage;
  const [page, setPage] = useState(read);
  useEffect(() => {
    const changed = () => setPage(read());
    addEventListener('popstate', changed); addEventListener('hashchange', changed);
    return () => { removeEventListener('popstate', changed); removeEventListener('hashchange', changed); };
  }, [defaultPage]);
  const navigate = (next: string) => {
    if (location.hash !== `#${next}`) history.pushState({}, '', `#${next}`);
    setPage(next);
  };
  return [page, navigate] as const;
}

export function PortalLayout({ label, links, current, navigate, locked = false, children }: {
  label: string; links: PageLink[]; current: string; navigate: (page: string) => void;
  locked?: boolean; children: React.ReactNode;
}) {
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const heading = Array.from(content.current?.querySelectorAll<HTMLElement>('h1, h2') || []).find(node => !node.closest('[hidden]'));
    heading?.setAttribute('tabindex', '-1'); heading?.focus({ preventScroll: true });
  }, [current]);
  return <div className="portal-layout">
    <aside className="portal-sidebar"><p className="eyebrow">{label}</p><nav aria-label={`${label} navigation`}>
      {links.map(link => <a key={link.id} href={`#${link.id}`} aria-current={current === link.id ? 'page' : undefined} aria-disabled={locked || undefined}
        onClick={e => { e.preventDefault(); if (!locked) navigate(link.id); }}>
        <strong>{link.label}</strong><span>{link.description}</span>
      </a>)}
    </nav></aside>
    <div className="portal-content" ref={content}>{children}</div>
  </div>;
}
