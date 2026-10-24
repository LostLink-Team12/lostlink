import React from 'react';

type Entry = { label: string; value?: string | null };
const formatter = new Intl.DateTimeFormat('en-GB', {
  year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
  second: '2-digit', timeZoneName: 'short',
});

export function RecordTimes({ entries }: { entries: Entry[] }) {
  const valid = entries.flatMap(({ label, value }) => {
    if (!value) return [];
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? [] : [{ label, date }];
  });
  if (!valid.length) return null;
  return <dl className="record-times">{valid.map(({ label, date }) => <div key={label}>
    <dt>{label}</dt><dd><time dateTime={date.toISOString()} title={date.toISOString()}>{formatter.format(date)}</time></dd>
  </div>)}</dl>;
}
