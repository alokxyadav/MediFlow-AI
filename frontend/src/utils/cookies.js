// Minimal cookie helpers - no dependency needed for simple key/value use.
// Used for the cookie-consent choice itself (see CookieConsent.jsx). The
// admin JWT is deliberately NOT stored here - it stays in React state only
// (see App.jsx's auth section) to limit XSS blast radius, a decision this
// file does not change.

export function setCookie(name, value, days = 365) {
  const expires = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toUTCString();
  const secure = window.location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax${secure}`;
}

export function getCookie(name) {
  const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
  return match ? decodeURIComponent(match[2]) : null;
}

export function deleteCookie(name) {
  document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
}
