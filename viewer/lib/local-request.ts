// Defense in depth for a LOOPBACK-ONLY viewer; these headers are not authentication.
function authority(value: string | null): string | null {
  if (!value) return null;
  const match = /^(localhost|127\.0\.0\.1|\[::1\])(?::([1-9][0-9]{0,4}))?$/.exec(value);
  if (!match || (match[2] && Number(match[2]) > 65535)) return null;
  return value;
}
export function isLocalRequest(request: Request): boolean {
  const host = authority(request.headers.get("host"));
  if (!host) return false;
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin" && site !== "none") return false;
  const origin = request.headers.get("origin");
  if (origin !== null) {
    // Compare the literal authority, not URL-normalized numeric IP aliases,
    // credentials, paths, query strings, trailing dots, or forwarded headers.
    const protocol = new URL(request.url).protocol;
    if ((protocol !== "http:" && protocol !== "https:") || origin !== `${protocol}//${host}`) return false;
  }
  return true;
}
export const localHeaders = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Cross-Origin-Resource-Policy": "same-origin",
};
export const forbidden = () => Response.json({ error: "Local same-origin requests only" }, { status: 403, headers: localHeaders });
