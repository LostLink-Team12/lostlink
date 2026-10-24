import React, { useEffect, useState } from 'react';
import type { Api } from './App';

export function ReportClaimMarker({ api, reportId, revision }: { api: Api; reportId: string; revision: number }) {
  const [claim, setClaim] = useState<{ state: string; created_at?: string } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true; setClaim(null); setError('');
    api('claim', `/v1/claims?lost_report_id=${encodeURIComponent(reportId)}&limit=1&offset=0`)
      .then(page => { if (active) setClaim(page.items[0] || null); })
      .catch(() => { if (active) setError('Could not load the related claim. Refresh records to retry.'); });
    return () => { active = false; };
  }, [api, reportId, revision]);
  return <>{claim && <p className="report-claim-marker"><strong>Claim submitted</strong> · Latest claim: {claim.state}</p>}{error && <p role="alert">{error}</p>}</>;
}
