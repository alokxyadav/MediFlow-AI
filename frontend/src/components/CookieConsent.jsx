import { useState, useEffect } from 'react';
import { getCookie, setCookie } from '../utils/cookies';

const CONSENT_COOKIE = 'mediflow_cookie_consent'; // 'accepted' | 'necessary_only'

export default function CookieConsent({ policyOpen, onPolicyOpenChange }) {
  const [visible, setVisible] = useState(false);
  const [internalShowPolicy, setInternalShowPolicy] = useState(false);

  // Supports being opened from elsewhere (e.g. a footer link) via
  // controlled props, while still working standalone (banner's own
  // "Learn more" link) via internal state.
  const showPolicy = policyOpen !== undefined ? policyOpen : internalShowPolicy;
  const setShowPolicy = (value) => {
    if (onPolicyOpenChange) onPolicyOpenChange(value);
    else setInternalShowPolicy(value);
  };

  useEffect(() => {
    if (!getCookie(CONSENT_COOKIE)) setVisible(true);
  }, []);

  const choose = (value) => {
    setCookie(CONSENT_COOKIE, value);
    setVisible(false);
  };

  return (
    <>
      {visible && (
        <div
          role="dialog"
          aria-label="Cookie consent"
          className="fixed inset-x-0 bottom-0 z-[300] border-t border-[#E4EAF0] bg-white shadow-[0_-8px_24px_rgba(20,40,60,0.08)]"
        >
          <div className="max-w-[1600px] mx-auto px-4 sm:px-8 py-4 sm:py-5 flex flex-col sm:flex-row items-start sm:items-center gap-4">
            <p className="text-[12px] leading-relaxed text-[#465964] flex-1">
              <span className="font-extrabold text-[#20313D]">We use minimal cookies.</span>{' '}
              Only one — remembering this choice. MediFlow does not use
              tracking or advertising cookies, and your login session is
              kept in memory only (never stored in a cookie).{' '}
              <button
                onClick={() => setShowPolicy(true)}
                className="underline font-semibold text-[#3978A8] hover:text-[#2F6690]"
              >
                Learn more
              </button>
            </p>
            <div className="flex gap-2 flex-shrink-0 w-full sm:w-auto">
              <button
                onClick={() => choose('necessary_only')}
                className="flex-1 sm:flex-none px-4 py-2.5 rounded-lg border border-[#E5EBEF] text-[12px] font-bold text-[#687780] hover:bg-[#FAFBFC] transition"
              >
                Necessary only
              </button>
              <button
                onClick={() => choose('accepted')}
                className="flex-1 sm:flex-none px-4 py-2.5 rounded-lg bg-[#3978A8] text-white text-[12px] font-bold hover:bg-[#2F6690] transition"
              >
                Accept
              </button>
            </div>
          </div>
        </div>
      )}

      {showPolicy && (
        <div className="fixed inset-0 z-[310] flex items-center justify-center bg-black/40 px-4">
          <div className="w-full max-w-lg max-h-[80vh] overflow-y-auto bg-white rounded-2xl border border-[#E3EAF0] shadow-2xl p-6">
            <h3 className="text-lg font-extrabold text-[#20313D] mb-3">Cookie & Privacy Policy</h3>

            <div className="space-y-4 text-[12px] leading-relaxed text-[#465964]">
              <div>
                <p className="font-bold text-[#20313D] mb-1">What we store</p>
                <p>
                  A single cookie (<code className="bg-[#F5F7F9] px-1 rounded">mediflow_cookie_consent</code>)
                  recording your choice on this banner, valid for 1 year.
                  That's the only cookie MediFlow sets.
                </p>
              </div>
              <div>
                <p className="font-bold text-[#20313D] mb-1">What we don't do</p>
                <p>
                  No tracking cookies, no advertising cookies, no
                  third-party analytics pixels. Your staff/admin login
                  session is held in memory in your browser tab only — it
                  is never written to a cookie or to local storage, and is
                  cleared automatically when you close or refresh the page.
                </p>
              </div>
              <div>
                <p className="font-bold text-[#20313D] mb-1">Patient data</p>
                <p>
                  Symptom and appointment information you submit is sent
                  directly to the MediFlow API over HTTPS to generate care
                  recommendations. It is not used for advertising and is
                  not shared with third parties.
                </p>
              </div>
            </div>

            <button
              onClick={() => setShowPolicy(false)}
              className="mt-6 w-full px-4 py-2.5 rounded-lg bg-[#3978A8] text-white text-[12px] font-bold hover:bg-[#2F6690] transition"
            >
              Close
            </button>
          </div>
        </div>
      )}
    </>
  );
}
