import React from 'react';
import { createRoot } from 'react-dom/client';
import { PortalShell } from './PortalShell';
import { loadConfig } from './config';
import './style.css';

const root = createRoot(document.getElementById('root')!);
root.render(<p role="status">Loading settings…</p>);
void loadConfig(import.meta.env).then(settings => root.render(<PortalShell settings={settings} />)).catch(error => {
  root.render(<main><h1>LostLink</h1><p role="alert">{error.message}</p><button onClick={() => location.reload()}>Retry loading settings</button></main>);
});
