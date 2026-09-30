import type { Device, Review, AuditRun, Benchmark, CanonicalKey } from './types';

export async function request<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const form = opts?.body instanceof FormData;
  const res = await fetch('/api/v1' + path, {
    ...opts,
    headers: form ? opts?.headers : { 'Content-Type': 'application/json', ...opts?.headers },
  });
  const ct = res.headers.get('content-type') || '';
  const body: any = ct.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = typeof body === 'string' ? body : (body?.detail || body?.error || JSON.stringify(body));
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return body;
}

export function downloadReport(r: any) {
  const text =
    r.text ||
    [
      'CONFIGLENS / CONFIGURATION AUDIT',
      `Device: ${r.device}`,
      `Benchmarks: ${(r.benchmarks || []).join(', ')}`,
      `Score: ${r.summary?.score ?? '—'}% | Pass ${r.summary?.PASS ?? 0} | Fail ${r.summary?.FAIL ?? 0} | Unknown ${r.summary?.UNKNOWN ?? 0}`,
    ].join('\n');
  const blob = new Blob([text], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${r.device || 'report'}-audit.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

export const api = {
  listDevices: () => request<Device[]>('/devices'),
  getDevice: (id: string) => request<Device>('/devices/' + id),
  uploadDevices: (files: File[]) => {
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    return request<Device[]>('/devices', { method: 'POST', body: fd });
  },
  getReviewQueue: (id: string) => request<Review[]>('/devices/' + id + '/review'),
  submitReview: (id: string, tid: number, body: unknown) =>
    request<Device>(`/devices/${id}/review/${tid}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  listBenchmarks: () => request<Benchmark[]>('/benchmarks'),
  runAudit: (id: string, benchmarks: string[]) =>
    request<AuditRun>(`/devices/${id}/audit`, {
      method: 'POST',
      body: JSON.stringify({ benchmarks }),
    }),
  getAudit: (id: string) => request<AuditRun>(`/devices/${id}/audit`),
  getReport: (id: string) => request<any>(`/devices/${id}/audit/report`),
  listCanonicalKeys: () => request<CanonicalKey[]>('/ontology/keys'),
  getSource: (id: string) => request<{ text: string; filename: string }>(`/devices/${id}/source`),
};
